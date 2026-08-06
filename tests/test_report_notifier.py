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

import laser_space_daily.cli as cli_module
from laser_space_daily.analyzer import DeepSeekAnalyzer, ResilientAnalyzer
from laser_space_daily.cli import (
    CliDependencies,
    RunAlreadyActive,
    _LocalRunLock,
    _build_renderer,
    _build_parser,
    _mark_test_report,
    build_pipeline,
    run_cli,
)
from laser_space_daily.config import Settings
from laser_space_daily.discovery import BochaProvider, OfficialSeedCollector, QueryPlanner
from laser_space_daily.fetcher import PageFetcher
from laser_space_daily.matching import ProjectMatcher
from laser_space_daily.models import (
    AnalysisResult,
    Candidate,
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
from laser_space_daily.pipeline import CandidateDiagnostic, RunResult
from laser_space_daily.report import (
    DingTalkShortReportRenderer,
    RenderedReport,
    ReportRenderer,
    ReportTooLong,
    _actionable_deadline,
    _category_candidate_lines,
    _followup_lines,
    _pending_reason_text,
)
from laser_space_daily.repository import StateRepository
from laser_space_daily.verifier import RuleVerifier


BEIJING = ZoneInfo("Asia/Shanghai")
WINDOW_START = datetime(2026, 7, 21, 9, 30, tzinfo=BEIJING)
WINDOW_END = datetime(2026, 7, 22, 9, 30, tzinfo=BEIJING)


@pytest.mark.parametrize(
    ("reason", "label"),
    [
        ("classification_country_evidence_invalid", "境内主体证据不足"),
        ("classification_category_evidence_invalid", "目标业务证据不足"),
        ("classification_event_evidence_invalid", "事件动作证据不足"),
        ("classification_scope_evidence_invalid", "范围证据不足"),
        (
            "financing_requires_official_or_two_independent_b_sources",
            "缺少官方来源或第二个独立 B 级来源",
        ),
    ],
)
def test_pending_reason_renders_precise_classification_failure(reason, label):
    assert _pending_reason_text(reason) == label
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


def test_category_section_surfaces_selected_search_candidate_rejected_downstream():
    item = Candidate(
        title="星间激光通信终端采购公告",
        url="https://search.example.cn/laser-terminal",
        summary="某研究院发布空间激光通信终端采购信息",
        discovered_at=WINDOW_END,
        discovery_source="bocha",
        category_hint=Category.LASER_COMMUNICATION,
        source_published_at=dt(7, 21),
    )
    result = make_result(discovery_candidates=[item])

    candidate_lines = "\n".join(
        _category_candidate_lines(result, Category.LASER_COMMUNICATION)
    )
    followup = "\n".join(_followup_lines(result, {}))

    assert "候选线索（未核实）" in candidate_lines
    assert "2026-07-21" in candidate_lines
    assert item.title in candidate_lines
    assert item.summary in candidate_lines
    assert item.url in candidate_lines
    assert item.title not in followup


def test_high_confidence_pending_signal_renders_once_in_top_section():
    diagnostic = CandidateDiagnostic(
        source_url="https://www.stcn.com/article/guangyou",
        title="光邮星空完成Pre-A轮融资",
        summary="报道明确披露融资轮次和投资方。",
        discovery_source="search:bocha",
        selected_for_report=True,
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        organization="光邮星空",
        published_at=dt(7, 16),
        financing_round="Pre-A轮",
        evidence_count=6,
        stage="persisted",
        status="pending",
        reason="financing_requires_official_or_two_independent_b_sources",
        source_grade=SourceGrade.B,
        verification_event_key="光邮星空|financing|Pre-A",
    )
    result = make_result().model_copy(
        update={"candidate_diagnostics": [diagnostic]}
    )

    markdown = ReportRenderer().render(result).markdown
    top = markdown.split("## 今日最值得看", 1)[1].split(
        "## 过去24小时新增/变化", 1
    )[0]
    financing_section = markdown.split("## 商业航天融资", 1)[1].split(
        "## 今日重点跟进", 1
    )[0]

    assert "高可信待核实" in top
    assert "光邮星空完成Pre-A轮融资" in top
    assert "高可信待核实" not in financing_section
    assert markdown.count("光邮星空完成Pre-A轮融资") == 1


def test_verified_financing_suppresses_same_company_round_candidate_variants():
    verified = financing(
        company="微光启航",
        announced_at=dt(7, 7),
    ).model_copy(update={"round_name": "天使++轮"})
    diagnostic = CandidateDiagnostic(
        source_url="https://media.example/weiguang-third",
        title="微光启航完成亿元天使++轮融资",
        summary="北京微光启航科技有限公司完成本轮融资。",
        discovery_source="search:bocha",
        selected_for_report=True,
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        organization=None,
        published_at=dt(7, 7),
        financing_round=None,
        evidence_count=4,
        stage="persisted",
        status="pending",
        reason="missing_required_fields:organization",
        source_grade=SourceGrade.B,
    )
    candidate = Candidate(
        title="微光启航完成亿元级人民币天使++轮融资",
        url="https://search.example/weiguang-fourth",
        summary="该轮融资用于全碳纤维火箭工程研制。",
        discovered_at=WINDOW_END,
        discovery_source="bocha",
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=dt(7, 8),
    )
    result = make_result(
        state=StateBundle(financings=[verified]),
        changed_financing_ids=[verified.financing_id],
        discovery_candidates=[candidate],
    ).model_copy(update={"candidate_diagnostics": [diagnostic]})

    markdown = ReportRenderer().render(result).markdown

    assert "严格已核实" in markdown
    assert markdown.count("微光启航") == 1
    assert "weiguang-third" not in markdown
    assert "weiguang-fourth" not in markdown


def test_backfill_candidate_uses_90_day_time_label():
    item = Candidate(
        title="商业航天企业完成A轮融资",
        url="https://search.example.cn/financing",
        summary="卫星公司宣布完成商业航天股权融资",
        discovered_at=WINDOW_END,
        discovery_source="bocha",
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=dt(5, 20),
    )
    metrics = RunMetrics(
        started_at=WINDOW_END,
        fallback_window_days=90,
    )
    result = make_result(metrics=metrics, discovery_candidates=[item])

    candidate_lines = "\n".join(
        _category_candidate_lines(result, Category.COMMERCIAL_SPACE_FINANCING)
    )

    assert "8–90 天补充" in candidate_lines
    assert "时间范围外" not in candidate_lines


def test_report_exposes_agentic_budget_and_stop_reason():
    metrics = RunMetrics(
        started_at=WINDOW_END,
        search_budget=12,
        search_budget_used=9,
        agent_round_count=3,
        duplicate_query_count=2,
        event_filter_rejected_count=7,
        event_duplicate_count=1,
        agent_stop_reason="no_new_candidates",
    )

    markdown = ReportRenderer().render(make_result(metrics=metrics)).markdown

    assert "智能检索：预算 12；实际调用 9；模型轮次 3" in markdown
    assert "重复查询拦截 2" in markdown
    assert "事件过滤淘汰 7" in markdown
    assert "事件级合并 1" in markdown
    assert "停止原因 no\\_new\\_candidates" in markdown


def test_report_exposes_elastic_verification_budget_and_outcome():
    metrics = RunMetrics(
        started_at=WINDOW_END,
        search_budget=12,
        search_budget_used=15,
        discovery_channel_calls=4,
        verification_channel_calls=11,
        elastic_search_calls=3,
        verification_targets_count=1,
        verification_new_source_count=2,
        verification_duplicate_source_count=4,
        elastic_trigger_reasons=[
            "financing_requires_official_or_two_independent_b_sources"
        ],
    )

    markdown = ReportRenderer().render(make_result(metrics=metrics)).markdown

    assert (
        "智能检索：基础预算 12；基础调用 12；弹性调用 3；总调用 15"
        in markdown
    )
    assert "定向核验：处理事件 1；新增来源 2；重复来源 4" in markdown
    assert (
        "financing\\_requires\\_official\\_or\\_two\\_independent\\_b\\_sources"
        in markdown
    )


def test_test_label_marks_title_and_markdown():
    report = RenderedReport(
        title="# 中国激光与商业航天情报日报｜2026-07-28",
        markdown="# 中国激光与商业航天情报日报｜2026-07-28\n\n正文\n",
    )

    marked = _mark_test_report(report)

    assert marked.title.startswith("# 【测试】")
    assert marked.markdown.startswith("# 【测试】")
    assert marked.markdown.count("【测试】") == 1


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
    discovery_candidates: list[Candidate] | None = None,
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
        discovery_candidates=discovery_candidates or [],
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


def test_production_renderer_uses_separated_dingtalk_short_report(
    cli_deps,
) -> None:
    renderer = _build_renderer(cli_deps.settings)

    assert isinstance(renderer, DingTalkShortReportRenderer)


def test_short_report_separates_financing_before_procurement_and_keeps_links(
    run_result: RunResult,
) -> None:
    text = DingTalkShortReportRenderer().render(run_result).markdown
    financing = text.split("## 一、商业航天融资新闻", 1)[1].split(
        "## 二、招标采购情况", 1
    )[0]
    procurement = text.split("## 二、招标采购情况", 1)[1].split(
        "## 三、其他行业动态", 1
    )[0]

    assert text.index("## 一、商业航天融资新闻") < text.index(
        "## 二、招标采购情况"
    )
    assert "星河动力" in financing
    assert "星间激光通信终端采购" not in financing
    assert "星间激光通信终端采购" in procurement
    assert "星河动力" not in procurement
    assert "[企业公告](https://company.example/financing)" in financing
    assert "[权威媒体报道](https://media.example/financing)" in financing
    assert "[查看官方公告](https://official.example/open)" in procurement
    assert "融资统计：已核实" in financing
    assert "招标统计：已核实" in procurement
    assert 600 <= len(text) <= 1500


def test_short_report_compacts_items_before_raising_length_error(
    run_result: RunResult,
) -> None:
    compact = DingTalkShortReportRenderer(max_chars=1600).render(run_result)

    assert len(compact.markdown) <= 1600
    assert "## 一、商业航天融资新闻" in compact.markdown
    assert "## 二、招标采购情况" in compact.markdown
    with pytest.raises(ReportTooLong):
        DingTalkShortReportRenderer(max_chars=200).render(run_result)


def test_procurement_brief_uses_three_business_sections(
    run_result: RunResult,
) -> None:
    text = DingTalkShortReportRenderer().render(run_result).markdown
    procurement = text.split("## 二、招标采购情况", 1)[1].split(
        "## 三、其他行业动态", 1
    )[0]

    headings = (
        "### 🟢 可投标机会",
        "### 🟡 需核实确认",
        "### 🔵 结果与行业动态",
    )
    assert [procurement.index(heading) for heading in headings] == sorted(
        procurement.index(heading) for heading in headings
    )
    opportunity = procurement.split(headings[0], 1)[1].split(headings[1], 1)[0]
    confirmation = procurement.split(headings[1], 1)[1].split(headings[2], 1)[0]
    results = procurement.split(headings[2], 1)[1]
    assert "星间激光通信终端采购" in opportunity
    assert "投标截止：2026-07-25 17:00" in opportunity
    assert "剩余：3天" in opportunity
    assert "疑似同项目公告" in confirmation
    assert "待确认：" in confirmation
    assert "2026-04-22边界项目" in results
    assert "结果：已中标" in results
    assert "搜索结果摘要" not in procurement


def test_procurement_candidate_cleans_site_suffix_and_omits_raw_summary() -> None:
    diagnostic = CandidateDiagnostic(
        source_url="https://scbid.com/bx/detail/1",
        title=(
            "电子科技大学光电吊舱系统材料采购项目单一来源成交公告-"
            "四川招投标网-官网-四川省招投标公共服务平台"
        ),
        summary="发布日期:2026年07月30日 【字号 特大 大 中 小】【打印】【关闭】",
        discovery_source="search:bocha",
        selected_for_report=True,
        category_hint=Category.EO_TURRET,
        organization="电子科技大学",
        published_at=dt(7, 30),
        event_type=EventType.AWARD,
        awarded_supplier="某光电科技有限公司",
        awarded_amount="86万元",
        evidence_count=6,
        stage="persisted",
        status="pending",
        reason="tender_requires_grade_a",
        source_grade=SourceGrade.B,
    )
    result = make_result().model_copy(
        update={"candidate_diagnostics": [diagnostic]}
    )

    text = DingTalkShortReportRenderer().render(result).markdown

    assert "**电子科技大学光电吊舱系统材料采购项目单一来源成交公告**" in text
    assert "四川招投标网" not in text
    assert "字号" not in text
    assert "打印" not in text
    assert (
        "摘要：事项：电子科技大学光电吊舱系统材料采购项目；"
        "方式：单一来源；当前：成交结果。"
    ) in text
    assert "成交供应商：某光电科技有限公司" in text
    assert "中标/成交金额：86万元" in text
    assert "[查看结果公告](https://scbid.com/bx/detail/1)" in text


def test_verified_open_procurement_without_deadline_requires_confirmation() -> None:
    notice = event(
        "e-no-deadline",
        "无人机激光反制系统招标公告",
        dt(7, 22, 8),
        "https://official.example/no-deadline",
        category=Category.LASER_WEAPON,
        event_type=EventType.TENDER,
    )
    item = project(
        "p-no-deadline",
        "无人机激光反制系统",
        Category.LASER_WEAPON,
        "open",
        [notice.event_id],
        notice.published_at,
        notice.source_url,
        current_stage=EventType.TENDER,
    )
    result = make_result(
        state=StateBundle(events=[notice], projects=[item])
    )

    text = DingTalkShortReportRenderer().render(result).markdown
    opportunity = text.split("### 🟢 可投标机会", 1)[1].split(
        "### 🟡 需核实确认", 1
    )[0]
    confirmation = text.split("### 🟡 需核实确认", 1)[1].split(
        "### 🔵 结果与行业动态", 1
    )[0]

    assert "无人机激光反制系统" not in opportunity
    assert "无人机激光反制系统" in confirmation
    assert "待确认：投标截止时间" in confirmation


def test_short_report_marks_changed_old_financing_as_historical_backfill() -> None:
    item = financing(announced_at=dt(7, 7))
    result = make_result(
        state=StateBundle(financings=[item]),
        changed_financing_ids=[item.financing_id],
    )

    text = DingTalkShortReportRenderer().render(result).markdown

    assert "【已核实·历史补录】星河动力完成A轮" in text
    assert "历史补录 1 条" in text
    assert "过去24小时融资新增 0 条" in text
    assert "本轮新核实/历史补录" not in text


def test_short_report_keeps_financing_and_tender_candidates_in_separate_sections() -> None:
    financing_diagnostic = CandidateDiagnostic(
        source_url="https://www.stcn.com/article/guangyou",
        title="光邮星空完成Pre-A轮融资",
        summary=(
            "光邮星空宣布完成Pre-A轮融资，投资方包括中关村科学城、"
            "九合创投和同创伟业，具体金额未披露。"
        ),
        discovery_source="search:bocha",
        selected_for_report=True,
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        organization="光邮星空",
        published_at=dt(7, 16),
        financing_round="Pre-A轮",
        evidence_count=6,
        stage="persisted",
        status="pending",
        reason="financing_requires_official_or_two_independent_b_sources",
        source_grade=SourceGrade.B,
        verification_event_key="光邮星空|financing|Pre-A",
    )
    tender = Candidate(
        title="无人机激光反制设备采购成交结果",
        url="https://shanghai.jianyu360.cn/item",
        summary=(
            "本条项目信息由聚合站提供。采购单位为上海市公安局黄浦分局，"
            "候选中标方为上海纬稳科技。采购联系人 张三 采购电话 123456"
        ),
        discovered_at=WINDOW_END,
        discovery_source="bocha",
        category_hint=Category.LASER_WEAPON,
        source_published_at=dt(7, 3),
    )
    result = make_result(discovery_candidates=[tender]).model_copy(
        update={"candidate_diagnostics": [financing_diagnostic]}
    )

    text = DingTalkShortReportRenderer().render(result).markdown
    financing_section = text.split("## 一、商业航天融资新闻", 1)[1].split(
        "## 二、招标采购情况", 1
    )[0]
    tender_section = text.split("## 二、招标采购情况", 1)[1].split(
        "## 三、其他行业动态", 1
    )[0]

    assert "光邮星空完成Pre-A轮融资" in financing_section
    assert "无人机激光反制设备" not in financing_section
    assert "无人机激光反制设备" in tender_section
    assert "光邮星空" not in tender_section
    assert "[查看聚合线索](https://shanghai.jianyu360.cn/item)" in tender_section
    assert "采购联系人" not in text
    assert "采购电话" not in text


def test_short_report_merges_same_pending_financing_across_media_sources() -> None:
    first = CandidateDiagnostic(
        source_url="https://www.chinaventure.com.cn/guangyou",
        title="光邮星空连续完成Pre-A和Pre-A+轮融资",
        summary="光邮星空宣布连续完成Pre-A和Pre-A+轮融资。",
        discovery_source="search:bocha",
        selected_for_report=True,
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        organization="北京光邮星空科技有限公司",
        published_at=dt(7, 16),
        financing_round="Pre-A和Pre-A+轮",
        evidence_count=6,
        stage="persisted",
        status="pending",
        reason="financing_missing_required_evidence",
        source_grade=SourceGrade.B,
        verification_event_key="光邮星空|financing|prea,prea+|2026-07-16",
    )
    second = first.model_copy(
        update={
            "source_url": "https://m.pedaily.cn/news/guangyou",
            "title": "光邮星空连续完成Pre-A和Pre-A+轮融资，聚焦星地激光通信",
            "organization": "光邮星空",
            "published_at": dt(7, 2),
            "financing_round": "Pre-A+轮",
            "verification_event_key": (
                "光邮星空|financing|prea,prea+|2026-07-02"
            ),
        }
    )
    result = make_result().model_copy(
        update={"candidate_diagnostics": [first, second]}
    )

    text = DingTalkShortReportRenderer().render(result).markdown
    financing_section = text.split("## 一、商业航天融资新闻", 1)[1].split(
        "## 二、招标采购情况", 1
    )[0]

    assert financing_section.count("**【高可信待核实】") == 1
    assert "企业：北京光邮星空科技有限公司" in financing_section
    assert "[来源1](https://m.pedaily.cn/news/guangyou)" in financing_section
    assert (
        "[来源2](https://www.chinaventure.com.cn/guangyou)"
        in financing_section
    )
    assert "待核实线索 1 条" in text


def test_short_report_renders_same_verified_source_bundle_once() -> None:
    sources = [
        "https://m.pedaily.cn/news/566658",
        "https://www.chinaventure.com.cn/news/114-20260716-392303.html",
    ]
    first = financing(
        "guangyou-16",
        company="光邮星空",
        announced_at=dt(7, 16),
        source_urls=sources,
    ).model_copy(update={"round_name": "Pre-A+轮"})
    second = financing(
        "guangyou-21",
        company="光邮星空",
        announced_at=dt(7, 21),
        source_urls=sources,
    ).model_copy(update={"round_name": "Pre-A+轮"})
    result = make_result(
        state=StateBundle(financings=[first, second]),
        changed_financing_ids=[first.financing_id, second.financing_id],
    )

    text = DingTalkShortReportRenderer().render(result).markdown
    financing_section = text.split("## 一、商业航天融资新闻", 1)[1].split(
        "## 二、招标采购情况", 1
    )[0]

    assert financing_section.count("**【已核实·历史补录】光邮星空完成") == 1
    assert "融资统计：已核实 1 条" in financing_section


def test_short_report_merges_legal_name_and_combined_round_verified_duplicate() -> None:
    sources = [
        "https://m.pedaily.cn/news/566658",
        "https://www.chinaventure.com.cn/news/114-20260716-392303.html",
    ]
    first = financing(
        "guangyou-short",
        company="光邮星空",
        announced_at=dt(7, 21),
        source_urls=sources,
    ).model_copy(update={"round_name": "Pre-A+轮"})
    second = financing(
        "guangyou-legal",
        company="北京光邮星空科技有限公司",
        announced_at=dt(7, 16),
        source_urls=sources,
    ).model_copy(update={"round_name": "Pre-A和Pre-A+轮"})
    result = make_result(
        state=StateBundle(financings=[first, second]),
        changed_financing_ids=[first.financing_id, second.financing_id],
    )

    text = DingTalkShortReportRenderer().render(result).markdown
    financing_section = text.split("## 一、商业航天融资新闻", 1)[1].split(
        "## 二、招标采购情况", 1
    )[0]

    assert financing_section.count("**【已核实·历史补录】") == 1
    assert "融资统计：已核实 1 条" in financing_section


def test_short_report_hides_combined_round_candidate_covered_by_verified_subround() -> None:
    source = "https://www.chinaventure.com.cn/news/guangyou"
    verified = financing(
        "guangyou-verified",
        company="光邮星空",
        announced_at=dt(7, 16),
        source_urls=[source, "https://m.pedaily.cn/news/guangyou"],
    ).model_copy(update={"round_name": "Pre-A+轮"})
    candidate_row = Candidate(
        title="光邮星空连续完成Pre-A和Pre-A+轮融资",
        url=source,
        summary="报道同一融资事件。",
        discovered_at=WINDOW_END,
        discovery_source="search:bocha",
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=dt(7, 16),
    )
    result = make_result(
        state=StateBundle(financings=[verified]),
        changed_financing_ids=[verified.financing_id],
        discovery_candidates=[candidate_row],
    )

    text = DingTalkShortReportRenderer().render(result).markdown
    financing_section = text.split("## 一、商业航天融资新闻", 1)[1].split(
        "## 二、招标采购情况", 1
    )[0]

    assert financing_section.count("**【已核实·历史补录】光邮星空完成") == 1
    assert "候选线索 0 条" in financing_section


def test_short_report_hides_empty_followup_and_technical_diagnostics() -> None:
    metrics = RunMetrics(
        started_at=WINDOW_END,
        raw_search_count=84,
        final_candidate_count=4,
        search_budget=12,
        search_budget_used=15,
        elastic_search_calls=3,
        agent_stop_reason="budget_exhausted",
        elastic_trigger_reasons=["missing_required_fields:published_at"],
    )

    text = DingTalkShortReportRenderer().render(
        make_result(metrics=metrics)
    ).markdown

    assert "## 四、重点跟进" not in text
    assert "系统状态：检索 84 条，形成 4 条候选" in text
    assert "budget_exhausted" not in text
    assert "missing_required_fields" not in text
    assert "采集漏斗" not in text
    assert "模型轮次" not in text
    assert "失败域" not in text


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
    top = (
        ReportRenderer(18000)
        .render(run_result)
        .markdown.split("## 今日最值得看", maxsplit=1)[1]
        .split("## 过去24小时新增/变化", maxsplit=1)[0]
    )

    assert "状态：开放报名" in top
    assert "截止：投标截止 2026-07-25 17:00" in top
    assert "[查看原始公告](https://official.example/open)" in top
    assert "公告链：[招标公告](https://official.example/open)（2026-07-21）" in top
    assert top.count("星间激光通信终端采购") == 1
    assert "｜招标公告｜星间激光通信终端采购｜" not in top


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


def test_new_financing_appears_once_in_top_section(
    run_result: RunResult,
) -> None:
    text = ReportRenderer(18000).render(run_result).markdown
    top = text.split("## 今日最值得看", maxsplit=1)[1].split(
        "## 过去24小时新增/变化", maxsplit=1
    )[0]
    rest = text.split("## 当前可报名及即将启动", maxsplit=1)[1]
    financing_section = rest.split("## 商业航天融资", maxsplit=1)[1].split(
        "## 今日重点跟进", maxsplit=1
    )[0]

    assert "星河动力" in top
    assert "星河动力" not in financing_section
    assert text.count("星河动力") == 1
    assert "[企业公告](https://company.example/financing)" in top
    assert "[权威媒体报道](https://media.example/financing)" in top


def test_old_late_discovered_financing_is_only_in_top_module() -> None:
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
    top = text.split("## 今日最值得看", maxsplit=1)[1].split(
        "## 过去24小时新增/变化", maxsplit=1
    )[0]
    daily = text.split("## 过去24小时新增/变化", maxsplit=1)[1].split(
        "## 本轮新核实/历史补录", maxsplit=1
    )[0]
    rest = text.split("## 当前可报名及即将启动", maxsplit=1)[1]
    financing_section = rest.split("## 商业航天融资", maxsplit=1)[1].split(
        "## 今日重点跟进", maxsplit=1
    )[0]

    assert "星河动力" in top
    assert "星河动力" not in daily
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


def test_financing_omits_amount_when_source_does_not_disclose_or_deny_it() -> None:
    item = financing().model_copy(
        update={
            "amount_cny": None,
            "amount_disclosed": False,
            "investors": [],
            "business_area": None,
            "evidence": [
                Evidence(
                    field="financing_round",
                    quote="公司完成A轮融资",
                    source_url="https://company.example/financing",
                )
            ],
        }
    )

    text = ReportRenderer(18000).render(
        make_result(
            state=StateBundle(financings=[item]),
            changed_financing_ids=[item.financing_id],
        )
    ).markdown

    assert "金额：未披露" not in text
    assert "投资方：未披露" not in text
    assert "领域：未披露" not in text


def test_financing_renders_amount_undisclosed_only_with_explicit_evidence() -> None:
    item = financing().model_copy(
        update={
            "amount_cny": None,
            "amount_disclosed": False,
            "evidence": [
                Evidence(
                    field="amount",
                    quote="具体融资金额未披露",
                    source_url="https://company.example/financing",
                )
            ],
        }
    )

    text = ReportRenderer(18000).render(
        make_result(
            state=StateBundle(financings=[item]),
            changed_financing_ids=[item.financing_id],
        )
    ).markdown

    assert "金额：未披露" in text


def test_multiple_generic_financing_sources_are_numbered() -> None:
    urls = [
        "https://media-one.example/financing",
        "https://media-two.example/financing",
    ]
    item = financing(
        source_urls=urls,
        evidence=[
            Evidence(field="title", quote="融资报道一", source_url=urls[0]),
            Evidence(field="title", quote="融资报道二", source_url=urls[1]),
        ],
    )

    text = ReportRenderer(18000).render(
        make_result(
            state=StateBundle(financings=[item]),
            changed_financing_ids=[item.financing_id],
        )
    ).markdown

    assert f"[来源1]({urls[0]})" in text
    assert f"[来源2]({urls[1]})" in text


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

    assert len(heading_lines) == 11
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
        "## 本轮新核实/历史补录",
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
        self.reports: list[RenderedReport] = []
        self.error: BaseException | None = None

    def send(self, report: RenderedReport) -> None:
        self.calls += 1
        self.reports.append(report)
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
    cli_deps.pipeline.result = cli_deps.pipeline.result.model_copy(
        update={
            "candidate_diagnostics": [
                CandidateDiagnostic(
                    source_url="https://news.example/item",
                    title="微光启航完成融资",
                    discovery_source="search:bocha",
                    selected_for_report=True,
                    category_hint=Category.COMMERCIAL_SPACE_FINANCING,
                    stage="persisted",
                    status="pending",
                    reason="missing_required_fields:published_at",
                    source_grade=SourceGrade.B,
                    missing_fields=["published_at"],
                    elastic_eligible=True,
                )
            ]
        }
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

    assert code == 0
    report_path = tmp_path / "reports" / "2026-07-22.md"
    assert report_path.read_text(encoding="utf-8") == cli_deps.renderer.report.markdown
    diagnostics_path = tmp_path / "data" / "candidate-diagnostics.json"
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics == [
            {
                "source_url": "https://news.example/item",
                "title": "微光启航完成融资",
                "summary": "",
                "brief_summary": "",
                "discovery_source": "search:bocha",
                "selected_for_report": True,
                "category_hint": "commercial_space_financing",
                "organization": None,
                "published_at": None,
                "event_type": None,
                "amount": None,
                "awarded_supplier": None,
                "awarded_amount": None,
                "financing_round": None,
                "registration_deadline": None,
                "bid_submission_deadline": None,
                "opening_deadline": None,
                "deadline_precision": {},
                "deadline_evidence_fields": [],
                "evidence_count": 0,
            "stage": "persisted",
            "status": "pending",
            "reason": "missing_required_fields:published_at",
            "source_grade": "B",
            "missing_fields": ["published_at"],
            "elastic_eligible": True,
            "elastic_ineligible_reason": None,
            "elastic_attempted": False,
            "elastic_not_attempted_reason": None,
            "publication_date_source": None,
            "verification_event_key": None,
        }
    ]
    delivery = json.loads(
        (tmp_path / "data" / "delivery-status.json").read_text(encoding="utf-8")
    )
    assert delivery["status"] == "skipped"
    assert delivery["report_kind"] == "standard"
    assert cli_deps.notifier.calls == 0


def test_short_financing_fields_are_separate_and_ai_summary_is_preserved() -> None:
    item = financing(company="光邮星空", announced_at=dt(7, 16)).model_copy(
        update={
            "round_name": "Pre-A轮",
            "brief_summary": (
                "光邮星空完成Pre-A轮融资，资金将用于高速星地激光通信产品研发。"
            ),
        }
    )
    result = make_result(
        state=StateBundle(financings=[item]),
        changed_financing_ids=[item.financing_id],
    )

    text = DingTalkShortReportRenderer().render(result).markdown

    assert "  - 企业：光邮星空\n" in text
    assert "  - 时间：2026-07-16\n" in text
    assert "  - 轮次：Pre-A轮\n" in text
    assert "  - 金额：1.00亿元\n" in text
    assert "  - 摘要：光邮星空完成Pre-A轮融资" in text
    assert "完整采集与诊断见 GitHub Artifact" not in text


def test_short_report_never_uses_raw_search_summary_as_financing_summary() -> None:
    raw = "这是搜索引擎直接复制的原文第一段，不应进入钉钉短报。"
    brief = "光邮星空完成Pre-A轮融资，投资方与资金用途仍需进一步核实。"
    diagnostic = CandidateDiagnostic(
        source_url="https://www.stcn.com/article/guangyou",
        title="光邮星空完成Pre-A轮融资",
        summary=raw,
        brief_summary=brief,
        discovery_source="search:bocha",
        selected_for_report=True,
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        organization="光邮星空",
        published_at=dt(7, 16),
        financing_round="Pre-A轮",
        stage="persisted",
        status="pending",
        reason="missing_required_fields:amount",
        source_grade=SourceGrade.B,
    )
    result = make_result().model_copy(
        update={"candidate_diagnostics": [diagnostic]}
    )

    text = DingTalkShortReportRenderer().render(result).markdown

    assert "摘要：光邮星空完成Pre-A轮融资" in text
    assert raw not in text


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


def test_live_delivery_without_current_strict_item_still_notifies(
    cli_deps, tmp_path: Path
) -> None:
    cli_deps.pipeline.result = make_result()

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    delivery = json.loads(
        (tmp_path / "data" / "delivery-status.json").read_text(encoding="utf-8")
    )
    assert code == 0
    assert cli_deps.notifier.calls == 1
    assert not cli_deps.notifier.reports[0].title.startswith("【测试】")
    assert delivery["status"] == "accepted"
    assert delivery["report_kind"] == "standard"


def test_test_delivery_gate_blocks_run_without_current_strict_item(
    cli_deps, tmp_path: Path
) -> None:
    cli_deps.pipeline.result = make_result()

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--test-label",
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    delivery = json.loads(
        (tmp_path / "data" / "delivery-status.json").read_text(encoding="utf-8")
    )
    assert code == 3
    assert cli_deps.notifier.calls == 0
    assert delivery["status"] == "failed"
    assert delivery["error_type"] == "DeliveryGateError"


def test_test_delivery_gate_requires_sourced_category_content(
    cli_deps, tmp_path: Path
) -> None:
    cli_deps.renderer.report = RenderedReport(
        title="# 中国激光与商业航天情报日报｜2026-07-22",
        markdown=(
            "# 中国激光与商业航天情报日报｜2026-07-22\n\n"
            "## 一、商业航天融资新闻\n- 暂无可展示信息\n\n"
            "## 二、招标采购情况\n- 暂无可展示信息\n"
        ),
    )

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--test-label",
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    delivery = json.loads(
        (tmp_path / "data" / "delivery-status.json").read_text(encoding="utf-8")
    )
    assert code == 3
    assert cli_deps.notifier.calls == 0
    assert delivery["status"] == "failed"
    assert delivery["error_type"] == "DeliveryGateError"


def test_test_delivery_gate_allows_qualified_report(cli_deps, tmp_path: Path) -> None:
    cli_deps.renderer.report = DingTalkShortReportRenderer().render(
        cli_deps.pipeline.result
    )

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--test-label",
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    delivery = json.loads(
        (tmp_path / "data" / "delivery-status.json").read_text(encoding="utf-8")
    )
    assert code == 0
    assert cli_deps.notifier.calls == 1
    assert cli_deps.notifier.reports[0].title.startswith("# 【测试】")
    assert delivery["status"] == "accepted"


def test_pipeline_failure_returns_four_and_sends_anomaly_alert(
    cli_deps, tmp_path: Path
) -> None:
    cli_deps.pipeline.error = RuntimeError("pipeline exploded")

    code = run_cli(
        ["--config", str(cli_deps.config), "--now", "2026-07-22T07:30:00+08:00"],
        dependencies=cli_deps.dependencies,
    )

    assert code == 4
    assert cli_deps.notifier.calls == 1
    assert (tmp_path / "reports" / "2026-07-22-degraded.md").exists()
    manifest = json.loads(
        (tmp_path / "data" / "run-result.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "degraded"
    assert manifest["failure_stage"] == "pipeline_run"


def test_failure_after_checkpoint_sends_test_labeled_degraded_intelligence(
    cli_deps, tmp_path: Path
) -> None:
    class FailingWithCheckpoint:
        def run(self, now):
            cli_deps.settings.data_dir.mkdir(parents=True, exist_ok=True)
            (cli_deps.settings.data_dir / "candidate-checkpoint.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "analyzed",
                        "occurred_at": now.isoformat(),
                        "candidates": [
                            {
                                "title": "光邮星空完成Pre-A轮融资",
                                "summary": "公司披露融资轮次和投资方。",
                                "source_url": "https://www.stcn.com/article/1",
                                "category": "commercial_space_financing",
                                "organization": "光邮星空",
                                "published_at": "2026-07-16T00:00:00+08:00",
                                "amount": None,
                                "financing_round": "Pre-A轮",
                                "in_china": True,
                                "in_scope": True,
                                "evidence_count": 6,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            raise RuntimeError("failure after checkpoint")

    dependencies = CliDependencies(
        settings_loader=cli_deps.dependencies.settings_loader,
        pipeline_factory=lambda loaded: FailingWithCheckpoint(),
        renderer_factory=cli_deps.dependencies.renderer_factory,
        notifier_factory=cli_deps.dependencies.notifier_factory,
    )

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--test-label",
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=dependencies,
    )

    assert code == 4
    assert cli_deps.notifier.calls == 1
    report = cli_deps.notifier.reports[0]
    assert report.title.startswith("# 【测试】【降级】")
    assert "光邮星空完成Pre-A轮融资" in report.markdown
    assert "均未通过最终严格核验" in report.markdown
    delivery = json.loads(
        (tmp_path / "data" / "delivery-status.json").read_text(encoding="utf-8")
    )
    assert delivery["status"] == "accepted"
    assert delivery["report_kind"] == "degraded"


def _failing_cli_dependencies(cli_deps, factory) -> CliDependencies:
    return CliDependencies(
        settings_loader=cli_deps.dependencies.settings_loader,
        pipeline_factory=factory,
        renderer_factory=cli_deps.dependencies.renderer_factory,
        notifier_factory=cli_deps.dependencies.notifier_factory,
    )


@pytest.mark.parametrize(
    ("failure_point", "expected_stage", "expected_code"),
    [
        ("pipeline_build", "pipeline_build", 4),
        ("pipeline_run", "pipeline_run", 4),
        ("report_render", "report_render", 4),
        ("report_write", "report_write", 4),
        ("diagnostics_write", "diagnostics_write", 4),
        ("notification", "notification", 3),
    ],
)
def test_cli_failure_diagnostic_records_stable_stage(
    cli_deps,
    monkeypatch,
    failure_point: str,
    expected_stage: str,
    expected_code: int,
) -> None:
    error = TypeError(
        "https://example.invalid/?access_token=secret-query-token"
    )
    dependencies = cli_deps.dependencies
    arguments = [
        "--config",
        str(cli_deps.config),
        "--now",
        "2026-07-22T07:30:00+08:00",
    ]

    if failure_point == "pipeline_build":
        def failing_factory(settings):
            del settings
            raise error

        dependencies = _failing_cli_dependencies(cli_deps, failing_factory)
    elif failure_point == "pipeline_run":
        cli_deps.pipeline.error = error
    elif failure_point == "report_render":
        cli_deps.renderer.error = error
    elif failure_point == "report_write":
        def failing_report_write(path, report):
            del path, report
            raise error

        monkeypatch.setattr(
            cli_module,
            "_atomic_write_report",
            failing_report_write,
        )
    elif failure_point == "diagnostics_write":
        cli_deps.pipeline.result = cli_deps.pipeline.result.model_copy(
            update={"research_trace": [{"round_index": 1}]}
        )

        def failing_trace_write(path, trace):
            del path, trace
            raise error

        monkeypatch.setattr(
            cli_module,
            "_atomic_write_research_trace",
            failing_trace_write,
        )
    else:
        cli_deps.notifier.error = error

    code = run_cli(arguments, dependencies=dependencies)

    diagnostic_path = (
        cli_deps.settings.data_dir / "failure-diagnostics.json"
    )
    payload = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert code == expected_code
    assert payload["schema_version"] == 1
    assert payload["status"] == "failure"
    assert payload["stage"] == expected_stage
    assert payload["error_type"] == "TypeError"
    assert payload["occurred_at"] == "2026-07-22T07:30:00+08:00"
    assert payload["frames"]
    assert all(
        set(frame) == {"path", "line", "function"}
        for frame in payload["frames"]
    )
    assert all(
        not Path(str(frame["path"])).is_absolute()
        for frame in payload["frames"]
    )


def test_cli_failure_diagnostic_and_log_exclude_exception_secrets(
    cli_deps,
    caplog,
) -> None:
    cli_deps.pipeline.error = TypeError(
        "https://example.invalid/?access_token=secret-query-token"
    )

    with caplog.at_level(logging.ERROR):
        code = run_cli(
            [
                "--config",
                str(cli_deps.config),
                "--now",
                "2026-07-22T07:30:00+08:00",
            ],
            dependencies=cli_deps.dependencies,
        )

    diagnostic_path = (
        cli_deps.settings.data_dir / "failure-diagnostics.json"
    )
    serialized = (
        diagnostic_path.read_text(encoding="utf-8") + caplog.text
    )
    assert code == 4
    assert "pipeline_run" in caplog.text
    assert "secret-query-token" not in serialized
    assert "access_token" not in serialized


def test_cli_diagnostic_write_failure_preserves_primary_exit_code(
    cli_deps,
    monkeypatch,
    caplog,
) -> None:
    cli_deps.pipeline.error = TypeError("primary-secret")

    def failing_json_write(path, payload):
        del path, payload
        raise OSError("diagnostic-secret")

    monkeypatch.setattr(cli_module, "_atomic_write_json", failing_json_write)

    with caplog.at_level(logging.ERROR):
        code = run_cli(
            [
                "--config",
                str(cli_deps.config),
                "--now",
                "2026-07-22T07:30:00+08:00",
            ],
            dependencies=cli_deps.dependencies,
        )

    assert code == 4
    assert "diagnostic_write_failed error=OSError" in caplog.text
    assert "primary-secret" not in caplog.text
    assert "diagnostic-secret" not in caplog.text


def test_successful_dry_run_replaces_stale_metadata_with_run_result(
    cli_deps,
) -> None:
    data_dir = cli_deps.settings.data_dir
    data_dir.mkdir(parents=True)
    (data_dir / "failure-diagnostics.json").write_text(
        '{"status":"stale"}\n',
        encoding="utf-8",
    )
    (data_dir / "run-result.json").write_text(
        '{"status":"stale"}\n',
        encoding="utf-8",
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

    assert code == 0
    assert not (data_dir / "failure-diagnostics.json").exists()
    payload = json.loads(
        (data_dir / "run-result.json").read_text(encoding="utf-8")
    )
    assert payload == {
        "schema_version": 1,
        "status": "success",
        "occurred_at": "2026-07-22T07:30:00+08:00",
        "report_path": "reports/2026-07-22.md",
    }


def test_failed_run_replaces_stale_success_manifest_with_degraded_result(
    cli_deps,
) -> None:
    data_dir = cli_deps.settings.data_dir
    data_dir.mkdir(parents=True)
    (data_dir / "run-result.json").write_text(
        '{"status":"stale"}\n',
        encoding="utf-8",
    )
    cli_deps.pipeline.error = TypeError("failure")

    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--now",
            "2026-07-22T07:30:00+08:00",
        ],
        dependencies=cli_deps.dependencies,
    )

    assert code == 4
    payload = json.loads(
        (data_dir / "run-result.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "degraded"
    assert payload["failure_stage"] == "pipeline_run"
    assert (data_dir / "failure-diagnostics.json").exists()


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
    data_dir = tmp_path / "data"
    assert code == 0
    assert {target for _source, target in replacements} == {
        report_dir / "2026-07-22.md",
        data_dir / "candidate-diagnostics.json",
        data_dir / "run-result.json",
        data_dir / "delivery-status.json",
    }
    assert list(report_dir.glob("*.tmp")) == []
    assert list(data_dir.glob("*.tmp")) == []


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


def test_report_too_long_returns_four_and_sends_anomaly_alert(cli_deps) -> None:
    cli_deps.renderer.error = ReportTooLong("protected content")

    code = run_cli(
        ["--config", str(cli_deps.config), "--now", "2026-07-22T07:30:00+08:00"],
        dependencies=cli_deps.dependencies,
    )

    assert code == 4
    assert cli_deps.notifier.calls == 1


def test_cli_parser_exposes_exact_public_arguments() -> None:
    parser = _build_parser()
    actions = {action.dest: set(action.option_strings) for action in parser._actions}

    assert actions == {
        "help": {"-h", "--help"},
        "config": {"--config"},
        "dry_run": {"--dry-run"},
        "test_label": {"--test-label"},
        "discovery_mode": {"--discovery-mode"},
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


def test_cli_backfill_mode_allows_40_query_budget(cli_deps) -> None:
    observed: list[tuple[str, int]] = []

    def pipeline_factory(settings: Settings):
        observed.append(
            (settings.discovery.mode, settings.discovery.max_queries)
        )
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
            "--discovery-mode",
            "backfill",
            "--max-queries",
            "40",
        ],
        dependencies=dependencies,
    )

    assert code == 0
    assert observed == [("backfill", 40)]


def test_cli_daily_mode_rejects_budget_over_20(cli_deps) -> None:
    code = run_cli(
        [
            "--config",
            str(cli_deps.config),
            "--dry-run",
            "--discovery-mode",
            "daily",
            "--max-queries",
            "21",
        ],
        dependencies=cli_deps.dependencies,
    )

    assert code == 2


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
    assert pipeline._analyzer._primary._max_attempts == 1
    assert pipeline._analyzer._primary._client.max_retries == 0
    assert pipeline._analyzer._primary._client.timeout == 20
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
