"""Command-line entry point and production dependency composition."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import traceback
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

import httpx
from openai import OpenAI
import yaml

from .agentic_discovery import AgenticSearchOrchestrator
from .analyzer import DeepSeekAnalyzer, ResilientAnalyzer, RuleFallbackAnalyzer
from .config import Settings, load_settings
from .discovery import BochaProvider, OfficialSeed, OfficialSeedCollector, QueryPlanner
from .fetcher import PageFetcher
from .matching import ProjectMatcher
from .models import SourceGrade, VerificationStatus
from .notifier import DingTalkNotifier, suppress_secret_bearing_http_logs
from .pipeline import Pipeline, RunResult
from .report import DingTalkShortReportRenderer, RenderedReport
from .repository import StateRepository
from .timebox import beijing_now
from .verifier import RuleVerifier, SourceRegistry
from .verification_followup import VerificationFollowupPlanner


BEIJING = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FAILURE_DIAGNOSTICS_NAME = "failure-diagnostics.json"
_RUN_RESULT_NAME = "run-result.json"
_CANDIDATE_CHECKPOINT_NAME = "candidate-checkpoint.json"
_DELIVERY_STATUS_NAME = "delivery-status.json"


class ConfigurationError(ValueError):
    """Raised when non-secret adapter configuration is invalid."""


class RunAlreadyActive(RuntimeError):
    """Raised when another local process owns the configured data directory."""


class DeliveryGateError(RuntimeError):
    """Raised when a real test delivery does not meet the current-run gate."""


class _LocalRunLock:
    """Non-blocking OS lock that is automatically released if a process exits."""

    FILENAME = ".laser-space-daily.lock"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._handle: Any | None = None

    def __enter__(self) -> "_LocalRunLock":
        self._data_dir.mkdir(parents=True, exist_ok=True)
        handle = (self._data_dir / self.FILENAME).open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            handle.close()
            raise RunAlreadyActive("daily pipeline already active") from error
        self._handle = handle
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _load_official_seeds(path: Path) -> list[OfficialSeed]:
    try:
        with path.open(encoding="utf-8") as source_file:
            raw = yaml.safe_load(source_file) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ConfigurationError("official source configuration is unavailable") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("official_sources"), list):
        raise ConfigurationError("official source configuration is invalid")
    try:
        return [OfficialSeed.model_validate(item) for item in raw["official_sources"]]
    except (TypeError, ValueError) as error:
        raise ConfigurationError("official source configuration is invalid") from error


def build_pipeline(settings: Settings) -> Pipeline:
    """Compose the production pipeline without performing external requests."""
    seeds = _load_official_seeds(settings.official_sources_path)
    search_client = httpx.Client(timeout=settings.bocha.timeout_seconds)
    official_client = httpx.Client(timeout=settings.discovery.fetch_timeout_seconds)
    model_client = OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek.base_url,
        timeout=settings.deepseek.timeout_seconds,
        max_retries=settings.deepseek.max_retries,
    )
    deepseek = DeepSeekAnalyzer(
        model_client,
        flash_model=settings.deepseek.flash_model,
        pro_model=settings.deepseek.pro_model,
        max_attempts=1,
    )
    analyzer = ResilientAnalyzer(deepseek, RuleFallbackAnalyzer())
    domains: dict[str, SourceGrade] = {seed.domain: seed.grade for seed in seeds}
    registry = SourceRegistry(
        domains,
        financing_company_domains=(
            settings.financing_sources.official_company_domains
        ),
        financing_investor_domains=(
            settings.financing_sources.official_investor_domains
        ),
        financing_b_domains=settings.financing_sources.independent_media_domains,
    )
    planner = QueryPlanner(
        max_queries=settings.discovery.max_queries,
        financing_domains=registry.financing_domains,
    )
    search_provider = BochaProvider(settings.bocha_api_key, client=search_client)
    researcher = AgenticSearchOrchestrator(
        client=model_client,
        search_provider=search_provider,
        fallback_planner=QueryPlanner(
            max_queries=min(20, settings.discovery.max_queries),
            financing_domains=(
                settings.financing_sources.independent_media_domains
            ),
        ),
        model=settings.deepseek.pro_model,
        mode=settings.discovery.mode,
        search_budget=settings.discovery.max_queries,
        max_agent_rounds=settings.discovery.max_agent_rounds,
        max_results_per_call=settings.discovery.max_results_per_call,
        stop_after_no_new_rounds=settings.discovery.stop_after_no_new_rounds,
    )
    return Pipeline(
        repository=StateRepository(settings.data_dir),
        planner=planner,
        search_provider=search_provider,
        official_collector=OfficialSeedCollector(seeds, client=official_client),
        fetcher=PageFetcher(timeout=settings.discovery.fetch_timeout_seconds),
        analyzer=analyzer,
        verifier=RuleVerifier(registry),
        matcher=ProjectMatcher(),
        trend_summarizer=deepseek,
        logger=LOGGER,
        researcher=researcher,
        verification_followup=VerificationFollowupPlanner(
            financing_b_domains=(
                settings.financing_sources.independent_media_domains
            ),
            official_company_domains=(
                settings.financing_sources.official_company_domains
            ),
            official_investor_domains=(
                settings.financing_sources.official_investor_domains
            ),
            elastic_budget=settings.discovery.daily_elastic_budget,
            pool_days=settings.discovery.verification_pool_days,
            max_targets=settings.discovery.verification_max_targets,
            stop_after_no_new=(
                settings.discovery.verification_stop_after_no_new
            ),
        )
        if settings.discovery.mode == "daily"
        else None,
        checkpoint_writer=lambda payload: _atomic_write_json(
            settings.data_dir / _CANDIDATE_CHECKPOINT_NAME,
            payload,
        ),
    )


def _build_renderer(settings: Settings) -> DingTalkShortReportRenderer:
    return DingTalkShortReportRenderer(max_chars=settings.report.max_chars)


def _build_notifier(settings: Settings) -> DingTalkNotifier:
    return DingTalkNotifier(
        settings.dingtalk_webhook,
        settings.dingtalk_secret,
        timeout_seconds=settings.notifier.timeout_seconds,
    )


@dataclass(frozen=True)
class CliDependencies:
    """Factories that isolate CLI orchestration from concrete adapters in tests."""

    settings_loader: Callable[[Path], Settings] = load_settings
    pipeline_factory: Callable[[Settings], Any] = build_pipeline
    renderer_factory: Callable[[Settings], Any] = _build_renderer
    notifier_factory: Callable[[Settings], Any] = _build_notifier


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="laser-space-daily")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-label", action="store_true")
    parser.add_argument("--discovery-mode", choices=("daily", "backfill"))
    parser.add_argument("--max-queries", type=_non_negative_int)
    parser.add_argument("--now")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return beijing_now()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def _atomic_write_report(path: Path, report: RenderedReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            markdown = report.markdown.replace("\r\n", "\n").replace("\r", "\n")
            temporary_file.write(markdown)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_write_research_trace(path: Path, trace: list[dict[str, Any]]) -> None:
    _atomic_write_json(path, trace)


def _mark_test_report(report: RenderedReport) -> RenderedReport:
    title = report.title.replace("# ", "# 【测试】", 1)
    markdown = report.markdown.replace(report.title, title, 1)
    return report.model_copy(update={"title": title, "markdown": markdown})


def _safe_traceback_frames(
    error: BaseException,
) -> list[dict[str, str | int]]:
    frames: list[dict[str, str | int]] = []
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename).resolve()
        try:
            relative = path.relative_to(_PROJECT_ROOT)
        except ValueError:
            continue
        if relative.suffix != ".py":
            continue
        frames.append(
            {
                "path": relative.as_posix(),
                "line": frame.lineno,
                "function": frame.name,
            }
        )
    return frames


def _log_failure(
    code: str,
    stage: str,
    error: BaseException,
) -> None:
    LOGGER.error(
        "cli_failure code=%s error=%s stage=%s frames=%s",
        code,
        type(error).__name__,
        stage,
        json.dumps(_safe_traceback_frames(error), ensure_ascii=True),
    )


def _write_failure_diagnostic(
    settings: Settings,
    stage: str,
    error: BaseException,
    now: datetime,
) -> None:
    _atomic_write_json(
        settings.data_dir / _FAILURE_DIAGNOSTICS_NAME,
        {
            "schema_version": 1,
            "status": "failure",
            "stage": stage,
            "error_type": type(error).__name__,
            "occurred_at": now.isoformat(),
            "frames": _safe_traceback_frames(error),
        },
    )


def _record_failure(
    settings: Settings,
    *,
    code: str,
    stage: str,
    error: BaseException,
    now: datetime,
) -> None:
    _log_failure(code, stage, error)
    try:
        (settings.data_dir / _RUN_RESULT_NAME).unlink(missing_ok=True)
        _write_failure_diagnostic(settings, stage, error, now)
    except Exception as diagnostic_error:
        LOGGER.error(
            "diagnostic_write_failed error=%s",
            type(diagnostic_error).__name__,
        )


def _clear_run_metadata(settings: Settings) -> None:
    for name in (
        _FAILURE_DIAGNOSTICS_NAME,
        _RUN_RESULT_NAME,
        _CANDIDATE_CHECKPOINT_NAME,
        _DELIVERY_STATUS_NAME,
    ):
        (settings.data_dir / name).unlink(missing_ok=True)


def _write_run_result(
    settings: Settings,
    report_path: Path,
    now: datetime,
    *,
    status: str = "success",
    failure_stage: str | None = None,
) -> None:
    report_reference = (
        report_path.resolve()
        .relative_to(settings.report_dir.parent.resolve())
        .as_posix()
    )
    payload = {
        "schema_version": 1,
        "status": status,
        "occurred_at": now.isoformat(),
        "report_path": report_reference,
    }
    if failure_stage is not None:
        payload["failure_stage"] = failure_stage
    _atomic_write_json(settings.data_dir / _RUN_RESULT_NAME, payload)


def _write_delivery_status(
    settings: Settings,
    *,
    status: str,
    now: datetime,
    report_kind: str,
    error: BaseException | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "occurred_at": now.isoformat(),
        "report_kind": report_kind,
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
    _atomic_write_json(settings.data_dir / _DELIVERY_STATUS_NAME, payload)


def _validate_test_delivery(
    result: RunResult,
    report: RenderedReport,
) -> None:
    changed_event_ids = set(result.changed_event_ids)
    changed_financing_ids = set(result.changed_financing_ids)
    changed_project_ids = set(result.changed_project_ids)
    verified_event_ids = {
        item.event_id
        for item in result.state.events
        if item.formal_record
        and item.verification_status is VerificationStatus.VERIFIED
    }
    current_verified = len(changed_event_ids & verified_event_ids)
    current_verified += sum(
        item.financing_id in changed_financing_ids
        and item.verification_status is VerificationStatus.VERIFIED
        for item in result.state.financings
    )
    current_verified += sum(
        project.project_id in changed_project_ids
        and bool(set(project.event_ids) & verified_event_ids)
        for project in result.state.projects
    )
    if current_verified < 1:
        raise DeliveryGateError("current_run_has_no_strict_verified_item")

    category_headings = (
        "一、商业航天融资新闻",
        "二、招标采购情况",
    )
    category_has_content = False
    for heading in category_headings:
        marker = f"## {heading}\n"
        if marker not in report.markdown:
            continue
        body = report.markdown.split(marker, maxsplit=1)[1]
        body = body.split("\n## ", maxsplit=1)[0]
        if any(
            line.lstrip().startswith("- ")
            and "](" in line
            and "暂无可展示" not in line
            for line in body.splitlines()
        ):
            category_has_content = True
            break
    if not category_has_content:
        raise DeliveryGateError("category_sections_have_no_sourced_content")


def _safe_recovery_text(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "|"):
        text = text.replace(character, "")
    return text[:limit]


def _safe_recovery_link(url: object) -> str:
    value = str(url or "").strip()
    return value if value.startswith(("https://", "http://")) else ""


def _recovery_report(
    settings: Settings,
    *,
    now: datetime,
    stage: str,
    test_label: bool,
) -> tuple[RenderedReport, bool]:
    checkpoint_path = settings.data_dir / _CANDIDATE_CHECKPOINT_NAME
    candidates: list[dict[str, Any]] = []
    if checkpoint_path.is_file():
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            raw_candidates = payload.get("candidates")
            if isinstance(raw_candidates, list):
                candidates = [
                    item for item in raw_candidates if isinstance(item, dict)
                ]
        except (OSError, UnicodeError, json.JSONDecodeError):
            candidates = []

    date_text = now.astimezone(BEIJING).date().isoformat()
    if candidates:
        title = f"# 【降级】中国激光与商业航天情报快报｜{date_text}"
        lines = [
            title,
            "",
            (
                f"运行在 `{_safe_recovery_text(stage, 60)}` 阶段失败；"
                "以下仅为本轮已抓取并完成结构分析的线索，"
                "均未通过最终严格核验。"
            ),
            "",
            "## 本轮可用线索",
            "",
        ]
        visible = [
            item
            for item in candidates
            if item.get("in_china") is True and item.get("in_scope") is True
        ][:5]
        if not visible:
            lines.append("- 本轮检查点中没有境内且主题相关的可用线索。")
        for item in visible:
            parts = ["- 候选线索（未核实）"]
            category = _safe_recovery_text(item.get("category"), 40)
            published_at = _safe_recovery_text(item.get("published_at"), 32)
            organization = _safe_recovery_text(item.get("organization"), 80)
            title_text = _safe_recovery_text(item.get("title"), 140)
            amount = _safe_recovery_text(item.get("amount"), 40)
            if category:
                parts.append(category)
            if published_at:
                parts.append(published_at[:10])
            if organization:
                parts.append(organization)
            parts.append(title_text)
            if amount:
                parts.append(f"明确金额：{amount}")
            source_url = _safe_recovery_link(item.get("source_url"))
            if source_url:
                parts.append(f"[查看原始来源]({source_url})")
            lines.append("｜".join(parts))
            summary = _safe_recovery_text(item.get("summary"), 180)
            if summary:
                lines.append(f"  - 摘要（未核实）：{summary}")
        lines.extend(
            (
                "",
                "## 状态说明",
                "",
                "- 本消息不包含任何“已核实”结论。",
                "- Actions 仍会显示失败，请以失败诊断和后续修复为准。",
            )
        )
        has_intelligence = bool(visible)
    else:
        title = f"# 【异常】情报日报运行告警｜{date_text}"
        lines = [
            title,
            "",
            (
                f"运行在 `{_safe_recovery_text(stage, 60)}` 阶段失败，"
                "且尚未形成本轮候选检查点。"
            ),
            "",
            "- 本消息不包含情报内容。",
            "- 未使用历史日报或推测性信息进行填充。",
        ]
        has_intelligence = False

    report = RenderedReport(title=title, markdown="\n".join(lines).strip() + "\n")
    if test_label:
        report = _mark_test_report(report)
    return report, has_intelligence


def _recover_failed_run(
    arguments: Any,
    selected: CliDependencies,
    settings: Settings,
    *,
    now: datetime,
    stage: str,
) -> None:
    try:
        report, has_intelligence = _recovery_report(
            settings,
            now=now,
            stage=stage,
            test_label=arguments.test_label,
        )
        report_path = settings.report_dir / f"{now.date().isoformat()}-degraded.md"
        _atomic_write_report(report_path, report)
        _write_run_result(
            settings,
            report_path,
            now,
            status="degraded",
            failure_stage=stage,
        )
        if arguments.dry_run:
            _write_delivery_status(
                settings,
                status="skipped",
                now=now,
                report_kind="degraded" if has_intelligence else "alert",
            )
            return
        try:
            selected.notifier_factory(settings).send(report)
        except Exception as error:
            _write_delivery_status(
                settings,
                status="failed",
                now=now,
                report_kind="degraded" if has_intelligence else "alert",
                error=error,
            )
        else:
            _write_delivery_status(
                settings,
                status="accepted",
                now=now,
                report_kind="degraded" if has_intelligence else "alert",
            )
    except Exception as recovery_error:
        LOGGER.error(
            "recovery_report_failed error=%s",
            type(recovery_error).__name__,
        )


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level))
    suppress_secret_bearing_http_logs()


def _run_locked_cycle(
    arguments: Any,
    selected: CliDependencies,
    settings: Settings,
    now: datetime,
) -> int:
    try:
        _clear_run_metadata(settings)
    except Exception as error:
        _record_failure(
            settings,
            code="pipeline",
            stage="diagnostics_write",
            error=error,
            now=now,
        )
        _recover_failed_run(
            arguments,
            selected,
            settings,
            now=now,
            stage="diagnostics_write",
        )
        return 4

    try:
        stage = "pipeline_build"
        pipeline = selected.pipeline_factory(settings)
    except ConfigurationError as error:
        _record_failure(
            settings,
            code="configuration",
            stage=stage,
            error=error,
            now=now,
        )
        _recover_failed_run(
            arguments,
            selected,
            settings,
            now=now,
            stage=stage,
        )
        return 2
    except Exception as error:
        _record_failure(
            settings,
            code="pipeline",
            stage=stage,
            error=error,
            now=now,
        )
        _recover_failed_run(
            arguments,
            selected,
            settings,
            now=now,
            stage=stage,
        )
        return 4

    try:
        stage = "pipeline_run"
        result = pipeline.run(now)
        stage = "report_render"
        renderer = selected.renderer_factory(settings)
        report = renderer.render(result)
        if arguments.test_label:
            report = _mark_test_report(report)
        report_path = settings.report_dir / f"{now.date().isoformat()}.md"
        stage = "report_write"
        _atomic_write_report(report_path, report)
        stage = "diagnostics_write"
        if result.research_trace:
            _atomic_write_research_trace(
                settings.data_dir / "research-trace.json",
                result.research_trace,
            )
        _atomic_write_json(
            settings.data_dir / "candidate-diagnostics.json",
            [
                item.model_dump(mode="json")
                for item in result.candidate_diagnostics
            ],
        )
    except Exception as error:
        _record_failure(
            settings,
            code="pipeline",
            stage=stage,
            error=error,
            now=now,
        )
        _recover_failed_run(
            arguments,
            selected,
            settings,
            now=now,
            stage=stage,
        )
        return 4

    if arguments.dry_run:
        try:
            _write_run_result(settings, report_path, now)
            _write_delivery_status(
                settings,
                status="skipped",
                now=now,
                report_kind="standard",
            )
        except Exception as error:
            _record_failure(
                settings,
                code="pipeline",
                stage="diagnostics_write",
                error=error,
                now=now,
            )
            return 4
        return 0
    if arguments.test_label:
        try:
            _validate_test_delivery(result, report)
        except DeliveryGateError as error:
            try:
                _write_delivery_status(
                    settings,
                    status="failed",
                    now=now,
                    report_kind="standard",
                    error=error,
                )
                _write_run_result(settings, report_path, now)
            except Exception as status_error:
                LOGGER.error(
                    "delivery_gate_status_write_failed error=%s",
                    type(status_error).__name__,
                )
            _record_failure(
                settings,
                code="notification",
                stage="delivery_gate",
                error=error,
                now=now,
            )
            return 3
    try:
        selected.notifier_factory(settings).send(report)
    except Exception as error:
        try:
            _write_delivery_status(
                settings,
                status="failed",
                now=now,
                report_kind="standard",
                error=error,
            )
        except Exception as status_error:
            LOGGER.error(
                "delivery_status_write_failed error=%s",
                type(status_error).__name__,
            )
        _record_failure(
            settings,
            code="notification",
            stage="notification",
            error=error,
            now=now,
        )
        return 3
    try:
        _write_run_result(settings, report_path, now)
        _write_delivery_status(
            settings,
            status="accepted",
            now=now,
            report_kind="standard",
        )
    except Exception as error:
        _record_failure(
            settings,
            code="pipeline",
            stage="diagnostics_write",
            error=error,
            now=now,
        )
        return 4
    return 0


def run_cli(
    argv: Sequence[str], dependencies: CliDependencies | None = None
) -> int:
    """Run one report cycle and return a stable process exit code."""
    try:
        arguments = _build_parser().parse_args(list(argv))
    except SystemExit as error:
        return int(error.code or 0)
    _configure_logging(arguments.log_level)
    selected = dependencies or CliDependencies()

    try:
        now = _parse_now(arguments.now)
        settings = selected.settings_loader(Path(arguments.config))
        discovery_updates: dict[str, Any] = {}
        if arguments.discovery_mode is not None:
            discovery_updates["mode"] = arguments.discovery_mode
        if arguments.max_queries is not None:
            discovery_updates["max_queries"] = arguments.max_queries
        if discovery_updates:
            discovery_document = {
                **settings.discovery.model_dump(),
                **discovery_updates,
            }
            settings = settings.model_copy(
                update={
                    "discovery": type(settings.discovery).model_validate(
                        discovery_document
                    )
                }
            )
    except Exception as error:
        _log_failure("configuration", "configuration", error)
        return 2

    try:
        with _LocalRunLock(settings.data_dir):
            return _run_locked_cycle(arguments, selected, settings, now)
    except RunAlreadyActive as error:
        _log_failure("pipeline", "pipeline_lock", error)
        return 4


def main() -> NoReturn:
    raise SystemExit(run_cli(sys.argv[1:]))
