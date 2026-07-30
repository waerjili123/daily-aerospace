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
from .models import SourceGrade
from .notifier import DingTalkNotifier, suppress_secret_bearing_http_logs
from .pipeline import Pipeline
from .report import RenderedReport, ReportRenderer
from .repository import StateRepository
from .timebox import beijing_now
from .verifier import RuleVerifier, SourceRegistry
from .verification_followup import VerificationFollowupPlanner


BEIJING = ZoneInfo("Asia/Shanghai")
LOGGER = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FAILURE_DIAGNOSTICS_NAME = "failure-diagnostics.json"
_RUN_RESULT_NAME = "run-result.json"


class ConfigurationError(ValueError):
    """Raised when non-secret adapter configuration is invalid."""


class RunAlreadyActive(RuntimeError):
    """Raised when another local process owns the configured data directory."""


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
    )
    deepseek = DeepSeekAnalyzer(
        model_client,
        flash_model=settings.deepseek.flash_model,
        pro_model=settings.deepseek.pro_model,
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
            max_queries=4,
            financing_domains=registry.financing_domains,
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
    )


def _build_renderer(settings: Settings) -> ReportRenderer:
    return ReportRenderer(max_chars=settings.report.max_chars)


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
    for name in (_FAILURE_DIAGNOSTICS_NAME, _RUN_RESULT_NAME):
        (settings.data_dir / name).unlink(missing_ok=True)


def _write_run_result(
    settings: Settings,
    report_path: Path,
    now: datetime,
) -> None:
    report_reference = (
        report_path.resolve()
        .relative_to(settings.report_dir.parent.resolve())
        .as_posix()
    )
    _atomic_write_json(
        settings.data_dir / _RUN_RESULT_NAME,
        {
            "schema_version": 1,
            "status": "success",
            "occurred_at": now.isoformat(),
            "report_path": report_reference,
        },
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
        return 2
    except Exception as error:
        _record_failure(
            settings,
            code="pipeline",
            stage=stage,
            error=error,
            now=now,
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
        return 4

    if arguments.dry_run:
        try:
            _write_run_result(settings, report_path, now)
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
    try:
        selected.notifier_factory(settings).send(report)
    except Exception as error:
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
