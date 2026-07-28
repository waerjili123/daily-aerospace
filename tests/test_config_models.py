from datetime import datetime
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError

from laser_space_daily.cli import build_pipeline
from laser_space_daily.config import load_settings
from laser_space_daily.models import Candidate, Category, Event, Evidence, SourceGrade, VerificationStatus
from laser_space_daily.timebox import daily_window, rolling_start


REPOSITORY_ROOT = Path(__file__).parents[1]


def _repository_file(path: str) -> Path:
    return REPOSITORY_ROOT / path


def _base_yaml(path: str) -> dict:
    """Load repository YAML without YAML 1.1 coercing ``on`` to a boolean."""
    loaded = yaml.load(_repository_file(path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _workflow_step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_workflow_is_manual_bounded_one_time_dingtalk_test():
    workflow_path = ".github/workflows/daily-intelligence.yml"
    workflow = _repository_file(workflow_path).read_text(encoding="utf-8")
    document = _base_yaml(workflow_path)

    assert set(document["on"]) == {"workflow_dispatch"}
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in workflow
    assert "BOCHA_API_KEY: ${{ secrets.BOCHA_API_KEY }}" in workflow
    assert "DINGTALK_WEBHOOK: ${{ secrets.DINGTALK_WEBHOOK }}" in workflow
    assert "DINGTALK_SECRET: ${{ secrets.DINGTALK_SECRET }}" in workflow
    assert "concurrency:" in workflow
    assert document["on"]["workflow_dispatch"]["inputs"]["max_queries"] == {
        "description": "Bocha hard query budget (daily <=12, backfill <=40)",
        "type": "choice",
        "options": ["4", "12", "40"],
        "default": "12",
    }
    assert document["on"]["workflow_dispatch"]["inputs"]["discovery_mode"] == {
        "description": "Daily incremental or one-time 90-day backfill",
        "type": "choice",
        "options": ["daily", "backfill"],
        "default": "daily",
    }
    assert document["concurrency"] == {
        "group": "laser-space-daily",
        "cancel-in-progress": "false",
    }
    pipeline_step = _workflow_step(
        document["jobs"]["run"]["steps"], "Run daily pipeline"
    )
    assert "--dry-run" not in pipeline_step["run"]
    assert "--test-label" in pipeline_step["run"]
    assert "--discovery-mode daily" in pipeline_step["run"]
    assert "--max-queries 12" in pipeline_step["run"]
    guard_step = _workflow_step(
        document["jobs"]["run"]["steps"], "Guard one-time DingTalk test branch"
    )
    assert guard_step["run"] == (
        'test "${GITHUB_REF}" = '
        '"refs/heads/codex/agentic-dingtalk-test-20260728"'
    )


def test_committed_config_contains_no_secret_values():
    expected_config = {
        "deepseek": {
            "timeout_seconds": "60",
            "base_url": "https://api.deepseek.com",
            "flash_model": "deepseek-v4-flash",
            "pro_model": "deepseek-v4-pro",
        },
        "bocha": {"timeout_seconds": "30"},
        "discovery": {
            "mode": "daily",
            "max_queries": "12",
            "daily_search_budget": "12",
            "backfill_search_budget": "40",
            "max_agent_rounds": "8",
            "max_results_per_call": "10",
            "stop_after_no_new_rounds": "2",
            "fetch_timeout_seconds": "15",
        },
        "report": {"max_chars": "18000"},
        "notifier": {"timeout_seconds": "15"},
        "data_dir": "data",
        "report_dir": "reports",
        "official_sources_path": "config/official_sources.yaml",
        "financing_sources": {
            "official_company_domains": {
                "landspace.com": "蓝箭航天",
                "spacepioneer.cc": "天兵科技",
                "cas-space.com": "中科宇航",
                "galactic-energy.cn": "星河动力航天",
                "orienspace.com": "东方空间",
                "yinhehangtian.cn": "银河航天",
                "minospace.cn": "微纳星空",
                "geespace.com": "时空道宇",
            },
                "official_investor_domains": {},
            "independent_media_domains": ["cls.cn", "pedaily.cn", "stcn.com"],
        },
    }
    config = _base_yaml("config.yaml")
    example = _base_yaml("config.example.yaml")

    assert config == expected_config
    assert example == expected_config
    sensitive_patterns = (
        r"\bsk-[A-Za-z0-9_-]{16,}\b",
        r"\btvly-[A-Za-z0-9_-]{16,}\b",
        r"access_token=[^&\s\"']+",
        r"https://oapi\.dingtalk\.com/robot/send\?access_token=[^&\s\"']+",
    )
    for path in ("config.yaml", "config.example.yaml"):
        contents = _repository_file(path).read_text(encoding="utf-8").lower()
        assert "deepseek_api_key" not in contents
        assert "bocha_api_key" not in contents
        assert "dingtalk_webhook" not in contents
        assert "dingtalk_secret" not in contents
        assert "oapi.dingtalk.com" not in contents
    for path in (
        "config.yaml",
        "config.example.yaml",
        ".github/workflows/daily-intelligence.yml",
        "README.md",
    ):
        contents = _repository_file(path).read_text(encoding="utf-8").lower()
        assert not any(re.search(pattern, contents) for pattern in sensitive_patterns)
    ignored = _repository_file(".gitignore").read_text(encoding="utf-8").splitlines()
    assert "config.yaml" not in ignored
    assert "data/" not in ignored
    assert "reports/" not in ignored
    assert ".env" in ignored
    assert "*.log" in ignored
    assert ".cache/" in ignored
    assert "tmp/" in ignored
    assert ".worktrees/" in ignored


def test_workflow_uses_python_313_tests_and_artifact_without_state_commit():
    workflow_path = ".github/workflows/daily-intelligence.yml"
    workflow = _repository_file(workflow_path).read_text(encoding="utf-8")
    document = _base_yaml(workflow_path)
    job = document["jobs"]["run"]
    steps = job["steps"]

    assert document["permissions"] == {"contents": "read"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "45"
    assert any(
        step.get("uses")
        == "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
        and step.get("with", {}).get("fetch-depth") == "0"
        for step in steps
    )
    assert any(
        step.get("uses")
        == "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
        and step.get("with", {}).get("python-version") == "3.13"
        and step.get("with", {}).get("cache") == "pip"
        for step in steps
    )
    assert 'python -m pip install -e ".[dev]"' in workflow
    install_step = _workflow_step(steps, "Install project and development dependencies")
    assert install_step["env"] == {"PIP_CONSTRAINT": "constraints.txt"}
    assert "python -m pytest -q" in workflow
    assert steps.index(_workflow_step(steps, "Run tests")) < steps.index(
        _workflow_step(steps, "Run daily pipeline")
    )
    artifact_step = _workflow_step(steps, "Upload generated report")
    assert artifact_step["uses"] == (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert artifact_step["if"] == "always()"
    assert artifact_step["with"]["path"].splitlines() == ["reports/", "data/"]
    assert all(step.get("name") != "Commit state and report" for step in steps)
    assert "git add data reports" not in workflow
    assert "git push origin" not in workflow
    assert "set -x" not in workflow
    assert "https://oapi.dingtalk.com/robot/send" not in workflow


def test_constraints_pin_every_direct_build_and_test_dependency() -> None:
    constraints = {
        line.strip()
        for line in _repository_file("constraints.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert constraints == {
        "beautifulsoup4==4.15.0",
        "hatchling==1.27.0",
        "httpx==0.28.1",
        "openai==1.109.1",
        "pydantic==2.13.4",
        "pytest==8.4.2",
        "pytest-cov==6.2.1",
        "PyYAML==6.0.3",
        "python-dateutil==2.9.0.post0",
        "respx==0.23.1",
        "trafilatura==2.1.0",
    }


def test_readme_documents_schema_migration_and_single_writer_lock() -> None:
    readme = _repository_file("README.md").read_text(encoding="utf-8")

    for required in (
        "state.json",
        "schema_version=2",
        "v1",
        "单写入者",
        ".laser-space-daily.lock",
        "constraints.txt",
    ):
        assert required in readme


def test_one_time_test_workflow_cannot_schedule_or_commit() -> None:
    document = _base_yaml(".github/workflows/daily-intelligence.yml")
    workflow = _repository_file(
        ".github/workflows/daily-intelligence.yml"
    ).read_text(encoding="utf-8")
    steps = document["jobs"]["run"]["steps"]

    assert set(document["on"]) == {"workflow_dispatch"}
    assert "--test-label" in _workflow_step(steps, "Run daily pipeline")["run"]
    assert "secrets.DINGTALK" in workflow
    assert all(step.get("name") != "Commit state and report" for step in steps)


def test_readme_documents_required_setup_and_dry_run():
    readme = _repository_file("README.md").read_text(encoding="utf-8")

    for required_text in (
        "AI日报",
        "Python 3.13",
        "--dry-run",
        "私有",
        "DEEPSEEK_API_KEY",
        "BOCHA_API_KEY",
        "DINGTALK_WEBHOOK",
        "DINGTALK_SECRET",
        "dry_run=true",
        "dry_run=false",
        "07:30",
        "artifact",
        "A/B/C",
        "pending",
        "JSON",
        "JSONL",
        "SQLite",
        "PostgreSQL",
        "退出码 2",
        "退出码 3",
        "退出码 4",
    ):
        assert required_text in readme


def test_hatch_wheel_package_path_is_explicit():
    pyproject_path = Path(__file__).parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)

    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/laser_space_daily"
    ]


def test_settings_require_four_secrets(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("report:\n  max_chars: 18000\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        load_settings(path)


def test_windows_use_beijing_time_and_calendar_months():
    now = datetime(2026, 7, 22, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    start, end = daily_window(now)
    assert start.isoformat() == "2026-07-21T07:30:00+08:00"
    assert end == now
    assert rolling_start(now).isoformat() == "2026-04-22T07:30:00+08:00"


def test_event_rejects_unverified_formal_record():
    with pytest.raises(ValueError, match="verified"):
        Event(
            event_id="e1",
            category=Category.LASER_COMMUNICATION,
            title="空间激光通信终端",
            organization="某研究院",
            published_at="2026-07-22T00:00:00+08:00",
            source_url="https://example.gov.cn/a",
            source_grade=SourceGrade.A,
            verification_status=VerificationStatus.PENDING,
        )


def test_event_rejects_removed_candidates_field():
    with pytest.raises(ValidationError, match="candidates"):
        Event(
            event_id="e1",
            category=Category.LASER_COMMUNICATION,
            title="空间激光通信终端",
            organization="某研究院",
            published_at="2026-07-22T00:00:00+08:00",
            source_url="https://example.gov.cn/a",
            source_grade=SourceGrade.A,
            verification_status=VerificationStatus.VERIFIED,
            candidates=[],
        )


@pytest.mark.parametrize(
    ("legacy_field", "value"),
    [
        ("name", "某供应商"),
        ("organization", "某单位"),
        ("rank", 1),
        ("amount_cny", 1.0),
    ],
)
def test_candidate_models_discovered_web_result_and_forbids_bidder_fields(
    legacy_field, value
):
    candidate = Candidate(
        title="空间激光通信终端采购公告",
        url="https://example.gov.cn/a",
        discovered_at="2026-07-22T00:00:00+08:00",
        discovery_source="bocha",
    )

    assert candidate.summary == ""
    with pytest.raises(ValidationError, match=legacy_field):
        Candidate(
            title="空间激光通信终端采购公告",
            url="https://example.gov.cn/a",
            discovered_at="2026-07-22T00:00:00+08:00",
            discovery_source="bocha",
            **{legacy_field: value},
        )


def test_production_financing_registry_is_explicit_and_used_by_pipeline(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("BOCHA_API_KEY", "test-bocha-key")
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://example.invalid/test-webhook")
    monkeypatch.setenv("DINGTALK_SECRET", "test-dingtalk-secret")

    settings = load_settings(_repository_file("config.yaml"))
    pipeline = build_pipeline(settings)
    registry = pipeline._verifier._registry

    assert settings.financing_sources.official_company_domains["landspace.com"] == "蓝箭航天"
    assert settings.financing_sources.independent_media_domains == [
        "cls.cn",
        "pedaily.cn",
        "stcn.com",
    ]
    assert registry.grade_financing("https://news.landspace.com/a", "蓝箭航天") is SourceGrade.A
    assert registry.grade_financing("https://landspace.com/a", "无关航天公司") is SourceGrade.C
    assert registry.grade_financing("https://www.cls.cn/detail/1", "蓝箭航天") is SourceGrade.B


def test_build_pipeline_wires_configured_official_investor_registry(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("BOCHA_API_KEY", "test-bocha-key")
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://example.invalid/test-webhook")
    monkeypatch.setenv("DINGTALK_SECRET", "test-dingtalk-secret")
    settings = load_settings(_repository_file("config.yaml"))
    sources = settings.financing_sources.model_copy(
        update={"official_investor_domains": {"capital.example": ["远航产业基金"]}}
    )
    registry = build_pipeline(
        settings.model_copy(update={"financing_sources": sources})
    )._verifier._registry
    evidence = [Evidence(
        field="investors", quote="远航产业基金参与本轮融资",
        source_url="https://capital.example/announcement",
    )]

    assert registry.grade_financing(
        "https://capital.example/announcement", "星舟航天", ["远航产业基金"], evidence
    ) is SourceGrade.A
    assert registry.grade_financing(
        "https://capital.example/announcement", "另一家公司", ["无关基金"], evidence
    ) is SourceGrade.C
    assert registry.grade_financing(
        "https://capital.example/announcement", "星舟航天", ["远航产业基金"], []
    ) is SourceGrade.C


@pytest.mark.parametrize(
    "financing_sources",
    [None, {}, {"official_company_domains": {}, "independent_media_domains": []}],
)
def test_missing_or_empty_financing_registry_fails_fast(
    tmp_path, monkeypatch, financing_sources
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("BOCHA_API_KEY", "test-bocha-key")
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://example.invalid/test-webhook")
    monkeypatch.setenv("DINGTALK_SECRET", "test-dingtalk-secret")
    payload = {
        "official_sources_path": str(_repository_file("config/official_sources.yaml")),
    }
    if financing_sources is not None:
        payload["financing_sources"] = financing_sources
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises((ValueError, ValidationError), match="financing"):
        load_settings(path)
