from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest

from laser_space_daily.analyzer import DeepSeekAnalyzer, ResilientAnalyzer
from laser_space_daily.cli import (
    CliDependencies,
    RunAlreadyActive,
    _LocalRunLock,
    _build_parser,
    build_pipeline,
    run_cli,
)
from laser_space_daily.config import Settings
from laser_space_daily.discovery import BochaProvider, OfficialSeedCollector, QueryPlanner
from laser_space_daily.fetcher import PageFetcher
from laser_space_daily.matching import ProjectMatcher
from laser_space_daily.models import (
    AnalysisResult,
    Category,
    Event,
    EventType,
    Evidence,
    Financing,
    PendingItem,
    Project,
    RunMetrics,
    SourceGrade,
    StateBundle,
    TrendSummary,
    VerificationStatus,
)
from laser_space_daily.notifier import DingTalkNotifier, NotificationError
from laser_space_daily.pipeline import RunResult
from laser_space_daily.report import (
    RenderedReport,
    ReportRenderer,
    ReportTooLong,
    _actionable_deadline,
    _followup_lines,
)
from laser_space_daily.repository import StateRepository
from laser_space_daily.verifier import RuleVerifier


BEIJING = ZoneInfo("Asia/Shanghai")
WINDOW_START = datetime(2026, 7, 21, 9, 30, tzinfo=BEIJING)
WINDOW_END = datetime(2026, 7, 22, 9, 30, tzinfo=BEIJING)
ROLLING_START = datetime(2026, 4, 22, 9, 30, tzinfo=BEIJING)
WEBHOOK = "https://dingtalk.example/robot/send/test-token-never-log"
DINGTALK_SECRET = "SECtest-signing-secret-never-log"


def dt(month: int, day: int, hour: int = 8) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=BEIJING)


def event(
    event_id: str,
    title: str,
    published_at: datetime,
    source_url: str,
    *,
    category: Category = Category.LASER_COMMUNICATION,
    event_type: EventType = EventType.TENDER,
    organization: str = "中国航天采购中心",
    amount: str | None = None,
) -> Event:
    analysis = AnalysisResult(
        in_china=True,
        in_scope=True,
        category=category,
        event_type=event_type,
        title=title,
        organization=organization,
        published_at=published_at,
        amount=amount,
        source_url=source_url,
    )
    return Event(
        event_id=event_id,
        category=category,
        title=title,
        organization=organization,
        published_at=published_at,
        source_url=source_url,
        source_grade=SourceGrade.A,
        verification_status=VerificationStatus.VERIFIED,
        event_type=event_type,
        analysis=analysis,
    )


def project(
    project_id: str,
    name: str,
    category: Category,
    status: str,
    event_ids: list[str],
    latest_event_at: datetime,
    latest_source_url: str,
    *,
    current_stage: EventType,
    amount: str | None = None,
    deadlines: dict[str, datetime] | None = None,
    organization: str = "中国航天采购中心",
) -> Project:
    supplied_deadlines = deadlines or {}
    return Project(
        project_id=project_id,
        name=name,
        organization=organization,
        category=category,
        status=status,
        event_ids=event_ids,
        current_stage=current_stage,
        amount=amount,
        first_published_at=latest_event_at,
        latest_event_at=latest_event_at,
        deadlines=supplied_deadlines,
        deadline_evidence={
            label: Evidence(
                field=f"{label}_deadline",
                quote=value.isoformat(),
                source_url=latest_source_url,
            )
            for label, value in supplied_deadlines.items()
        },
        deadline_precision={label: "minute" for label in supplied_deadlines},
        latest_source_url=latest_source_url,
    )


def test_date_only_deadline_remains_actionable_and_in_followup_until_local_day_end():
    deadline = dt(7, 22, 0)
    item = project(
        "date-deadline", "当日截止项目", Category.LASER_COMMUNICATION,
        "open", [], dt(7, 21, 9), "https://official.example/deadline",
        current_stage=EventType.TENDER,
        deadlines={"bid_submission": deadline},
    )
    item.deadline_precision = {"bid_submission": "date"}
    morning = dt(7, 22, 9)
    late = dt(7, 22, 23)
    following_day = dt(7, 23, 0)

    assert _actionable_deadline(item, morning) == deadline
    assert _actionable_deadline(item, late) == deadline
    assert _actionable_deadline(item, following_day) is None
    result = make_result(state=StateBundle(projects=[item]))
    assert any("当日截止项目" in line for line in _followup_lines(result, {}))


def financing(
    financing_id: str = "f-new",
    *,
    company: str = "星河动力",
    announced_at: datetime | None = None,
    source_urls: list[str] | None = None,
    evidence: list[Evidence] | None = None,
    source_published_at: dict[str, datetime] | None = None,
) -> Financing:
    urls = source_urls or [
        "https://company.example/financing",
        "https://media.example/financing",
    ]
    return Financing(
        financing_id=financing_id,
        company=company,
        announced_at=announced_at or dt(7, 22, 7),
        round_name="A轮",
        amount_cny=100_000_000,
        amount_disclosed=True,
        business_area="商业运载火箭",
        investors=["航天资本", "未来基金"],
        source_url=urls[0],
        source_urls=urls,
        source_published_at=source_published_at or {},
        evidence=evidence
        or [
            Evidence(
                field="企业公告",
                quote="公司宣布完成融资",
                source_url=urls[0],
            ),
            Evidence(
                field="权威媒体报道",
                quote="融资消息获确认",
                source_url=urls[1],
            ),
        ],
        verification_status=VerificationStatus.VERIFIED,
    )


def make_result(
    *,
    state: StateBundle | None = None,
    changed_event_ids: list[str] | None = None,
    changed_project_ids: list[str] | None = None,
    changed_financing_ids: list[str] | None = None,
    metrics: RunMetrics | None = None,
) -> RunResult:
    report_state = state or StateBundle()
    return RunResult(
        state=report_state,
        metrics=metrics
        or RunMetrics(
            started_at=WINDOW_END,
            finished_at=WINDOW_END,
            verified_count=5,
            pending_count=len(report_state.pending),
            deduplicated_count=2,
            raw_search_count=10,
            valid_shape_count=9,
            relevance_pass_count=7,
            recent_7d_count=5,
            final_candidate_count=5,
            information_available=True,
        ),
        trend_summary=TrendSummary(
            window_start=dt(4, 22),
            window_end=WINDOW_END,
            summary="采购项目由招标向中标阶段推进，商业航天融资保持活跃。",
            event_count=len(report_state.events) + len(report_state.financings),
            category_counts={
                Category.LASER_COMMUNICATION: 3,
                Category.LASER_WEAPON: 1,
                Category.COMMERCIAL_SPACE_FINANCING: 1,
            },
        ),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        rolling_start=ROLLING_START,
        changed_event_ids=changed_event_ids or [],
        changed_project_ids=changed_project_ids or [],
        changed_financing_ids=changed_financing_ids or [],
    )


@pytest.fixture
def run_result() -> RunResult:
    intention = event(
        "e-intention",
        "2026-04-22边界项目采购意向",
        dt(4, 22, 10),
        "https://official.example/intention",
        event_type=EventType.PROCUREMENT_INTENTION,
    )
    award = event(
        "e-award",
        "2026-04-22边界项目中标结果",
        dt(7, 21, 8),
        "https://official.example/award",
        event_type=EventType.AWARD,
        amount="1.2亿元",
    )
    open_tender = event(
        "e-open",
        "星间激光通信终端采购",
        dt(7, 21, 11),
        "https://official.example/open",
        event_type=EventType.TENDER,
        amount="800万元",
    )
    weapon_award = event(
        "e-weapon",
        "车载反无人机激光系统中标",
        dt(6, 12),
        "https://official.example/weapon-award",
        category=Category.LASER_WEAPON,
        event_type=EventType.AWARD,
        organization="某装备采购单位",
    )
    historical = event(
        "e-history",
        "2026-04-21历史项目",
        dt(4, 21),
        "https://official.example/history",
        event_type=EventType.AWARD,
    )
    projects = [
        project(
            "p-boundary",
            "2026-04-22边界项目",
            Category.LASER_COMMUNICATION,
            "awarded",
            ["e-award", "e-intention"],
            award.published_at,
            award.source_url,
            current_stage=EventType.AWARD,
            amount="1.2亿元",
        ),
        project(
            "p-open",
            "星间激光通信终端采购",
            Category.LASER_COMMUNICATION,
            "open",
            ["e-open"],
            open_tender.published_at,
            open_tender.source_url,
            current_stage=EventType.TENDER,
            amount="800万元",
            deadlines={"bid_submission": dt(7, 25, 17)},
        ),
        project(
            "p-weapon",
            "车载反无人机激光系统",
            Category.LASER_WEAPON,
            "awarded",
            ["e-weapon"],
            weapon_award.published_at,
            weapon_award.source_url,
            current_stage=EventType.AWARD,
            organization="某装备采购单位",
        ),
        project(
            "p-history",
            "2026-04-21历史项目",
            Category.LASER_COMMUNICATION,
            "awarded",
            ["e-history"],
            historical.published_at,
            historical.source_url,
            current_stage=EventType.AWARD,
        ),
    ]
    state = StateBundle(
        events=[weapon_award, historical, open_tender, award, intention],
        projects=list(reversed(projects)),
        financings=[financing()],
        pending=[
            PendingItem(
                item_id="suspected-1",
                title="疑似同项目公告",
                summary="搜索结果摘要，仅供人工复核。",
                reason="suspected_project_match",
                source_url="https://pending.example/item",
                discovered_at=WINDOW_END,
                category_hint=Category.LASER_COMMUNICATION,
                source_published_at=dt(7, 22, 8),
            )
        ],
    )
    return make_result(
        state=state,
        changed_event_ids=["e-open", "e-award"],
        changed_project_ids=["p-open", "p-boundary"],
        changed_financing_ids=["f-new"],
    )


@pytest.fixture
def snapshot_text() -> str:
    return (Path(__file__).parent / "snapshots" / "daily_report.md").read_text(
        encoding="utf-8"
    )


def test_report_matches_snapshot(run_result: RunResult, snapshot_text: str) -> None:
    report = ReportRenderer(max_chars=18000).render(run_result)

    assert report.markdown == snapshot_text
    headings = [
        "过去24小时新增/变化",
        "当前可报名及即将启动",
        "激光通信",
        "激光武器/反无人机",
        "光电转塔/吊舱",
        "商业航天融资",
        "今日重点跟进",
        "三个月趋势与数据完整性",
    ]
    positions = [report.markdown.index(f"## {heading}") for heading in headings]
    assert positions == sorted(positions)


def test_currently_open_section_requires_supported_nonexpired_deadline() -> None:
    rows = []
    projects = []
    for project_id, name, deadline in (
        ("future", "有未来截止时间", dt(7, 25, 17)),
        ("past", "已经截止项目", dt(7, 21, 8)),
        ("unknown", "截止时间未知项目", None),
    ):
        item = event(
            f"event-{project_id}",
            name,
            dt(7, 21, 7),
            f"https://official.example/{project_id}",
        )
        rows.append(item)
        projects.append(
            project(
                project_id,
                name,
                Category.LASER_COMMUNICATION,
                "open",
                [item.event_id],
                item.published_at,
                item.source_url,
                current_stage=EventType.TENDER,
                deadlines=(
                    {"bid_submission": deadline} if deadline is not None else None
                ),
            )
        )

    markdown = ReportRenderer(18000).render(
        make_result(state=StateBundle(events=rows, projects=projects))
    ).markdown
    open_section = markdown.split("## 当前可报名及即将启动", 1)[1].split(
        "## 激光通信", 1
    )[0]

    assert "有未来截止时间" in open_section
    assert "已经截止项目" not in open_section
    assert "截止时间未知项目" not in open_section


def test_stage_links_and_latest_link_are_original_urls(run_result: RunResult) -> None:
    text = ReportRenderer(18000).render(run_result).markdown

    assert "[采购意向](https://official.example/intention)" in text
    assert "[中标结果](https://official.example/award)" in text
    assert "[查看原始公告](https://official.example/award)" in text


def test_changed_projects_render_status_deadline_chain_without_event_duplicate(
    run_result: RunResult,
) -> None:
    daily = ReportRenderer(18000).render(run_result).markdown.split(
        "## 当前可报名及即将启动", maxsplit=1
    )[0]

    assert "状态：开放报名" in daily
    assert "截止：投标截止 2026-07-25 17:00" in daily
    assert "[查看原始公告](https://official.example/open)" in daily
    assert "公告链：[招标公告](https://official.example/open)（2026-07-21）" in daily
    assert daily.count("星间激光通信终端采购") == 1
    assert "｜招标公告｜星间激光通信终端采购｜" not in daily


def test_announcement_chain_is_sorted_by_date_then_event_id(
    run_result: RunResult,
) -> None:
    text = ReportRenderer(18000).render(run_result).markdown

    intention = text.index("[采购意向](https://official.example/intention)")
    award = text.index("[中标结果](https://official.example/award)")
    assert intention < award


def test_compression_never_drops_protected_sections() -> None:
    protected_event = event(
        "e-protected",
        "必须保留的24小时变化",
        dt(7, 22),
        "https://official.example/protected",
    )
    open_event = event(
        "e-open-protected",
        "必须保留的开放项目",
        dt(7, 20),
        "https://official.example/open-protected",
    )
    events = [protected_event, open_event]
    projects = [
        project(
            "p-open-protected",
            "必须保留的开放项目",
            Category.LASER_COMMUNICATION,
            "open",
            [open_event.event_id],
            open_event.published_at,
            open_event.source_url,
            current_stage=EventType.TENDER,
        )
    ]
    for index in range(12):
        item = event(
            f"e-completed-{index:02d}",
            f"超长已完结历史项目{index:02d}" + "详" * 180,
            dt(5, index + 1),
            f"https://official.example/completed/{index}",
            event_type=EventType.AWARD,
        )
        events.append(item)
        projects.append(
            project(
                f"p-completed-{index:02d}",
                item.title,
                Category.LASER_COMMUNICATION,
                "awarded",
                [item.event_id],
                item.published_at,
                item.source_url,
                current_stage=EventType.AWARD,
            )
        )
    result = make_result(
        state=StateBundle(events=events, projects=projects, financings=[financing()]),
        changed_event_ids=[protected_event.event_id],
        changed_financing_ids=["f-new"],
    )

    report = ReportRenderer(max_chars=3000).render(result)

    assert len(report.markdown) <= 3000
    assert "必须保留的24小时变化" in report.markdown
    assert "必须保留的开放项目" in report.markdown
    assert "星河动力" in report.markdown
    assert report.omitted_completed_projects > 0
    assert f"已压缩 {report.omitted_completed_projects} 个已完结历史项目" in report.markdown


def test_protected_overflow_raises() -> None:
    long_text = "必须完整保留" * 150
    protected_event = event(
        "e-protected",
        long_text,
        dt(7, 22),
        "https://official.example/protected",
    )
    open_event = event(
        "e-open",
        long_text,
        dt(7, 20),
        "https://official.example/open",
    )
    result = make_result(
        state=StateBundle(
            events=[protected_event, open_event],
            projects=[
                project(
                    "p-open",
                    long_text,
                    Category.LASER_COMMUNICATION,
                    "open",
                    [open_event.event_id],
                    open_event.published_at,
                    open_event.source_url,
                    current_stage=EventType.TENDER,
                )
            ],
            financings=[financing(company=long_text)],
        ),
        changed_event_ids=[protected_event.event_id],
        changed_financing_ids=["f-new"],
    )

    with pytest.raises(ReportTooLong):
        ReportRenderer(max_chars=500).render(result)


def test_compression_rejects_a_protected_only_project_candidate() -> None:
    open_event = event(
        "e-open-only",
        "仅受保护的开放项目",
        dt(7, 22),
        "https://official.example/open-only",
    )
    open_project = project(
        "p-open-only",
        open_event.title,
        Category.LASER_COMMUNICATION,
        "open",
        [open_event.event_id],
        open_event.published_at,
        open_event.source_url,
        current_stage=EventType.TENDER,
    )
    result = make_result(
        state=StateBundle(events=[open_event], projects=[open_project]),
        changed_project_ids=[open_project.project_id],
    )

    with pytest.raises(AssertionError, match="unprotected rolling section"):
        ReportRenderer(18000)._render_document(
            result,
            rolling_entries=(),
            compact_project_ids=frozenset({open_project.project_id}),
            removed_project_ids=frozenset(),
        )


def test_rolling_pool_uses_three_calendar_months(run_result: RunResult) -> None:
    text = ReportRenderer(18000).render(run_result).markdown

    assert "2026-04-22边界项目" in text
    assert "2026-04-21历史项目" not in text


def test_new_financing_appears_in_24h_and_financing_module(
    run_result: RunResult,
) -> None:
    text = ReportRenderer(18000).render(run_result).markdown
    daily, rest = text.split("## 当前可报名及即将启动", maxsplit=1)
    financing_section = rest.split("## 商业航天融资", maxsplit=1)[1].split(
        "## 今日重点跟进", maxsplit=1
    )[0]

    assert "星河动力" in daily
    assert "星河动力" in financing_section
    assert "[企业公告](https://company.example/financing)" in financing_section
    assert "[权威媒体报道](https://media.example/financing)" in financing_section


def test_old_late_discovered_financing_is_only_in_24h_module() -> None:
    late_discovery = financing(
        announced_at=dt(3, 1),
        source_published_at={
            "https://company.example/financing": dt(3, 1),
            "https://media.example/financing": dt(3, 2),
        },
    )
    text = ReportRenderer(18000).render(
        make_result(
            state=StateBundle(financings=[late_discovery]),
            changed_financing_ids=[late_discovery.financing_id],
        )
    ).markdown
    daily, rest = text.split("## 当前可报名及即将启动", maxsplit=1)
    financing_section = rest.split("## 商业航天融资", maxsplit=1)[1].split(
        "## 今日重点跟进", maxsplit=1
    )[0]

    assert "星河动力" in daily
    assert "星河动力" not in financing_section


def test_old_financing_uses_latest_verified_source_date_for_rolling_pool() -> None:
    item = financing(
        announced_at=dt(3, 1),
        source_published_at={
            "https://company.example/financing": dt(3, 1),
            "https://media.example/financing": dt(6, 1),
        },
    )
    text = ReportRenderer(18000).render(
        make_result(state=StateBundle(financings=[item]))
    ).markdown
    financing_section = text.split("## 商业航天融资", maxsplit=1)[1].split(
        "## 今日重点跟进", maxsplit=1
    )[0]

    assert "星河动力" in financing_section


def test_financing_source_labels_use_metadata_and_fall_back_to_source() -> None:
    urls = [
        "https://company.example/news",
        "https://investor.example/news",
        "https://media.example/news",
        "https://unknown.example/news",
    ]
    item = financing(
        source_urls=urls,
        evidence=[
            Evidence(field="企业公告", quote="企业公告", source_url=urls[0]),
            Evidence(field="投资方公告", quote="投资方公告", source_url=urls[1]),
            Evidence(field="权威媒体报道", quote="媒体报道", source_url=urls[2]),
        ],
    )
    text = ReportRenderer(18000).render(
        make_result(state=StateBundle(financings=[item]))
    ).markdown

    assert f"[企业公告]({urls[0]})" in text
    assert f"[投资方公告]({urls[1]})" in text
    assert f"[权威媒体报道]({urls[2]})" in text
    assert f"[来源]({urls[3]})" in text


def test_financing_source_label_ignores_unstructured_quote_text() -> None:
    url = "https://media.example/ambiguous"
    item = financing(
        source_urls=[url],
        evidence=[
            Evidence(
                field="title",
                quote="报道援引投资方公告，但本文不是投资方公告",
                source_url=url,
            )
        ],
    )
    text = ReportRenderer(18000).render(
        make_result(state=StateBundle(financings=[item]))
    ).markdown

    assert f"[来源]({url})" in text
    assert f"[投资方公告]({url})" not in text


def test_report_has_no_html_or_markdown_table(run_result: RunResult) -> None:
    text = ReportRenderer(18000).render(run_result).markdown

    assert re.search(r"<[^>]+>", text) is None
    assert not any(line.lstrip().startswith("|") for line in text.splitlines())
    assert "|---" not in text


def test_source_text_cannot_inject_html_or_markdown_structure() -> None:
    hostile = event(
        "e-hostile",
        "<script>alert</script>\n|---| [伪链接]",
        dt(7, 22),
        "https://official.example/hostile",
        organization="<style>body{display:none}</style>",
        amount="1|2",
    )
    text = ReportRenderer(18000).render(
        make_result(
            state=StateBundle(events=[hostile]),
            changed_event_ids=[hostile.event_id],
        )
    ).markdown

    assert "<script>" not in text
    assert "<style>" not in text
    assert "\n|---|" not in text
    assert "[伪链接]" not in text
    assert "&lt;script&gt;alert&lt;/script&gt;" in text
    assert r"\|---\| \[伪链接\]" in text


def test_trend_text_escapes_block_markers_without_creating_headings() -> None:
    result = make_result()
    hostile_summary = "# 伪标题\n> 引用\n+ 加号列表\n- 减号列表\n* 星号列表\n1. 有序列表"
    result = result.model_copy(
        update={
            "trend_summary": result.trend_summary.model_copy(
                update={"summary": hostile_summary}
            )
        }
    )

    text = ReportRenderer(18000).render(result).markdown
    heading_lines = [line for line in text.splitlines() if line.startswith("#")]

    assert len(heading_lines) == 9
    assert heading_lines[0].startswith("# 中国激光与商业航天情报日报")
    assert all(line.startswith("## ") for line in heading_lines[1:])
    assert (
        r"- \# 伪标题 &gt; 引用 \+ 加号列表 \- 减号列表 "
        r"\* 星号列表 1\. 有序列表"
    ) in text


def test_trend_text_escapes_tilde_fence_markers() -> None:
    result = make_result()
    result = result.model_copy(
        update={
            "trend_summary": result.trend_summary.model_copy(
                update={"summary": "~~~python\n伪代码\n~~~"}
            )
        }
    )

    text = ReportRenderer(18000).render(result).markdown

    assert "~~~" not in text
    assert r"- \~\~\~python 伪代码 \~\~\~" in text


def test_empty_sections_keep_all_headings_and_explicit_empty_notice() -> None:
    text = ReportRenderer(18000).render(make_result()).markdown

    assert text.count("- 暂无已核实信息") >= 6
    for heading in (
        "## 过去24小时新增/变化",
        "## 当前可报名及即将启动",
        "## 激光通信",
        "## 激光武器/反无人机",
        "## 光电转塔/吊舱",
        "## 商业航天融资",
    ):
        assert heading in text


def test_report_marks_information_shortage_and_renders_collection_funnel() -> None:
    metrics = RunMetrics(
        started_at=WINDOW_END,
        finished_at=WINDOW_END,
        raw_search_count=12,
        valid_shape_count=10,
        relevance_pass_count=4,
        recent_7d_count=3,
        fallback_8_30d_count=1,
        final_candidate_count=4,
        fetch_failure_count=2,
        information_available=False,
    )

    text = ReportRenderer(18000).render(make_result(metrics=metrics)).markdown

    assert "信息不足：最终候选 4 条，未达到 5 条验收门槛" in text
    assert "博查原始 12" in text
    assert "主题相关 4" in text
    assert "正文抓取失败 2" in text


def test_degraded_coverage_names_search_ai_and_failed_domains() -> None:
    metrics = RunMetrics(
        started_at=WINDOW_END,
        finished_at=WINDOW_END,
        search_coverage_degraded=True,
        model_coverage_degraded=True,
        search_failure_reasons=[
            "quota_or_rate_limit",
            "network_or_timeout",
            "quota_or_rate_limit",
        ],
        failed_domains=["broken.gov.cn", "timeout.example"],
    )
    text = ReportRenderer(18000).render(make_result(metrics=metrics)).markdown

    assert "覆盖：降级" in text
    assert "博查 API 配额不足或触发限流" in text
    assert "博查 API 网络连接或请求超时" in text
    assert text.count("博查 API 配额不足或触发限流") == 1
    assert "官方来源访问失败：broken.gov.cn、timeout.example" in text
    assert "AI" in text


def test_deterministic_ordering_ignores_state_input_order(run_result: RunResult) -> None:
    original = ReportRenderer(18000).render(run_result).markdown
    reversed_result = run_result.model_copy(
        update={
            "state": run_result.state.model_copy(
                update={
                    "events": list(reversed(run_result.state.events)),
                    "projects": list(reversed(run_result.state.projects)),
                    "financings": list(reversed(run_result.state.financings)),
                    "pending": list(reversed(run_result.state.pending)),
                }
            )
        }
    )

    assert ReportRenderer(18000).render(reversed_result).markdown == original


def test_late_discovery_shows_actual_publication_date(run_result: RunResult) -> None:
    daily = ReportRenderer(18000).render(run_result).markdown.split(
        "## 当前可报名及即将启动", maxsplit=1
    )[0]

    assert "2026-07-21｜激光通信｜2026-04-22边界项目｜" in daily
    assert "2026-07-22｜激光通信｜2026-04-22边界项目｜" not in daily


def test_naive_datetimes_are_interpreted_as_utc() -> None:
    naive_notice = event(
        "e-naive",
        "无时区公告",
        datetime(2026, 7, 21, 20),
        "https://official.example/naive",
    )
    text = ReportRenderer(18000).render(
        make_result(
            state=StateBundle(events=[naive_notice]),
            changed_event_ids=[naive_notice.event_id],
        )
    ).markdown

    assert "2026-07-22｜激光通信｜招标公告｜无时区公告" in text


@pytest.fixture
def rendered_report() -> RenderedReport:
    return RenderedReport(title="情报日报", markdown="# 情报日报\n\n- 已核验条目\n")


def test_dingtalk_requires_errcode_zero(respx_mock, rendered_report) -> None:
    respx_mock.post(url__startswith=WEBHOOK).respond(
        200,
        json={"errcode": 310000, "errmsg": "keywords not in content"},
    )

    with pytest.raises(NotificationError, match="310000") as caught:
        DingTalkNotifier(WEBHOOK, DINGTALK_SECRET).send(rendered_report)

    assert WEBHOOK not in str(caught.value)
    assert "test-token-never-log" not in str(caught.value)


def test_dingtalk_sends_one_markdown_payload(respx_mock, rendered_report) -> None:
    route = respx_mock.post(url__startswith=WEBHOOK).respond(
        200, json={"errcode": 0, "errmsg": "ok"}
    )

    DingTalkNotifier(WEBHOOK, DINGTALK_SECRET).send(rendered_report)

    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content) == {
        "msgtype": "markdown",
        "markdown": {
            "title": rendered_report.title,
            "text": rendered_report.markdown,
        },
    }


def test_dingtalk_adds_deterministic_hmac_signature_and_preserves_query(
    respx_mock, rendered_report
) -> None:
    webhook = f"{WEBHOOK}?access_token=token-value&channel=daily"
    route = respx_mock.post(url__startswith=WEBHOOK).respond(
        200, json={"errcode": 0, "errmsg": "ok"}
    )
    clock = lambda: 1_721_629_800.123

    DingTalkNotifier(
        webhook,
        DINGTALK_SECRET,
        clock=clock,
    ).send(rendered_report)

    query = parse_qs(urlsplit(str(route.calls[0].request.url)).query)
    timestamp = "1721629800123"
    digest = hmac.new(
        DINGTALK_SECRET.encode(),
        f"{timestamp}\n{DINGTALK_SECRET}".encode(),
        hashlib.sha256,
    ).digest()
    assert query == {
        "access_token": ["token-value"],
        "channel": ["daily"],
        "timestamp": [timestamp],
        "sign": [base64.b64encode(digest).decode()],
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"errmsg": "missing errcode"}),
        httpx.Response(200, json=[{"errcode": 0}]),
    ],
)
def test_dingtalk_rejects_malformed_response(
    respx_mock, rendered_report, response: httpx.Response
) -> None:
    respx_mock.post(url__startswith=WEBHOOK).mock(return_value=response)

    with pytest.raises(NotificationError, match="invalid response") as caught:
        DingTalkNotifier(WEBHOOK, DINGTALK_SECRET).send(rendered_report)

    assert "test-token-never-log" not in str(caught.value)


def test_dingtalk_converts_http_failure_without_url_or_payload(
    respx_mock, rendered_report
) -> None:
    respx_mock.post(url__startswith=WEBHOOK).respond(
        503, text=rendered_report.markdown
    )

    with pytest.raises(NotificationError, match="HTTP request failed") as caught:
        DingTalkNotifier(WEBHOOK, DINGTALK_SECRET).send(rendered_report)

    message = str(caught.value)
    assert "test-token-never-log" not in message
    assert rendered_report.markdown not in message


def test_dingtalk_converts_timeout_without_webhook(rendered_report) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout containing test-token-never-log", request=request)

    client = httpx.Client(transport=httpx.MockTransport(timeout))

    with pytest.raises(NotificationError, match="request failed") as caught:
        DingTalkNotifier(
            WEBHOOK,
            DINGTALK_SECRET,
            client=client,
        ).send(rendered_report)

    assert "test-token-never-log" not in str(caught.value)


class _CliPipeline:
    def __init__(self, result: RunResult) -> None:
        self.result = result
        self.error: BaseException | None = None
        self.now_values: list[datetime] = []

    def run(self, now: datetime) -> RunResult:
        self.now_values.append(now)
        if self.error is not None:
            raise self.error
        return self.result


class _CliRenderer:
    def __init__(self, report: RenderedReport) -> None:
        self.report = report
        self.error: BaseException | None = None

    def render(self, result: RunResult) -> RenderedReport:
        del result
        if self.error is not None:
            raise self.error
        return self.report


class _CliNotifier:
    def __init__(self) -> None:
        self.calls = 0
        self.error: BaseException | None = None

    def send(self, report: RenderedReport) -> None:
        del report
        self.calls += 1
        if self.error is not None:
            raise self.error


@pytest.fixture
def cli_deps(
    tmp_path: Path, run_result: RunResult, rendered_report: RenderedReport
) -> SimpleNamespace:
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    settings = Settings(
        deepseek_api_key="deepseek-secret-value",
        bocha_api_key="bocha-secret-value",
        dingtalk_webhook="https://example.invalid/?token=dingtalk-secret-value",
        dingtalk_secret="dingtalk-signing-secret-value",
        data_dir=tmp_path / "data",
        report_dir=tmp_path / "reports",
        official_sources_path=Path(__file__).parents[1] / "config" / "official_sources.yaml",
        financing_sources={
            "official_company_domains": {"company.example": "示例航天"},
            "independent_media_domains": ["media-one.example", "media-two.example"],
        },
    )
    pipeline = _CliPipeline(run_result)
    renderer = _CliRenderer(rendered_report)
    notifier = _CliNotifier()
    dependencies = CliDependencies(
        settings_loader=lambda path: settings,
        pipeline_factory=lambda loaded: pipeline,
        renderer_factory=lambda loaded: renderer,
        notifier_factory=lambda loaded: notifier,
    )
    return SimpleNamespace(
        config=config,
        settings=settings,
        pipeline=pipeline,
        renderer=renderer,
        notifier=notifier,
        dependencies=dependencies,
    )


def test_dry_run_writes_report_without_posting(cli_deps, tmp_path: Path) -> None:
    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--dry-run",
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    assert code == 0
    report_path = tmp_path / "reports" / "2026-07-22.md"
    assert report_path.read_text(encoding="utf-8") == cli_deps.renderer.report.markdown
    assert cli_deps.notifier.calls == 0


def test_local_run_lock_rejects_overlap_and_is_reusable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    with _LocalRunLock(data_dir):
        with pytest.raises(RunAlreadyActive):
            with _LocalRunLock(data_dir):
                pass

    with _LocalRunLock(data_dir):
        assert (data_dir / ".laser-space-daily.lock").exists()


def test_cli_does_not_start_pipeline_or_notify_when_local_lock_is_held(
    cli_deps,
) -> None:
    with _LocalRunLock(cli_deps.settings.data_dir):
        code = run_cli(
            ["--config", str(cli_deps.config)],
            dependencies=cli_deps.dependencies,
        )

    assert code == 4
    assert cli_deps.pipeline.now_values == []
    assert cli_deps.notifier.calls == 0


def test_push_failure_keeps_report_and_returns_three(cli_deps, tmp_path: Path) -> None:
    cli_deps.notifier.error = NotificationError("failed")

    code = run_cli(
        ["--config", str(cli_deps.config), "--now", "2026-07-22T07:30:00+08:00"],
        dependencies=cli_deps.dependencies,
    )

    assert code == 3
    assert (tmp_path / "reports" / "2026-07-22.md").exists()
    assert cli_deps.notifier.calls == 1


def test_pipeline_failure_returns_four_and_does_not_push(cli_deps, tmp_path: Path) -> None:
    cli_deps.pipeline.error = RuntimeError("pipeline exploded")

    code = run_cli(
        ["--config", str(cli_deps.config), "--now", "2026-07-22T07:30:00+08:00"],
        dependencies=cli_deps.dependencies,
    )

    assert code == 4
    assert cli_deps.notifier.calls == 0
    assert not (tmp_path / "reports").exists()


def test_config_failure_returns_two_without_secrets_in_output(
    cli_deps, caplog, capsys
) -> None:
    secret_values = (
        "deepseek-secret-value",
        "bocha-secret-value",
        "dingtalk-secret-value",
        "dingtalk-signing-secret-value",
    )
    dependencies = CliDependencies(
        settings_loader=lambda path: (_ for _ in ()).throw(
            ValueError("invalid " + " ".join(secret_values))
        ),
        pipeline_factory=cli_deps.dependencies.pipeline_factory,
        renderer_factory=cli_deps.dependencies.renderer_factory,
        notifier_factory=cli_deps.dependencies.notifier_factory,
    )

    with caplog.at_level(logging.ERROR):
        code = run_cli(["--config", str(cli_deps.config)], dependencies=dependencies)

    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    assert code == 2
    assert all(secret not in output for secret in secret_values)


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("2026-07-22T07:30:00", "2026-07-22T07:30:00+08:00"),
        ("2026-07-21T23:30:00+00:00", "2026-07-22T07:30:00+08:00"),
    ],
)
def test_cli_now_naive_and_aware_are_normalized_to_beijing(
    cli_deps, supplied: str, expected: str
) -> None:
    code = run_cli(
        ["--config", str(cli_deps.config), "--dry-run", "--now", supplied],
        dependencies=cli_deps.dependencies,
    )

    assert code == 0
    assert cli_deps.pipeline.now_values[-1].isoformat() == expected


def test_atomic_report_write_uses_same_directory_and_leaves_no_temp_file(
    cli_deps, monkeypatch, tmp_path: Path
) -> None:
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        target_path = Path(target)
        replacements.append((source_path, target_path))
        assert source_path.parent == target_path.parent
        real_replace(source_path, target_path)

    monkeypatch.setattr("laser_space_daily.cli.os.replace", recording_replace)

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--dry-run",
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    report_dir = tmp_path / "reports"
    assert code == 0
    assert len(replacements) == 1
    assert replacements[0][1] == report_dir / "2026-07-22.md"
    assert list(report_dir.glob("*.tmp")) == []


def test_atomic_report_write_failure_cleans_temp_and_returns_four(
    cli_deps, monkeypatch, tmp_path: Path
) -> None:
    def fail_replace(source, target) -> None:
        del source, target
        raise OSError("replace failed")

    monkeypatch.setattr("laser_space_daily.cli.os.replace", fail_replace)

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--dry-run",
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    report_dir = tmp_path / "reports"
    assert code == 4
    assert list(report_dir.glob("*.tmp")) == []
    assert not (report_dir / "2026-07-22.md").exists()


def test_atomic_report_write_normalizes_all_newlines_to_lf(cli_deps, tmp_path: Path) -> None:
    cli_deps.renderer.report = RenderedReport(
        title="newline report",
        markdown="# title\r\n\rbody\nend\r\n",
    )

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--dry-run",
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    raw = (tmp_path / "reports" / "2026-07-22.md").read_bytes()
    assert code == 0
    assert raw == b"# title\n\nbody\nend\n"


def test_report_too_long_returns_four_and_does_not_push(cli_deps) -> None:
    cli_deps.renderer.error = ReportTooLong("protected content")

    code = run_cli(
        ["--config", str(cli_deps.config), "--now", "2026-07-22T07:30:00+08:00"],
        dependencies=cli_deps.dependencies,
    )

    assert code == 4
    assert cli_deps.notifier.calls == 0


def test_cli_parser_exposes_exact_public_arguments() -> None:
    parser = _build_parser()
    actions = {action.dest: set(action.option_strings) for action in parser._actions}

    assert actions == {
        "help": {"-h", "--help"},
        "config": {"--config"},
        "dry_run": {"--dry-run"},
        "max_queries": {"--max-queries"},
        "now": {"--now"},
        "log_level": {"--log-level"},
    }


def test_cli_max_queries_overrides_loaded_settings(cli_deps) -> None:
    observed: list[int] = []

    def pipeline_factory(settings: Settings):
        observed.append(settings.discovery.max_queries)
        return cli_deps.pipeline

    dependencies = CliDependencies(
        settings_loader=cli_deps.dependencies.settings_loader,
        pipeline_factory=pipeline_factory,
        renderer_factory=cli_deps.dependencies.renderer_factory,
        notifier_factory=cli_deps.dependencies.notifier_factory,
    )

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--dry-run",
            "--max-queries",
            "4",
        ],
        dependencies=dependencies,
    )

    assert code == 0
    assert observed == [4]


@pytest.mark.parametrize("value", ["-1", "not-a-number"])
def test_cli_rejects_invalid_max_queries(cli_deps, value: str) -> None:
    assert (
        run_cli(
            ["--max-queries", value],
            dependencies=cli_deps.dependencies,
        )
        == 2
    )


def test_cli_rejects_unknown_argument_with_exit_two(cli_deps) -> None:
    assert run_cli(["--unknown"], dependencies=cli_deps.dependencies) == 2


@pytest.mark.parametrize("log_level", ["DEBUG", "INFO"])
@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [("success", 0), ("http_error", 3), ("timeout", 3)],
)
def test_real_notifier_never_logs_webhook_or_token_and_keeps_application_errors(
    cli_deps,
    respx_mock,
    caplog,
    monkeypatch,
    log_level: str,
    outcome: str,
    expected_code: int,
) -> None:
    settings = cli_deps.settings.model_copy(update={"dingtalk_webhook": WEBHOOK})
    route = respx_mock.post(url__startswith=WEBHOOK)
    if outcome == "success":
        route.respond(200, json={"errcode": 0})
    elif outcome == "http_error":
        route.respond(503, text="unavailable")
    else:
        route.mock(side_effect=httpx.ReadTimeout("timed out"))
    dependencies = CliDependencies(
        settings_loader=lambda path: settings,
        pipeline_factory=cli_deps.dependencies.pipeline_factory,
        renderer_factory=cli_deps.dependencies.renderer_factory,
    )
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.NOTSET)
    monkeypatch.setattr(logging.getLogger("httpcore"), "level", logging.NOTSET)

    with caplog.at_level(logging.DEBUG):
        code = run_cli(
            [
                "--config",
                str(cli_deps.config),
                "--log-level",
                log_level,
                "--now",
                "2026-07-22T07:30:00+08:00",
            ],
            dependencies=dependencies,
        )
        logging.getLogger("laser_space_daily.test").info("application-log-visible")

    assert code == expected_code
    assert WEBHOOK not in caplog.text
    assert "test-token-never-log" not in caplog.text
    assert "application-log-visible" in caplog.text
    stable_error = "cli_failure code=notification error=NotificationError"
    if expected_code == 3:
        assert stable_error in caplog.text
    else:
        assert stable_error not in caplog.text


def test_build_pipeline_uses_real_adapter_types_without_external_calls(tmp_path: Path) -> None:
    settings = Settings(
        deepseek_api_key="not-a-real-key",
        bocha_api_key="not-a-real-key",
        dingtalk_webhook="https://example.invalid/robot",
        dingtalk_secret="not-a-real-secret",
        data_dir=tmp_path / "data",
        report_dir=tmp_path / "reports",
        official_sources_path=Path(__file__).parents[1] / "config" / "official_sources.yaml",
        financing_sources={
            "official_company_domains": {"company.example": "示例航天"},
            "independent_media_domains": ["media-one.example", "media-two.example"],
        },
    )

    pipeline = build_pipeline(settings)

    assert isinstance(pipeline._repository, StateRepository)
    assert isinstance(pipeline._planner, QueryPlanner)
    assert isinstance(pipeline._search_provider, BochaProvider)
    assert isinstance(pipeline._official_collector, OfficialSeedCollector)
    assert isinstance(pipeline._fetcher, PageFetcher)
    assert isinstance(pipeline._analyzer, ResilientAnalyzer)
    assert isinstance(pipeline._analyzer._primary, DeepSeekAnalyzer)
    assert pipeline._trend_summarizer is pipeline._analyzer._primary
    assert isinstance(pipeline._verifier, RuleVerifier)
    assert isinstance(pipeline._matcher, ProjectMatcher)


def test_build_pipeline_never_downgrades_official_a_domain_to_optional_b(
    tmp_path: Path,
) -> None:
    settings = Settings(
        deepseek_api_key="not-a-real-key",
        bocha_api_key="not-a-real-key",
        dingtalk_webhook="https://example.invalid/robot",
        dingtalk_secret="not-a-real-secret",
        official_sources_path=Path(__file__).parents[1] / "config" / "official_sources.yaml",
        financing_sources={
            "official_company_domains": {"company.example": "示例航天"},
            "independent_media_domains": ["www.ccgp.gov.cn"],
        },
        data_dir=tmp_path / "data",
        report_dir=tmp_path / "reports",
    )

    pipeline = build_pipeline(settings)

    assert (
        pipeline._verifier._registry.grade("https://www.ccgp.gov.cn/cggg/item")
        is SourceGrade.A
    )
