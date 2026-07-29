from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from laser_space_daily.models import (
    AnalysisResult,
    Candidate,
    Category,
    EventType,
    PendingItem,
    SourceGrade,
    VerificationStatus,
)
from laser_space_daily.verification_followup import (
    FollowupTarget,
    VerificationFollowupPlanner,
)
from laser_space_daily.verifier import VerificationDecision


BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 29, 7, 30, tzinfo=BEIJING)


def target(
    *,
    url: str = "https://www.stcn.com/article/primary",
    grade: SourceGrade = SourceGrade.B,
    reason: str = "financing_requires_official_or_two_independent_b_sources",
    published_at: datetime | None = None,
    pending: PendingItem | None = None,
) -> FollowupTarget:
    published = published_at or NOW - timedelta(days=6)
    candidate = Candidate(
        title="龙擎空天完成Pre-A+轮融资",
        url=url,
        summary="龙擎空天完成近亿元Pre-A+轮融资，用于低轨卫星产品研发。",
        discovered_at=NOW,
        discovery_source="bocha",
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=published,
    )
    analysis = AnalysisResult(
        in_china=True,
        in_scope=True,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        title=candidate.title,
        organization="龙擎空天",
        published_at=published,
        amount="近亿元",
        amount_disclosed=True,
        financing_round="Pre-A+轮",
        financing_subtype="round_equity",
        investors=["九合创投", "同创伟业"],
        source_url=url,
    )
    decision = VerificationDecision(
        status=VerificationStatus.PENDING,
        reason=reason,
        source_grade=grade,
    )
    return FollowupTarget(
        candidate=candidate,
        analysis=analysis,
        decision=decision,
        pending=pending,
    )


def planner(**overrides) -> VerificationFollowupPlanner:
    values = {
        "financing_b_domains": ["stcn.com", "pedaily.cn", "cls.cn"],
        "elastic_budget": 3,
        "pool_days": 90,
        "max_targets": 1,
        "stop_after_no_new": 2,
    }
    values.update(overrides)
    return VerificationFollowupPlanner(**values)


def test_planner_spends_three_elastic_queries_on_top_financing_target():
    planned = planner().plan(NOW, [target()])

    assert len(planned) == 3
    assert all(item.target_url.endswith("/primary") for item in planned)
    assert "官网 投资机构 官方披露" in planned[0].query.text
    assert "九合创投 同创伟业 投资方" in planned[1].query.text
    assert "新闻 报道" in planned[2].query.text
    assert all("site:" not in item.query.text for item in planned)
    assert all(item.query.kind == "project_followup" for item in planned)


def test_planner_prioritizes_existing_b_source_over_newer_c_source():
    selected = planner(elastic_budget=1).plan(
        NOW,
        [
            target(
                url="https://media.example/newer",
                grade=SourceGrade.C,
                published_at=NOW - timedelta(days=1),
            ),
            target(
                url="https://www.stcn.com/article/older",
                grade=SourceGrade.B,
                published_at=NOW - timedelta(days=10),
            ),
        ],
    )

    assert len(selected) == 1
    assert selected[0].target_url.endswith("/older")


def test_planner_rejects_old_incomplete_and_non_source_gap_targets():
    old = target(published_at=NOW - timedelta(days=91))
    incomplete = target()
    incomplete.analysis.published_at = None
    incomplete.candidate.source_published_at = None
    wrong_reason = target(reason="missing_required_fields:published_at")

    assert planner().plan(NOW, [old, incomplete, wrong_reason]) == ()


def test_planner_stops_after_consecutive_no_new_threshold():
    existing = PendingItem(
        item_id="pending-1",
        title="龙擎空天完成Pre-A+轮融资",
        reason="financing_requires_official_or_two_independent_b_sources",
        source_url="https://www.stcn.com/article/primary",
        discovered_at=NOW - timedelta(days=2),
        source_published_at=NOW - timedelta(days=6),
        consecutive_no_new_sources=2,
    )

    assert planner().plan(NOW, [target(pending=existing)]) == ()


def test_planner_does_not_repeat_persisted_query():
    first_query = (
        "龙擎空天 Pre-A+轮 融资 官网 投资机构 官方披露 最近90天 中国境内"
    )
    existing = PendingItem(
        item_id="pending-1",
        title="龙擎空天完成Pre-A+轮融资",
        reason="financing_requires_official_or_two_independent_b_sources",
        source_url="https://www.stcn.com/article/primary",
        discovered_at=NOW - timedelta(days=2),
        source_published_at=NOW - timedelta(days=6),
        attempted_queries=[first_query],
    )

    planned = planner().plan(NOW, [target(pending=existing)])

    assert len(planned) == 2
    assert all(item.query.text != first_query for item in planned)
    assert "新闻 报道" in planned[-1].query.text


def test_planner_skips_target_b_domain_with_www_or_subdomain():
    for url in (
        "https://www.pedaily.cn/news/1",
        "https://news.stcn.com/article/1",
    ):
        planned = planner().plan(NOW, [target(url=url)])

        assert len(planned) == 3
        assert all("site:" not in item.query.text for item in planned)


def test_planner_uses_open_queries_for_non_b_target():
    planned = planner().plan(
        NOW,
        [target(url="https://finance.ifeng.com/article/1", grade=SourceGrade.C)],
    )

    assert len(planned) == 3
    assert all("site:" not in item.query.text for item in planned)


def test_planner_uses_investor_placeholders_when_investors_are_unknown():
    item = target()
    item.analysis.investors = []

    planned = planner().plan(NOW, [item])

    assert len(planned) == 3
    assert "领投方 投资方" in planned[1].query.text


def test_planner_does_not_depend_on_configured_b_domains():
    planned = planner(
        financing_b_domains=["stcn.com"],
        elastic_budget=3,
    ).plan(NOW, [target(url="https://www.stcn.com/article/1")])

    assert len(planned) == 3
    assert all("site:" not in item.query.text for item in planned)
