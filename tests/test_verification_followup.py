from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from laser_space_daily.models import (
    AnalysisResult,
    Candidate,
    Category,
    Evidence,
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
    assert "site:pedaily.cn" in planned[0].query.text
    assert "site:cls.cn" in planned[1].query.text
    assert "九合创投 同创伟业 投资方" in planned[2].query.text
    assert all(item.query.kind == "project_followup" for item in planned)


def test_planner_distributes_three_queries_across_two_events_before_revisit():
    first = target(url="https://www.stcn.com/article/first")
    second = target(
        url="https://media.example/second",
        grade=SourceGrade.C,
        published_at=NOW - timedelta(days=1),
    )
    second.analysis.organization = "谱星航天"
    second.analysis.title = "谱星航天完成Pre-A轮融资"
    second.analysis.financing_round = "Pre-A轮"

    planned = planner(max_targets=3).plan(NOW, [first, second])

    assert [item.target_url for item in planned] == [
        first.candidate.url,
        second.candidate.url,
        first.candidate.url,
    ]
    assert planned[1].allocation_reason == "cover_distinct_target"
    assert planned[2].allocation_reason == "retry_same_target"


def test_planner_covers_three_distinct_events_before_any_retry():
    first = target(url="https://www.stcn.com/article/first")
    second = target(url="https://media.example/second", grade=SourceGrade.C)
    second.analysis.organization = "谱星航天"
    second.analysis.title = "谱星航天完成Pre-A轮融资"
    second.analysis.financing_round = "Pre-A轮"
    third = target(url="https://media.example/third", grade=SourceGrade.C)
    third.analysis.organization = "微光启航"
    third.analysis.title = "微光启航完成天使++轮融资"
    third.analysis.financing_round = "天使++轮"

    planned = planner(max_targets=3).plan(NOW, [first, second, third])

    assert len(planned) == 3
    assert {item.target_url for item in planned} == {
        first.candidate.url,
        second.candidate.url,
        third.candidate.url,
    }
    assert len({item.target_key for item in planned}) == 3
    assert [item.allocation_reason for item in planned[1:]] == [
        "cover_distinct_target",
        "cover_distinct_target",
    ]


def test_full_and_short_company_names_share_stable_event_key():
    full = target(url="https://www.chinaventure.com.cn/news/guangyou")
    full.analysis.organization = "北京光邮星空科技有限公司"
    full.analysis.title = "光邮星空完成Pre-A轮融资"
    full.analysis.financing_round = "Pre-A和Pre-A+轮"
    short = target(
        url="https://m.pedaily.cn/news/guangyou",
        published_at=NOW - timedelta(days=2),
    )
    short.analysis.organization = "光邮星空"
    short.analysis.title = "光邮星空完成Pre-A轮融资"
    short.analysis.financing_round = "Pre-A轮"
    followup = planner(max_targets=3)

    full_key = followup.event_key(full, [short])
    short_key = followup.event_key(short, [full])
    planned = followup.plan(NOW, [full, short])

    assert full_key == short_key
    assert full_key.startswith("光邮星空|")
    assert len({item.target_key for item in planned}) == 1


def test_same_company_same_round_inside_30_days_is_one_event():
    first = target(
        url="https://www.stcn.com/article/pre-a",
        published_at=NOW - timedelta(days=20),
    )
    different_round = target(
        url="https://www.stcn.com/article/a",
        published_at=NOW - timedelta(days=20),
    )
    different_round.analysis.financing_round = "A轮"
    different_round.analysis.title = "龙擎空天完成A轮融资"
    distant_same_round = target(
        url="https://www.stcn.com/article/pre-a-later",
        published_at=NOW - timedelta(days=5),
    )

    round_plans = planner(
        elastic_budget=2,
        max_targets=2,
    ).plan(NOW, [first, different_round])
    date_plans = planner(
        elastic_budget=2,
        max_targets=2,
    ).plan(NOW, [first, distant_same_round])

    assert len({item.target_key for item in round_plans}) == 2
    assert len({item.target_key for item in date_plans}) == 1


def test_same_event_sources_do_not_crowd_out_a_second_event():
    first = target(url="https://www.stcn.com/article/first")
    first_copy = target(url="https://www.pedaily.cn/article/first-copy")
    first_copy.analysis.amount = None
    second = target(url="https://media.example/second", grade=SourceGrade.C)
    second.analysis.organization = "谱星航天"
    second.analysis.title = "谱星航天完成Pre-A轮融资"
    second.analysis.financing_round = "Pre-A轮"

    planned = planner(max_targets=2).plan(
        NOW,
        [first, first_copy, second],
    )

    assert planned[0].target_url in {
        first.candidate.url,
        first_copy.candidate.url,
    }
    assert planned[1].target_url == second.candidate.url


def test_planner_prioritizes_matching_official_investor_domain():
    official_ready = target(
        url="https://news.qq.com/article/light-post",
        grade=SourceGrade.C,
        published_at=NOW - timedelta(days=12),
    )
    official_ready.analysis.organization = "光邮星空"
    official_ready.analysis.investors = ["中关村科学城", "九合创投"]
    ordinary_b = target(
        url="https://www.stcn.com/article/ordinary",
        grade=SourceGrade.B,
        published_at=NOW - timedelta(days=1),
    )
    ordinary_b.analysis.organization = "谱星航天"

    planned = planner(
        max_targets=3,
        official_investor_domains={
            "zgccity.com": [
                "北京中关村科学城创新发展有限公司",
                "中关村科学城公司",
                "中关村科学城",
            ]
        },
    ).plan(NOW, [ordinary_b, official_ready])

    assert planned[0].target_url == official_ready.candidate.url
    assert "site:zgccity.com" in planned[0].query.text
    assert planned[0].preferred_domains == ("zgccity.com",)
    assert planned[0].allocation_reason == "official_source_match"


def test_candidate_summary_can_trigger_registered_official_investor_query():
    item = target(
        url="https://m.pedaily.cn/news/566658",
        grade=SourceGrade.B,
    )
    item.analysis.organization = "光邮星空"
    item.analysis.investors = []
    item.candidate.summary = (
        "北京光邮星空科技有限公司连续完成Pre-A和Pre-A+轮融资，"
        "九合创投领投，同创伟业、中关村科学城跟投。"
    )

    planned = planner(
        max_targets=3,
        official_investor_domains={
            "zgccity.com": [
                "北京中关村科学城创新发展有限公司",
                "中关村科学城公司",
                "中关村科学城",
            ]
        },
    ).plan(NOW, [item])

    assert "site:zgccity.com" in planned[0].query.text
    assert planned[0].preferred_domains == ("zgccity.com",)
    assert planned[0].matched_aliases == ("中关村科学城",)
    assert planned[0].clue_layers == ("candidate",)
    assert item.analysis.investors == []


def test_missing_financing_amount_evidence_triggers_official_gap_query():
    item = target(
        url="https://m.pedaily.cn/news/566658",
        grade=SourceGrade.B,
        reason="financing_missing_required_evidence",
    )
    item.analysis.organization = "光邮星空"
    item.analysis.amount = None
    item.analysis.amount_disclosed = None
    item.analysis.investors = []
    item.analysis.evidence = [
        Evidence(
            field="organization",
            quote="北京光邮星空科技有限公司",
            source_url=item.analysis.source_url,
        ),
        Evidence(
            field="published_at",
            quote="2026年7月23日",
            source_url=item.analysis.source_url,
        ),
        Evidence(
            field="financing_round",
            quote="Pre-A和Pre-A+轮融资",
            source_url=item.analysis.source_url,
        ),
    ]
    item.candidate.summary = (
        "北京光邮星空科技有限公司连续完成Pre-A和Pre-A+轮融资，"
        "九合创投领投，同创伟业、中关村科学城跟投。"
    )

    planned = planner(
        max_targets=3,
        official_investor_domains={
            "zgccity.com": ["中关村科学城"],
        },
    ).plan(NOW, [item])

    assert len(planned) == 3
    assert planned[0].missing_evidence_fields == ("amount",)
    assert "site:zgccity.com" in planned[0].query.text
    assert "融资金额" in planned[0].query.text
    assert "金额未披露" in planned[0].query.text
    assert item.analysis.amount is None
    assert item.analysis.amount_disclosed is None


def test_missing_page_publication_date_is_eligible_for_elastic_followup():
    item = target(reason="missing_required_fields:published_at")
    item.analysis.published_at = None
    candidate_date = item.candidate.source_published_at

    eligibility = planner().eligibility(NOW, item)
    planned = planner().plan(NOW, [item])

    assert eligibility.eligible is True
    assert eligibility.reason == "eligible"
    assert len(planned) == 3
    assert planned[0].missing_evidence_fields == ("published_at",)
    assert "发布日期" in planned[0].query.text
    assert "发布时间" in planned[0].query.text
    assert "公告时间" in planned[0].query.text
    assert "官方披露" in planned[0].query.text
    assert item.analysis.published_at is None
    assert item.candidate.source_published_at == candidate_date


def test_multiple_missing_required_fields_are_not_date_followup_eligible():
    item = target(
        reason="missing_required_fields:organization,published_at",
    )
    item.analysis.published_at = None
    item.analysis.organization = None

    eligibility = planner().eligibility(NOW, item)

    assert eligibility.eligible is False
    assert eligibility.reason == "reason_not_supported"
    assert planner().plan(NOW, [item]) == ()


def test_date_followup_requires_candidate_date_inside_pool():
    item = target(
        reason="missing_required_fields:published_at",
        published_at=NOW - timedelta(days=91),
    )
    item.analysis.published_at = None

    eligibility = planner().eligibility(NOW, item)

    assert eligibility.eligible is False
    assert eligibility.reason == "published_at_outside_pool"


def test_unregistered_candidate_name_does_not_trigger_site_query():
    item = target()
    item.analysis.investors = []
    item.candidate.summary = "某地方产业基金参与本轮融资。"

    planned = planner(
        official_investor_domains={
            "zgccity.com": ["中关村科学城"],
        },
    ).plan(NOW, [item])

    assert "site:pedaily.cn" in planned[0].query.text
    assert all(row.preferred_domains == () for row in planned)


def test_run_no_new_count_stops_target_at_threshold():
    item = target()
    target_key = planner().plan(NOW, [item])[0].target_key

    planned = planner().plan_next(
        NOW,
        [item],
        no_new_counts={target_key: 2},
    )

    assert planned is None


def test_run_no_new_target_transfers_slot_to_next_distinct_event():
    blocked = target(url="https://www.stcn.com/article/blocked")
    alternatives = []
    for index, organization in enumerate(("谱星航天", "微光启航", "星河动力"), 1):
        item = target(
            url=f"https://media.example/event-{index}",
            grade=SourceGrade.C,
            published_at=NOW - timedelta(days=index),
        )
        item.analysis.organization = organization
        item.analysis.title = f"{organization}完成Pre-A轮融资"
        item.analysis.financing_round = "Pre-A轮"
        alternatives.append(item)
    followup = planner(max_targets=3)
    blocked_key = followup.event_key(blocked)

    planned = followup.plan_next(
        NOW,
        [blocked, *alternatives],
        no_new_counts={blocked_key: 2},
    )

    assert planned is not None
    assert planned.target_url == alternatives[0].candidate.url


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
    wrong_reason = target(
        reason="missing_required_fields:organization,published_at"
    )

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

    assert len(planned) == 3
    assert all(item.query.text != first_query for item in planned)
    assert "site:pedaily.cn" in planned[0].query.text


def test_planner_skips_target_b_domain_with_www_or_subdomain():
    for url in (
        "https://www.pedaily.cn/news/1",
        "https://news.stcn.com/article/1",
    ):
        planned = planner().plan(NOW, [target(url=url)])

        assert len(planned) == 3
        current_domain = "pedaily.cn" if "pedaily.cn" in url else "stcn.com"
        assert all(f"site:{current_domain}" not in item.query.text for item in planned)
        assert any("site:" in item.query.text for item in planned)


def test_planner_uses_registered_b_queries_for_non_b_target():
    planned = planner().plan(
        NOW,
        [target(url="https://finance.ifeng.com/article/1", grade=SourceGrade.C)],
    )

    assert len(planned) == 3
    assert [item.query.text.split("site:", 1)[1].split()[0] for item in planned] == [
        "stcn.com",
        "pedaily.cn",
        "cls.cn",
    ]


def test_planner_uses_investor_placeholders_when_investors_are_unknown():
    item = target()
    item.analysis.investors = []

    planned = planner().plan(NOW, [item])

    assert len(planned) == 3
    assert "领投方 投资方" in planned[1].query.text


def test_planner_falls_back_to_open_queries_after_registered_b_domain_is_used():
    planned = planner(
        financing_b_domains=["stcn.com"],
        elastic_budget=3,
    ).plan(NOW, [target(url="https://www.stcn.com/article/1")])

    assert len(planned) == 3
    assert all("site:" not in item.query.text for item in planned)
