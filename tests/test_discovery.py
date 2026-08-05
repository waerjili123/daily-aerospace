from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml

from laser_space_daily.discovery import (
    DiscoveryConfigurationError,
    DiscoveryQuotaError,
    DiscoveryUnavailableError,
    BochaProvider,
    OfficialSeed,
    OfficialSeedCollector,
    QueryPlanner,
    SearchQuery,
    dedupe_candidates,
    normalize_url,
    select_search_candidates,
)
from laser_space_daily.models import Candidate, Category, Project, SourceGrade


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 7, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def project_factory():
    def create() -> Project:
        return Project(
            project_id="P-001",
            name="天基激光通信终端",
            organization="某研究院",
            category=Category.LASER_COMMUNICATION,
            status="active",
        )

    return create


@pytest.fixture
def bocha_payload() -> dict[str, object]:
    fixture_path = Path(__file__).parent / "fixtures" / "bocha_search.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


@pytest.fixture
def official_html() -> str:
    return '<ul class="notices"><li><a href="/notice/1">采购公告一</a></li></ul>'


SEEDS = [
    OfficialSeed(
        name="bad",
        domain="bad.gov.cn",
        grade=SourceGrade.A,
        list_urls=["https://bad.gov.cn/list"],
        link_selector="ul.notices a[href]",
    ),
    OfficialSeed(
        name="good",
        domain="good.gov.cn",
        grade=SourceGrade.A,
        list_urls=["https://good.gov.cn/list"],
        link_selector="ul.notices a[href]",
    ),
]


def test_planner_covers_incremental_backfill_and_overdue(project_factory, fixed_now):
    project = project_factory()
    project.deadlines = {
        "bid_submission": datetime(
            2026, 7, 21, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
    }
    project.deadline_precision = {"bid_submission": "minute"}
    project.deadline_evidence = {"bid_submission": "投标截止：2026-07-21 17:00"}
    queries = QueryPlanner(max_queries=40).plan(fixed_now, [project])

    kinds = {query.kind for query in queries}
    assert kinds == {
        "incremental",
        "procurement_open",
        "procurement_result",
        "project_followup",
        "rolling_recheck",
        "overdue_result",
    }
    assert any("激光通信" in query.text for query in queries)
    assert any("激光反无人机" in query.text for query in queries)
    assert any("光电转塔" in query.text for query in queries)
    assert any("商业航天 融资" in query.text for query in queries)


def test_planner_caps_queries_in_stable_priority_order(project_factory, fixed_now):
    queries = QueryPlanner(max_queries=5).plan(fixed_now, [project_factory(), project_factory()])

    assert len(queries) == 5
    assert [query.kind for query in queries[:4]] == [
        "procurement_open",
        "procurement_open",
        "procurement_open",
        "incremental",
    ]
    assert [query.category for query in queries[:4]] == [
        Category.LASER_COMMUNICATION,
        Category.LASER_WEAPON,
        Category.EO_TURRET,
        Category.COMMERCIAL_SPACE_FINANCING,
    ]
    assert "采购" not in queries[3].text
    assert "招标" not in queries[3].text
    assert queries[4].kind == "procurement_open"


def test_planner_reserves_twenty_daily_queries_by_business_stage(
    project_factory, fixed_now
):
    project = project_factory()
    project.deadlines = {
        "bid_submission": datetime(
            2026, 7, 21, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
    }
    project.deadline_precision = {"bid_submission": "minute"}
    project.deadline_evidence = {
        "bid_submission": "投标截止：2026-07-21 17:00"
    }
    queries = QueryPlanner(
        max_queries=20,
        financing_domains=(
            "stcn.com",
            "pedaily.cn",
            "chinaventure.com.cn",
            "cls.cn",
        ),
    ).plan(fixed_now, [project])

    assert len(queries) == 20
    assert sum(query.kind == "procurement_open" for query in queries) == 6
    assert sum(query.kind == "procurement_result" for query in queries) == 6
    assert sum(
        query.category is Category.COMMERCIAL_SPACE_FINANCING
        for query in queries
    ) == 5
    assert sum(
        query.kind in {"project_followup", "rolling_recheck", "overdue_result"}
        for query in queries
    ) == 3


def test_planner_scopes_every_query_to_china_and_excludes_ai_news(
    project_factory, fixed_now
):
    project = project_factory()
    project.deadlines = {
        "bid_submission": datetime(
            2026, 7, 21, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
    }
    project.deadline_precision = {"bid_submission": "minute"}
    project.deadline_evidence = {"bid_submission": "投标截止：2026-07-21 17:00"}
    queries = QueryPlanner(max_queries=40).plan(fixed_now, [project])

    assert {query.kind for query in queries} == {
        "incremental",
        "procurement_open",
        "procurement_result",
        "project_followup",
        "rolling_recheck",
        "overdue_result",
    }
    for query in queries:
        assert query.text.count("中国 境内 -人工智能新闻 -AI新闻") == 1


@pytest.mark.parametrize(
    "deadline",
    [
        None,
        datetime(2026, 7, 23, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ],
)
def test_planner_only_marks_supported_past_deadlines_overdue(
    project_factory, fixed_now, deadline
):
    project = project_factory()
    if deadline is not None:
        project.deadlines = {"bid_submission": deadline}

    queries = QueryPlanner(max_queries=40).plan(fixed_now, [project])

    assert "overdue_result" not in {query.kind for query in queries}


def test_planner_keeps_date_only_deadline_open_for_entire_local_day(
    project_factory,
):
    project = project_factory()
    deadline = datetime(2026, 7, 22, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    project.deadlines = {"bid_submission": deadline}
    project.deadline_precision = {"bid_submission": "date"}
    project.deadline_evidence = {"bid_submission": "投标截止日期：2026-07-22"}

    same_day = QueryPlanner(max_queries=40).plan(
        datetime(2026, 7, 22, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")), [project]
    )
    next_day = QueryPlanner(max_queries=40).plan(
        datetime(2026, 7, 23, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")), [project]
    )

    assert "overdue_result" not in {query.kind for query in same_day}
    assert "overdue_result" in {query.kind for query in next_day}


def test_planner_exact_deadline_expires_at_its_instant(project_factory):
    project = project_factory()
    deadline = datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    project.deadlines = {"bid_submission": deadline}
    project.deadline_precision = {"bid_submission": "minute"}
    project.deadline_evidence = {"bid_submission": "投标截止：2026-07-22 10:00"}

    queries = QueryPlanner(max_queries=40).plan(
        datetime(2026, 7, 22, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai")), [project]
    )

    assert "overdue_result" in {query.kind for query in queries}


def test_bocha_maps_web_result_and_keeps_source_url(respx_mock, bocha_payload):
    respx_mock.post("https://api.bochaai.com/v1/web-search").respond(
        200, json=bocha_payload
    )

    rows = BochaProvider("secret").search(
        SearchQuery(
            kind="incremental",
            text="测试",
            category=Category.LASER_COMMUNICATION,
        )
    )

    expected = bocha_payload["webPages"]["value"][0]
    assert rows[0].title == expected["name"]
    assert rows[0].url == expected["url"]
    assert rows[0].summary == expected["summary"]
    assert rows[0].discovery_source == "bocha"
    assert rows[0].category_hint is Category.LASER_COMMUNICATION
    assert rows[0].source_published_at.isoformat() == expected["datePublished"]


def test_bocha_maps_web_result_from_data_wrapped_response(
    respx_mock, bocha_payload
) -> None:
    respx_mock.post("https://api.bochaai.com/v1/web-search").respond(
        200,
        json={
            "code": 200,
            "msg": None,
            "log_id": "safe-log-id",
            "data": bocha_payload,
        },
    )

    rows = BochaProvider("secret").search(
        SearchQuery(kind="incremental", text="测试")
    )

    expected = bocha_payload["webPages"]["value"][0]
    assert rows[0].title == expected["name"]
    assert rows[0].url == expected["url"]
    assert rows[0].summary == expected["summary"]


def test_bocha_falls_back_to_snippet_when_summary_is_empty(
    respx_mock, bocha_payload
) -> None:
    bocha_payload["webPages"]["value"][0]["summary"] = ""
    route = respx_mock.post("https://api.bochaai.com/v1/web-search").respond(
        200, json=bocha_payload
    )

    rows = BochaProvider("secret").search(
        SearchQuery(kind="incremental", text="测试")
    )

    assert route.call_count == 1
    assert rows[0].summary == bocha_payload["webPages"]["value"][0]["snippet"]


def test_bocha_sends_bearer_auth_and_required_discovery_request(
    respx_mock, bocha_payload
) -> None:
    route = respx_mock.post("https://api.bochaai.com/v1/web-search").respond(
        200, json=bocha_payload
    )

    BochaProvider("secret").search(
        SearchQuery(kind="incremental", text="精确查询")
    )

    assert route.calls[0].request.headers["Authorization"] == "Bearer secret"
    assert json.loads(route.calls[0].request.content) == {
        "query": "精确查询",
        "freshness": "oneMonth",
        "summary": True,
        "count": 10,
    }


def test_bocha_counts_every_attempt_across_repeated_searches(
    respx_mock, bocha_payload
):
    respx_mock.post("https://api.bochaai.com/v1/web-search").respond(
        200, json=bocha_payload
    )
    provider = BochaProvider("secret")

    provider.search(SearchQuery(kind="incremental", text="first"))
    provider.search(SearchQuery(kind="incremental", text="second"))

    assert provider.usage_count == 2


def _search_candidate(
    *,
    title: str,
    summary: str,
    url: str,
    category: Category,
    published_at: datetime | None,
) -> Candidate:
    return Candidate(
        title=title,
        url=url,
        summary=summary,
        discovered_at=datetime(2026, 7, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
        discovery_source="bocha",
        category_hint=category,
        source_published_at=published_at,
    )


def test_search_selection_prefers_recent_then_uses_8_to_30_day_fallback(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title=f"星间激光通信终端采购公告 {index}",
            summary="空间激光通信终端采购项目",
            url=f"https://example.cn/recent/{index}",
            category=Category.LASER_COMMUNICATION,
            published_at=fixed_now - timedelta(days=index),
        )
        for index in (1, 2, 3)
    ]
    rows.extend(
        _search_candidate(
            title=f"高能激光反无人机系统招标 {index}",
            summary="激光反无人机装备采购",
            url=f"https://example.cn/fallback/{index}",
            category=Category.LASER_WEAPON,
            published_at=fixed_now - timedelta(days=index),
        )
        for index in (8, 20, 25)
    )
    rows.extend(
        (
            _search_candidate(
                title="空间激光通信激光打印机采购",
                summary="激光打印机和硒鼓",
                url="https://example.cn/noise",
                category=Category.LASER_COMMUNICATION,
                published_at=fixed_now - timedelta(days=1),
            ),
            _search_candidate(
                title="星间激光通信旧闻",
                summary="空间激光通信历史信息",
                url="https://example.cn/old",
                category=Category.LASER_COMMUNICATION,
                published_at=fixed_now - timedelta(days=31),
            ),
            _search_candidate(
                title="卫星公司发布消息",
                summary="商业航天企业动态但没有投资事件",
                url="https://example.cn/not-financing",
                category=Category.COMMERCIAL_SPACE_FINANCING,
                published_at=fixed_now - timedelta(days=1),
            ),
            _search_candidate(
                title="重复的星间激光通信终端采购公告",
                summary="空间激光通信终端采购项目",
                url="https://example.cn/recent/1?utm_source=duplicate",
                category=Category.LASER_COMMUNICATION,
                published_at=fixed_now - timedelta(days=1),
            ),
        )
    )

    selection = select_search_candidates(rows, fixed_now)

    assert len(selection.candidates) == 5
    assert selection.raw_search_count == 10
    assert selection.valid_shape_count == 10
    assert selection.relevance_pass_count == 7
    assert selection.filter_rejected_count == 3
    assert selection.recent_7d_count == 3
    assert selection.fallback_8_30d_count == 2
    assert selection.unknown_date_count == 0
    assert all("noise" not in item.url for item in selection.candidates)
    assert all("old" not in item.url for item in selection.candidates)


@pytest.mark.parametrize(
    ("title", "summary"),
    [
        (
            "2026-2030年中国星间激光通信行业深度研究报告 打印版",
            "中国行业研究网提供报告目录和市场发展前景。",
        ),
        (
            "太空光网筑基：2026年中国星间激光通信行业发展深度洞察",
            "介绍产业规模、市场需求、采购趋势和未来发展前景。",
        ),
        (
            "全球及中国卫星激光通信产业发展报告（2026）",
            "IIM信息发布报告摘要，分析终端批量采购需求。",
        ),
        (
            "一家量子计算公司半年融了20亿丨投融周报",
            "本周商业航天与量子信息成为热点，多家公司完成股权融资。",
        ),
        (
            "2026-07-21 新闻晚报_行业资讯_资讯_航天新域",
            "微光启航完成亿元级融资，云幕智造完成Pre-A轮融资。",
        ),
        (
            "广州商业航天再添两总部，加速构建千亿级全产业链生态",
            "广州开发区控股集团战略投资云遥宇航，推动卫星产业发展。",
        ),
        (
            "火箭成功回收，商业航天拉涨，能否成为高切低方向",
            "A股商业航天概念股上涨，建议关注低位布局机会。",
        ),
        (
            "中金：激光通信产业发展有望加速",
            "券商投资建议，建议关注核心零部件供应商。",
        ),
    ],
)
def test_search_selection_rejects_reports_and_market_commentary(
    fixed_now, title, summary
) -> None:
    row = _search_candidate(
        title=title,
        summary=summary,
        url="https://noise.example/item",
        category=Category.LASER_COMMUNICATION,
        published_at=fixed_now,
    )

    selection = select_search_candidates([row], fixed_now)

    assert selection.candidates == ()
    assert selection.filter_rejected_count == 1


def test_balanced_daily_selection_supplements_fallback_after_minimum_is_met(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title=f"商业航天企业星航{index}完成A轮融资",
            summary=f"卫星公司星航{index}完成股权融资。",
            url=f"https://finance.example/recent/{index}",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=1),
        )
        for index in range(5)
    ]
    rows.extend(
        (
            _search_candidate(
                title="空间激光通信终端招标公告",
                summary="空间激光通信终端采购项目正在招标。",
                url="https://procurement.example/open",
                category=Category.LASER_COMMUNICATION,
                published_at=fixed_now - timedelta(days=12),
            ),
            _search_candidate(
                title="无人机光电吊舱中标结果公告",
                summary="无人机光电吊舱采购项目发布中标结果。",
                url="https://procurement.example/award",
                category=Category.EO_TURRET,
                published_at=fixed_now - timedelta(days=15),
            ),
        )
    )

    selection = select_search_candidates(
        rows,
        fixed_now,
        minimum=5,
        maximum=15,
        balance_business_buckets=True,
    )

    selected_urls = {item.url for item in selection.candidates}
    assert "https://procurement.example/open" in selected_urls
    assert "https://procurement.example/award" in selected_urls
    assert selection.fallback_8_30d_count == 2
    assert len(selection.candidates) == 7


def test_search_selection_accepts_specific_space_company_financing_without_generic_subject():
    row = _search_candidate(
        title="微光启航完成亿元级天使++轮融资",
        summary="资金将用于液氧甲烷火箭发动机量产研发。",
        url="https://media.example/weiguang",
        category=Category.COMMERCIAL_SPACE_FINANCING,
        published_at=datetime(
            2026, 7, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
    )

    selection = select_search_candidates(
        [row],
        datetime(2026, 7, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
        minimum=0,
    )

    assert len(selection.candidates) == 1


def test_search_selection_accepts_space_laser_communication_financing():
    row = _search_candidate(
        title="光邮星空连续完成Pre-A和Pre-A+轮融资",
        summary="公司聚焦高速星地激光通信，资金用于产品规模化量产。",
        url="https://media.example/guangyou",
        category=Category.COMMERCIAL_SPACE_FINANCING,
        published_at=datetime(
            2026, 7, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
    )

    selection = select_search_candidates(
        [row],
        datetime(2026, 7, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
        minimum=0,
    )

    assert len(selection.candidates) == 1


def test_search_selection_rejects_financing_based_only_on_founder_space_background():
    row = _search_candidate(
        title="云幕智造完成数千万元Pre-A轮融资",
        summary=(
            "公司主营重载人形机器人。创始人曾在航天研究所工作，"
            "把航天基因转化为产业创新力。"
        ),
        url="https://media.example/yunmu",
        category=Category.COMMERCIAL_SPACE_FINANCING,
        published_at=datetime(
            2026, 7, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
    )

    selection = select_search_candidates(
        [row],
        datetime(2026, 7, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
        minimum=0,
    )

    assert selection.candidates == ()
    assert selection.filter_rejected_count == 1


def test_search_selection_merges_same_company_and_round_across_media(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title="商业航天企业「鹰飒科技」完成数千万元Pre-A轮融资",
            summary="鹰飒科技宣布完成数千万元Pre-A轮融资，用于卫星研发。",
            url="https://media-a.example/eaglesat",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=1),
        ),
        _search_candidate(
            title="鹰飒科技完成数千万元Pre-A轮融资，加速卫星研制",
            summary="商业航天卫星公司鹰飒科技完成Pre-A轮股权融资。",
            url="https://media-b.example/eaglesat",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now,
        ),
    ]

    selection = select_search_candidates(rows, fixed_now, minimum=0)

    assert len(selection.candidates) == 1
    assert {item.url for item in selection.corroborating_candidates} == {
        "https://media-a.example/eaglesat"
    }
    assert selection.event_duplicate_count == 1


def test_search_selection_prefers_registered_b_sources_over_newer_c_repost(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title="光邮星空完成Pre-A轮融资",
            summary="商业航天卫星激光通信公司光邮星空完成Pre-A轮股权融资。",
            url="https://finance.example.com/new-repost",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now,
        ),
        _search_candidate(
            title="光邮星空完成Pre-A轮融资",
            summary="商业航天卫星激光通信公司光邮星空完成Pre-A轮股权融资。",
            url="https://m.pedaily.cn/news/guangyou",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=1),
        ),
        _search_candidate(
            title="光邮星空完成Pre-A轮融资",
            summary="商业航天卫星激光通信公司光邮星空完成Pre-A轮股权融资。",
            url="https://www.chinaventure.com.cn/news/guangyou",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=2),
        ),
    ]

    selection = select_search_candidates(
        rows,
        fixed_now,
        minimum=0,
        preferred_domains=("pedaily.cn", "chinaventure.com.cn"),
    )

    assert [item.url for item in selection.candidates] == [
        "https://m.pedaily.cn/news/guangyou"
    ]
    assert [item.url for item in selection.corroborating_candidates] == [
        "https://www.chinaventure.com.cn/news/guangyou",
        "https://finance.example.com/new-repost",
    ]


def test_search_selection_keeps_distinct_financing_rounds(fixed_now) -> None:
    rows = [
        _search_candidate(
            title="谱星航天完成天使+轮融资",
            summary="商业航天卫星公司谱星航天完成天使+轮股权融资。",
            url="https://media.example/angel",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now,
        ),
        _search_candidate(
            title="谱星航天完成Pre-A轮融资",
            summary="商业航天卫星公司谱星航天完成Pre-A轮股权融资。",
            url="https://media.example/pre-a",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now,
        ),
    ]

    selection = select_search_candidates(rows, fixed_now, minimum=0)

    assert len(selection.candidates) == 2
    assert selection.event_duplicate_count == 0


def test_search_selection_merges_combined_round_announcement_with_overlapping_sources(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title="光邮星空连续完成Pre-A和Pre-A+轮融资",
            summary="光邮星空聚焦高速卫星激光通信领域。",
            url="https://media-a.example/combined",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now,
        ),
        _search_candidate(
            title="光邮星空获Pre-A轮投资",
            summary="光邮星空是一家卫星激光通信产品公司。",
            url="https://media-b.example/pre-a",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=4),
        ),
        _search_candidate(
            title="「光邮星空」完成Pre-A+轮融资",
            summary="该航天企业提供星地激光通信终端。",
            url="https://media-c.example/pre-a-plus",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=5),
        ),
    ]

    selection = select_search_candidates(rows, fixed_now, minimum=0)

    assert len(selection.candidates) == 1
    assert selection.event_duplicate_count == 2


def test_search_selection_normalizes_financing_company_prefix_and_legal_name(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title="商业航天动态|火箭新锐公司微光启航完成亿元级天使++轮融资",
            summary="资金用于液氧甲烷火箭发动机研发。",
            url="https://media-a.example/weiguang",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now,
        ),
        _search_candidate(
            title="微光启航完成亿元级人民币天使++轮融资",
            summary="这家航天企业正在研制液体火箭。",
            url="https://media-b.example/weiguang",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=1),
        ),
        _search_candidate(
            title="北京微光启航科技有限公司完成天使++轮融资",
            summary="融资用于卫星与火箭相关产品研发。",
            url="https://media-c.example/weiguang",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=2),
        ),
    ]

    selection = select_search_candidates(rows, fixed_now, minimum=0)

    assert len(selection.candidates) == 1
    assert {item.url for item in selection.corroborating_candidates} == {
        "https://media-b.example/weiguang",
        "https://media-c.example/weiguang",
    }
    assert selection.event_duplicate_count == 2


def test_search_selection_does_not_let_old_duplicate_hide_recent_source(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title="鹰飒科技完成Pre-A轮融资",
            summary="商业航天卫星公司鹰飒科技完成Pre-A轮股权融资。",
            url="https://old-media.example/eaglesat",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=40),
        ),
        _search_candidate(
            title="鹰飒科技完成Pre-A轮融资",
            summary="商业航天卫星公司鹰飒科技完成Pre-A轮股权融资。",
            url="https://recent-media.example/eaglesat",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=fixed_now - timedelta(days=1),
        ),
    ]

    selection = select_search_candidates(rows, fixed_now, minimum=0)

    assert [item.url for item in selection.candidates] == [
        "https://recent-media.example/eaglesat"
    ]
    assert selection.corroborating_candidates == ()


def test_search_selection_requires_procurement_event_intent_for_laser_categories(
    fixed_now,
) -> None:
    row = _search_candidate(
        title="星间激光通信技术发展趋势",
        summary="介绍空间激光通信原理和产业前景，没有具体采购事件。",
        url="https://commentary.example/item",
        category=Category.LASER_COMMUNICATION,
        published_at=fixed_now,
    )

    selection = select_search_candidates([row], fixed_now)

    assert selection.candidates == ()


def test_search_selection_merges_print_and_regular_pages_for_same_event(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title="星间激光通信终端采购公告",
            summary="某研究院发布星间激光通信终端采购公告。",
            url="https://example.cn/notice",
            category=Category.LASER_COMMUNICATION,
            published_at=fixed_now,
        ),
        _search_candidate(
            title="星间激光通信终端采购公告 打印版",
            summary="某研究院发布星间激光通信终端采购公告。",
            url="https://example.cn/notice/print",
            category=Category.LASER_COMMUNICATION,
            published_at=fixed_now,
        ),
    ]

    selection = select_search_candidates(rows, fixed_now, minimum=0)

    assert len(selection.candidates) == 1
    assert selection.event_duplicate_count == 1


def test_search_selection_keeps_distinct_lifecycle_events(fixed_now) -> None:
    rows = [
        _search_candidate(
            title="星间激光通信终端项目招标公告",
            summary="某研究院发布星间激光通信终端招标公告。",
            url="https://example.cn/tender",
            category=Category.LASER_COMMUNICATION,
            published_at=fixed_now,
        ),
        _search_candidate(
            title="星间激光通信终端项目中标公告",
            summary="某研究院发布星间激光通信终端中标结果。",
            url="https://example.cn/award",
            category=Category.LASER_COMMUNICATION,
            published_at=fixed_now,
        ),
    ]

    selection = select_search_candidates(rows, fixed_now, minimum=0)

    assert len(selection.candidates) == 2
    assert selection.event_duplicate_count == 0


def test_search_selection_limits_unknown_dates_and_rejects_future_rows(
    fixed_now,
) -> None:
    rows = [
        _search_candidate(
            title=f"商业航天卫星公司完成A轮融资 {index}",
            summary="卫星公司宣布完成股权融资",
            url=f"https://example.cn/unknown/{index}",
            category=Category.COMMERCIAL_SPACE_FINANCING,
            published_at=None,
        )
        for index in range(4)
    ]
    rows.extend(
        (
            _search_candidate(
                title="光电吊舱采购",
                summary="无人机光电载荷招标",
                url="https://example.cn/recent",
                category=Category.EO_TURRET,
                published_at=fixed_now - timedelta(days=1),
            ),
            _search_candidate(
                title="激光武器采购",
                summary="高能激光武器招标",
                url="https://example.cn/future",
                category=Category.LASER_WEAPON,
                published_at=fixed_now + timedelta(hours=25),
            ),
        )
    )

    selection = select_search_candidates(rows, fixed_now)

    assert len(selection.candidates) == 3
    assert selection.recent_7d_count == 1
    assert selection.unknown_date_count == 2
    assert all("future" not in item.url for item in selection.candidates)


def test_search_selection_requires_summary_and_valid_http_url(fixed_now) -> None:
    rows = [
        _search_candidate(
            title="激光反无人机",
            summary="",
            url="https://example.cn/no-summary",
            category=Category.LASER_WEAPON,
            published_at=fixed_now,
        ),
        _search_candidate(
            title="激光反无人机",
            summary="高能激光反制",
            url="file:///tmp/result",
            category=Category.LASER_WEAPON,
            published_at=fixed_now,
        ),
    ]

    selection = select_search_candidates(rows, fixed_now)

    assert selection.valid_shape_count == 0
    assert selection.candidates == ()


@pytest.mark.parametrize(
    ("status_code", "exception_type", "reason"),
    [
        (429, DiscoveryQuotaError, "quota_or_rate_limit"),
        (503, DiscoveryUnavailableError, "server_error"),
    ],
)
def test_bocha_maps_controlled_http_failures_and_counts_attempt(
    respx_mock, status_code, exception_type, reason
):
    respx_mock.post("https://api.bochaai.com/v1/web-search").respond(status_code)
    provider = BochaProvider("secret")

    with pytest.raises(exception_type) as caught:
        provider.search(SearchQuery(kind="incremental", text="limited"))

    assert provider.usage_count == 1
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("status_code", "exception_type", "reason"),
    [
        (400, DiscoveryUnavailableError, "request_rejected"),
        (401, DiscoveryConfigurationError, "authentication"),
        (403, DiscoveryConfigurationError, "authentication"),
        (422, DiscoveryUnavailableError, "request_rejected"),
    ],
)
def test_bocha_maps_every_non_quota_4xx_to_safe_controlled_error(
    respx_mock, status_code, exception_type, reason
) -> None:
    respx_mock.post("https://api.bochaai.com/v1/web-search").respond(
        status_code, text="api_key=super-secret private provider body"
    )

    with pytest.raises(exception_type) as caught:
        BochaProvider("super-secret").search(
            SearchQuery(kind="incremental", text="limited")
        )

    assert "super-secret" not in str(caught.value)
    assert "private provider body" not in str(caught.value)
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("business_code", "exception_type", "reason"),
    [
        (400, DiscoveryUnavailableError, "request_rejected"),
        (401, DiscoveryConfigurationError, "authentication"),
        (403, DiscoveryConfigurationError, "authentication"),
        (429, DiscoveryQuotaError, "quota_or_rate_limit"),
        (503, DiscoveryUnavailableError, "server_error"),
    ],
)
def test_bocha_maps_business_failures_inside_http_200_response(
    respx_mock, business_code, exception_type, reason
) -> None:
    respx_mock.post("https://api.bochaai.com/v1/web-search").respond(
        200,
        json={
            "code": business_code,
            "msg": "private provider body containing super-secret",
            "data": None,
        },
    )

    with pytest.raises(exception_type) as caught:
        BochaProvider("super-secret").search(
            SearchQuery(kind="incremental", text="limited")
        )

    assert caught.value.reason == reason
    assert "super-secret" not in str(caught.value)
    assert "private provider body" not in str(caught.value)


def test_bocha_maps_transport_failure_without_leaking_key(respx_mock) -> None:
    request = httpx.Request("POST", "https://api.bochaai.com/v1/web-search")
    respx_mock.post("https://api.bochaai.com/v1/web-search").mock(
        side_effect=httpx.ReadTimeout("timeout containing super-secret", request=request)
    )

    with pytest.raises(DiscoveryUnavailableError) as caught:
        BochaProvider("super-secret").search(
            SearchQuery(kind="incremental", text="limited")
        )

    assert caught.value.reason == "network_or_timeout"
    assert "super-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"text": "{not-json", "headers": {"content-type": "application/json"}},
        {"json": {}},
        {"json": {"code": 200, "data": {}}},
        {"json": {"code": True, "data": {}}},
        {"json": {"webPages": {}}},
        {"json": {"webPages": {"value": {}}}},
        {"json": {"webPages": {"value": [None]}}},
        {"json": {"webPages": {"value": [{"name": "A"}]}}},
        {
            "json": {
                "webPages": {
                    "value": [
                        {"name": 7, "url": "https://x.cn/a", "summary": "summary"}
                    ]
                }
            }
        },
    ],
)
def test_bocha_maps_malformed_success_payload_to_controlled_error(
    respx_mock, response_kwargs
) -> None:
    respx_mock.post("https://api.bochaai.com/v1/web-search").respond(
        200, **response_kwargs
    )

    with pytest.raises(DiscoveryUnavailableError, match="response invalid") as caught:
        BochaProvider("super-secret").search(
            SearchQuery(kind="incremental", text="malformed")
        )
    assert caught.value.reason == "invalid_response"


def test_official_collector_continues_after_one_seed_fails(respx_mock, official_html):
    respx_mock.get("https://bad.gov.cn/list").respond(503)
    respx_mock.get("https://good.gov.cn/list").respond(200, text=official_html)

    collector = OfficialSeedCollector(SEEDS)
    rows = collector.collect()

    assert [row.url for row in rows] == ["https://good.gov.cn/notice/1"]
    assert collector.failed_domains == frozenset({"bad.gov.cn"})


def test_official_collector_records_selector_coverage_degradation(respx_mock, official_html):
    seed = OfficialSeed(
        name="empty",
        domain="empty.gov.cn",
        grade=SourceGrade.A,
        list_urls=["https://empty.gov.cn/list"],
        link_selector="div.does-not-exist a[href]",
    )
    respx_mock.get("https://empty.gov.cn/list").respond(200, text=official_html)

    collector = OfficialSeedCollector([seed])

    assert collector.collect() == []
    assert collector.failed_domains == frozenset({"empty.gov.cn"})


def test_dedupe_normalizes_tracking_and_fragment(fixed_now):
    rows = [
        Candidate(
            title="A",
            url="https://x.gov.cn/a?utm_source=t#top",
            discovered_at=fixed_now,
            discovery_source="one",
        ),
        Candidate(
            title="A duplicate",
            url="https://x.gov.cn/a",
            discovered_at=fixed_now,
            discovery_source="two",
        ),
    ]

    assert [row.url for row in dedupe_candidates(rows)] == ["https://x.gov.cn/a"]


def test_normalize_url_lowercases_authority_removes_defaults_and_sorts_query():
    assert normalize_url("HTTPS://Example.CN:443/a?z=2&source=x&a=1&utm_medium=m#part") == (
        "https://example.cn/a?a=1&z=2"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.gov.cn/a?flag#top", "https://x.gov.cn/a?flag"),
        ("https://x.gov.cn/a?a=1&&b=%2F", "https://x.gov.cn/a?a=1&&b=%2F"),
        ("https://x.gov.cn/a?b=2&a=1&a=0", "https://x.gov.cn/a?a=0&a=1&b=2"),
        (
            "https://x.gov.cn/a?x=%2F&utm%5Fsource=t&spm=s&from=f&source=n&a=1",
            "https://x.gov.cn/a?a=1&x=%2F",
        ),
        ("https://x.gov.cn/a?z=%2F&a=1", "https://x.gov.cn/a?a=1&z=%2F"),
    ],
)
def test_normalize_url_preserves_raw_query_semantics(url, expected):
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    ("seed_name", "fixture_name", "expected_url"),
    [
        ("中国政府采购网", "official_ccgp.html", "https://www.ccgp.gov.cn/cggg/notice/1.html"),
        ("全军武器装备采购信息网", "official_plap.html", "https://www.plap.mil.cn/notices/1.html"),
        ("中国招标投标公共服务平台", "official_ceb.html", "https://bulletin.cebpubservice.com/notice/1.html"),
        ("全国公共资源交易平台", "official_ggzy.html", "https://www.ggzy.gov.cn/notice/1.html"),
    ],
)
def test_configured_official_seed_selectors_match_sanitized_lists(
    respx_mock, seed_name, fixture_name, expected_url
):
    root = Path(__file__).parents[1]
    configured = yaml.safe_load((root / "config" / "official_sources.yaml").read_text(encoding="utf-8"))
    seeds = [OfficialSeed.model_validate(item) for item in configured["official_sources"]]
    seed = next(item for item in seeds if item.name == seed_name)
    html = (root / "tests" / "fixtures" / fixture_name).read_text(encoding="utf-8")
    respx_mock.get(seed.list_urls[0]).respond(200, text=html)

    rows = OfficialSeedCollector([seed]).collect()

    assert [row.url for row in rows] == [expected_url]
