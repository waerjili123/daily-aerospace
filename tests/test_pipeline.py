from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from laser_space_daily.fetcher import FetchError, FetchedPage
from laser_space_daily.analyzer import (
    AnalyzerError,
    ResilientAnalyzer,
    RuleFallbackAnalyzer,
)
from laser_space_daily.agentic_discovery import (
    AgenticDiscoveryResult,
    ResearchTraceItem,
)
from laser_space_daily.discovery import (
    DiscoveryConfigurationError,
    DiscoveryQuotaError,
    DiscoveryUnavailableError,
    normalize_url,
)
from laser_space_daily.matching import MatchDecision
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
    SourceGrade,
    SourceRecord,
    StateBundle,
    TrendSummary,
    VerificationStatus,
)
from laser_space_daily.pipeline import Pipeline, _financing_index
from laser_space_daily.report import ReportRenderer
from laser_space_daily.verification_followup import VerificationFollowupPlanner
from laser_space_daily.verifier import (
    RuleVerifier,
    SourceRegistry,
    VerificationDecision,
)


BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 22, 9, 30, tzinfo=BEIJING)
OFFICIAL_URL = "https://official.example.cn/notices/award-1"
SECOND_URL = "https://official.example.cn/notices/award-2"


def candidate(
    url: str = OFFICIAL_URL,
    *,
    source: str = "official:official.example.cn",
    discovered_at: datetime = NOW,
    summary: str = "",
    title: str = "Laser terminal award",
    category_hint: Category | None = None,
    source_published_at: datetime | None = None,
) -> Candidate:
    return Candidate(
        title=title,
        url=url,
        summary=summary,
        discovered_at=discovered_at,
        discovery_source=source,
        category_hint=category_hint,
        source_published_at=source_published_at,
    )


def page(url: str = OFFICIAL_URL) -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=200,
        title="Laser terminal award",
        text="Laser terminal award\nOrganization: Space Institute\nPublished: 2026-07-21",
        fetched_at=NOW,
        content_hash="0" * 64,
    )


def analysis(
    url: str = OFFICIAL_URL,
    *,
    event_type: EventType = EventType.AWARD,
    category: Category = Category.LASER_COMMUNICATION,
    published_at: datetime | None = None,
    degraded: bool = False,
    title: str = "Laser terminal award",
) -> AnalysisResult:
    published = published_at or datetime(2026, 7, 21, tzinfo=BEIJING)
    return AnalysisResult(
        in_china=True,
        in_scope=True,
        category=category,
        event_type=event_type,
        title=title,
        organization="Space Institute",
        published_at=published,
        project_codes=["LS-1"] if category is not Category.COMMERCIAL_SPACE_FINANCING else [],
        financing_round="A" if category is Category.COMMERCIAL_SPACE_FINANCING else None,
        amount="1000万元" if category is Category.COMMERCIAL_SPACE_FINANCING else None,
        investors=["Capital One"] if category is Category.COMMERCIAL_SPACE_FINANCING else [],
        evidence=[Evidence(field="title", quote=title, source_url=url)],
        source_url=url,
        degraded=degraded,
    )


def stored_event(
    event_id: str = "old-event",
    *,
    published_at: datetime | None = None,
    event_type: EventType = EventType.TENDER,
) -> Event:
    published = published_at or datetime(2026, 7, 20, tzinfo=BEIJING)
    item_analysis = analysis(
        "https://official.example.cn/notices/old",
        event_type=event_type,
        published_at=published,
        title="Laser terminal tender",
    )
    return Event(
        event_id=event_id,
        category=Category.LASER_COMMUNICATION,
        title=item_analysis.title,
        organization="Space Institute",
        published_at=published,
        source_url=item_analysis.source_url,
        source_grade=SourceGrade.A,
        verification_status=VerificationStatus.VERIFIED,
        event_type=event_type,
        evidence=item_analysis.evidence,
        analysis=item_analysis,
    )


class FakeRepository:
    def __init__(self, state: StateBundle | None = None) -> None:
        self.state = state or StateBundle()
        self.commits: list[StateBundle] = []

    def load(self) -> StateBundle:
        return self.state.model_copy(deep=True)

    def commit(self, state: StateBundle) -> None:
        self.commits.append(state.model_copy(deep=True))
        self.state = state.model_copy(deep=True)


class FakePlanner:
    def __init__(self) -> None:
        self.queries = [SimpleNamespace(kind="incremental", text="laser")]

    def plan(self, now: datetime, projects: list[Project]):
        return list(self.queries)


class FakeSearchProvider:
    def __init__(self) -> None:
        self.rows: list[Candidate] = []
        self.error: BaseException | None = None
        self.usage_count = 0
        self.calls = 0
        self.responses: dict[str, list[Candidate]] = {}

    def search(self, query, **_kwargs):
        self.calls += 1
        self.usage_count += 1
        if self.error:
            raise self.error
        return list(self.responses.get(getattr(query, "text", ""), self.rows))


class FakeOfficialCollector:
    def __init__(self) -> None:
        self.rows = [candidate()]
        self.failed_domains: frozenset[str] = frozenset()
        self.calls = 0

    def collect(self) -> list[Candidate]:
        self.calls += 1
        return list(self.rows)


class FakeFetcher:
    def __init__(self) -> None:
        self.errors: dict[str, BaseException] = {}
        self.pages: dict[str, FetchedPage] = {}
        self.calls: list[str] = []

    def fetch(self, item: Candidate) -> FetchedPage:
        self.calls.append(item.url)
        error = self.errors.get(item.url)
        if error:
            raise error
        return self.pages.get(item.url, page(item.url))


class FakeAnalyzer:
    def __init__(self) -> None:
        self.results: dict[str, AnalysisResult] = {}
        self.errors: dict[str, BaseException] = {}
        self.deepseek_tokens = 0
        self.token_increment = 0

    def analyze(self, fetched: FetchedPage) -> AnalysisResult:
        self.deepseek_tokens += self.token_increment
        error = self.errors.get(fetched.final_url)
        if error:
            raise error
        return self.results.get(fetched.final_url, analysis(fetched.final_url))


class FakeVerifier:
    def __init__(self) -> None:
        self.decisions: dict[str, VerificationDecision] = {}
        self.corroborating_by_url: dict[str, list[object]] = {}

    def verify(
        self,
        result: AnalysisResult,
        fetched: FetchedPage,
        corroborating=(),
    ) -> VerificationDecision:
        self.corroborating_by_url[fetched.final_url] = list(corroborating)
        return self.decisions.get(
            fetched.final_url,
            VerificationDecision(
                status=VerificationStatus.VERIFIED,
                reason="verified_tender",
                source_grade=SourceGrade.A,
                evidence=result.evidence,
            ),
        )


class FakeMatcher:
    def __init__(self) -> None:
        self.decision: MatchDecision | None = None

    def match(self, event: Event, projects: list[Project]) -> MatchDecision:
        return self.decision or MatchDecision(
            relation="new_project", reason="no_match", score=0
        )


class FakeTrendSummarizer:
    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.deepseek_tokens = 0
        self.token_increment = 0
        self.windows: list[tuple[datetime, datetime]] = []

    def summarize_trends(self, state: StateBundle, window) -> TrendSummary:
        self.windows.append(window)
        self.deepseek_tokens += self.token_increment
        if self.error:
            raise self.error
        return TrendSummary(
            window_start=window[0],
            window_end=window[1],
            summary="verified trend",
            event_count=len(state.events) + len(state.financings),
        )


def test_pipeline_requests_rolling_three_month_trend_window(deps) -> None:
    Pipeline(**deps.as_kwargs()).run(NOW)

    assert deps.trend_summarizer.windows == [
        (datetime(2026, 4, 22, 9, 30, tzinfo=BEIJING), NOW)
    ]


def test_pipeline_writes_current_run_candidate_checkpoint_after_analysis(deps) -> None:
    checkpoints: list[dict] = []

    Pipeline(
        **deps.as_kwargs(),
        checkpoint_writer=checkpoints.append,
    ).run(NOW)

    assert len(checkpoints) == 1
    assert checkpoints[0]["status"] == "analyzed"
    assert checkpoints[0]["occurred_at"] == NOW.isoformat()
    assert checkpoints[0]["candidates"][0]["source_url"] == OFFICIAL_URL
    assert checkpoints[0]["candidates"][0]["organization"] == "Space Institute"
    assert checkpoints[0]["candidates"][0]["published_at"].startswith(
        "2026-07-21"
    )


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str) -> None:
        self.messages.append(message)


@pytest.fixture
def deps():
    values = SimpleNamespace(
        repository=FakeRepository(),
        planner=FakePlanner(),
        search_provider=FakeSearchProvider(),
        official_collector=FakeOfficialCollector(),
        fetcher=FakeFetcher(),
        analyzer=FakeAnalyzer(),
        verifier=FakeVerifier(),
        matcher=FakeMatcher(),
        trend_summarizer=FakeTrendSummarizer(),
        logger=FakeLogger(),
    )
    values.as_kwargs = lambda: {
        name: getattr(values, name)
        for name in (
            "repository",
            "planner",
            "search_provider",
            "official_collector",
            "fetcher",
            "analyzer",
            "verifier",
            "matcher",
            "trend_summarizer",
            "logger",
        )
    }
    return values


def pending_decision(reason: str = "source_unavailable") -> VerificationDecision:
    return VerificationDecision(
        status=VerificationStatus.PENDING,
        reason=reason,
        source_grade=SourceGrade.A,
    )


def test_pipeline_routes_verified_and_pending(deps) -> None:
    unreachable = candidate(SECOND_URL, summary="Search-provider fallback summary")
    rejected_url = "https://news.example.cn/rejected"
    rejected = candidate(rejected_url, summary="Visible search candidate")
    deps.official_collector.rows = [candidate(), unreachable, rejected]
    deps.verifier.decisions[SECOND_URL] = pending_decision()
    deps.verifier.decisions[rejected_url] = VerificationDecision(
        status=VerificationStatus.REJECTED,
        reason="out_of_scope",
        source_grade=SourceGrade.C,
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.verified_count == 1
    assert result.metrics.pending_count == 1
    assert len(result.state.events) == 1
    assert result.state.pending[0].reason == "source_unavailable"
    assert result.state.pending[0].summary == "Search-provider fallback summary"
    diagnostics = {
        item.source_url: item for item in result.candidate_diagnostics
    }
    assert diagnostics[OFFICIAL_URL].stage == "persisted"
    assert diagnostics[OFFICIAL_URL].status == "verified"
    assert diagnostics[OFFICIAL_URL].source_grade is SourceGrade.A
    assert diagnostics[SECOND_URL].stage == "persisted"
    assert diagnostics[SECOND_URL].status == "pending"
    assert diagnostics[SECOND_URL].reason == "source_unavailable"
    assert diagnostics[SECOND_URL].elastic_eligible is False
    assert (
        diagnostics[SECOND_URL].elastic_ineligible_reason
        == "followup_disabled"
    )
    assert diagnostics[rejected_url].stage == "verification"
    assert diagnostics[rejected_url].status == "rejected"
    assert diagnostics[rejected_url].reason == "out_of_scope"
    assert diagnostics[rejected_url].source_grade is SourceGrade.C


def test_pipeline_diagnostic_preserves_procurement_stage_and_deadline(deps) -> None:
    deadline = NOW + timedelta(days=5)
    analyzed = analysis(event_type=EventType.TENDER).model_copy(
        update={
            "bid_submission_deadline": deadline,
            "deadline_precision": {"bid_submission": "minute"},
            "evidence": [
                Evidence(
                    field="bid_submission_deadline",
                    quote="投标截止时间：2026-07-27 09:30",
                    source_url=OFFICIAL_URL,
                )
            ],
        }
    )
    deps.analyzer.results[OFFICIAL_URL] = analyzed

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    diagnostic = result.candidate_diagnostics[0]
    assert diagnostic.event_type is EventType.TENDER
    assert diagnostic.bid_submission_deadline == deadline
    assert diagnostic.deadline_precision == {"bid_submission": "minute"}
    assert diagnostic.deadline_evidence_fields == ["bid_submission_deadline"]


def test_pipeline_analyzes_each_corroborating_source_before_verification(deps) -> None:
    deps.official_collector.rows = [candidate(), candidate(SECOND_URL)]

    Pipeline(**deps.as_kwargs()).run(NOW)

    corroborating = deps.verifier.corroborating_by_url[OFFICIAL_URL]
    assert len(corroborating) == 1
    secondary_analysis, secondary_page = corroborating[0]
    assert isinstance(secondary_analysis, AnalysisResult)
    assert isinstance(secondary_page, FetchedPage)
    assert secondary_analysis.source_url == SECOND_URL
    assert secondary_page.final_url == SECOND_URL


def test_pipeline_fetches_event_duplicate_search_source_for_corroboration(deps) -> None:
    primary_url = "https://media-a.example/eaglesat"
    corroborating_url = "https://media-b.example/eaglesat"
    common = {
        "summary": "商业航天卫星公司鹰飒科技完成数千万元Pre-A轮股权融资。",
        "category_hint": Category.COMMERCIAL_SPACE_FINANCING,
    }
    deps.official_collector.rows = []
    deps.search_provider.rows = [
        candidate(
            primary_url,
            source="search:bocha",
            title="鹰飒科技完成数千万元Pre-A轮融资",
            source_published_at=NOW - timedelta(days=1),
            **common,
        ),
        candidate(
            corroborating_url,
            source="search:bocha",
            title="商业航天企业「鹰飒科技」完成Pre-A轮融资",
            source_published_at=NOW,
            **common,
        ),
    ]

    Pipeline(**deps.as_kwargs()).run(NOW)

    assert set(deps.fetcher.calls) == {primary_url, corroborating_url}
    assert len(deps.verifier.corroborating_by_url[corroborating_url]) == 1


def test_pipeline_strictly_verifies_consistent_chinaventure_and_pedaily_sources(
    deps,
) -> None:
    announced_at = datetime(2026, 7, 7, tzinfo=BEIJING)
    chinaventure_url = "https://www.chinaventure.com.cn/news/116-test.html"
    pedaily_url = "https://m.pedaily.cn/news/566-test"
    common_summary = (
        "北京微光启航科技有限公司完成亿元级天使++轮股权融资，"
        "资金用于商业航天液体火箭和发动机研发。"
    )
    chinaventure = candidate(
        chinaventure_url,
        source="search:bocha",
        title="微光启航完成亿元级人民币天使++轮融资",
        summary=common_summary,
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=announced_at,
    )
    pedaily = candidate(
        pedaily_url,
        source="search:bocha",
        title="微光启航完成亿元级人民币天使++轮融资",
        summary=common_summary,
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=announced_at,
    )

    class FakeResearcher:
        deepseek_tokens = 0

        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=(chinaventure, pedaily),
                trace=(),
                budget=12,
                budget_used=12,
                search_count=12,
                agent_round_count=2,
                duplicate_query_count=0,
                degraded=False,
                error_reasons=[],
                stop_reason="budget_exhausted",
            )

    chinaventure_text = (
        "微光启航完成亿元级人民币天使++轮融资\n"
        "发布时间：2026-07-07 14:13:24\n"
        "2026年7月7日，北京微光启航科技有限公司宣布完成亿元级人民币天使++轮融资。\n"
        "资金将用于商业航天液体火箭和发动机研发。\n"
        "行业背景随后比较了美国SpaceX的可回收火箭路线。"
    )
    pedaily_text = (
        "微光启航完成亿元级人民币天使++轮融资\n"
        "页面发布时间：2026-07-07\n"
        "2026年7月7日，北京微光启航科技有限公司完成亿元级人民币天使++轮融资。\n"
        "本轮资金用于商业航天液体火箭、发动机及核心部件研发。"
    )
    deps.fetcher.pages[chinaventure_url] = FetchedPage(
        requested_url=chinaventure_url,
        final_url=chinaventure_url,
        status_code=200,
        title=chinaventure.title,
        text=chinaventure_text,
        fetched_at=NOW,
        content_hash="1" * 64,
        publication_date_quote="2026-07-07 14:13:24",
        publication_date_source="visible_header",
    )
    deps.fetcher.pages[pedaily_url] = FetchedPage(
        requested_url=pedaily_url,
        final_url=pedaily_url,
        status_code=200,
        title=pedaily.title,
        text=pedaily_text,
        fetched_at=NOW,
        content_hash="2" * 64,
        publication_date_quote="2026-07-07",
        publication_date_source="metadata",
    )
    deps.official_collector.rows = []
    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeResearcher()
    arguments["analyzer"] = RuleFallbackAnalyzer()
    arguments["verifier"] = RuleVerifier(
        SourceRegistry(
            {},
            financing_b_domains=(
                "chinaventure.com.cn",
                "pedaily.cn",
            ),
        )
    )
    arguments["verification_followup"] = VerificationFollowupPlanner(
        financing_b_domains=(
            "chinaventure.com.cn",
            "pedaily.cn",
        ),
        elastic_budget=0,
        pool_days=90,
        max_targets=3,
    )

    result = Pipeline(**arguments).run(NOW)

    assert result.metrics.verified_count >= 1
    assert len(result.state.financings) == 1
    financing = result.state.financings[0]
    assert financing.verification_status is VerificationStatus.VERIFIED
    assert {record.source_url for record in financing.source_records} == {
        chinaventure_url,
        pedaily_url,
    }
    diagnostics = {
        item.source_url: item for item in result.candidate_diagnostics
    }
    assert diagnostics[chinaventure_url].publication_date_source == "visible_header"
    assert diagnostics[pedaily_url].publication_date_source == "metadata"
    assert (
        diagnostics[chinaventure_url].verification_event_key
        == diagnostics[pedaily_url].verification_event_key
    )


def test_pipeline_persists_two_b_source_evidence_records(deps) -> None:
    financing_result = analysis(category=Category.COMMERCIAL_SPACE_FINANCING)
    deps.analyzer.results[OFFICIAL_URL] = financing_result
    source_records = [
        SourceRecord(
            source_url=url,
            source_grade=SourceGrade.B,
            published_at=financing_result.published_at,
            content_hash=hash_value,
            evidence=[Evidence(field="organization", quote="Space Institute", source_url=url)],
        )
        for url, hash_value in ((OFFICIAL_URL, "1" * 64), (SECOND_URL, "2" * 64))
    ]
    deps.verifier.decisions[OFFICIAL_URL] = VerificationDecision(
        status=VerificationStatus.VERIFIED,
        reason="verified_financing_two_independent_sources",
        source_grade=SourceGrade.B,
        evidence=[item for record in source_records for item in record.evidence],
        source_records=source_records,
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert {record.source_url for record in result.state.financings[0].source_records} == {
        OFFICIAL_URL,
        SECOND_URL,
    }
    assert set(result.state.financings[0].source_urls) == {OFFICIAL_URL, SECOND_URL}


def test_pipeline_persists_deadline_value_precision_and_evidence(deps) -> None:
    deadline = datetime(2026, 7, 25, 9, 30, tzinfo=BEIJING)
    deadline_evidence = Evidence(
        field="bid_submission_deadline",
        quote="投标截止时间：2026-07-25 09:30",
        source_url=OFFICIAL_URL,
    )
    analyzed = analysis().model_copy(
        update={
            "bid_submission_deadline": deadline,
            "deadline_precision": {"bid_submission": "minute"},
            "evidence": [*analysis().evidence, deadline_evidence],
        }
    )
    deps.analyzer.results[OFFICIAL_URL] = analyzed
    deps.verifier.decisions[OFFICIAL_URL] = VerificationDecision(
        status=VerificationStatus.VERIFIED,
        reason="verified_tender",
        source_grade=SourceGrade.A,
        evidence=analyzed.evidence,
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    project = result.state.projects[0]
    assert project.deadlines == {"bid_submission": deadline}
    assert project.deadline_precision == {"bid_submission": "minute"}
    assert project.deadline_evidence["bid_submission"] == deadline_evidence


def test_pipeline_persists_grounded_financing_business_area(deps) -> None:
    result = analysis(
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
    )
    result.business_area = "商业运载火箭"
    result.evidence.append(
        Evidence(
            field="business_area",
            quote="业务领域：商业运载火箭",
            source_url=OFFICIAL_URL,
        )
    )
    deps.analyzer.results[OFFICIAL_URL] = result

    run = Pipeline(**deps.as_kwargs()).run(NOW)

    assert run.state.financings[0].business_area == "商业运载火箭"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            DiscoveryQuotaError("quota api-key=super-secret"),
            "quota_or_rate_limit",
        ),
        (
            DiscoveryConfigurationError("authentication rejected"),
            "authentication",
        ),
        (
            DiscoveryUnavailableError(
                "service unavailable", reason="server_error"
            ),
            "server_error",
        ),
    ],
)
def test_search_api_failure_still_runs_official_collector(
    deps, error, reason
) -> None:
    deps.search_provider.error = error

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.search_coverage_degraded is True
    assert result.metrics.search_api_usage == 1
    assert result.metrics.search_failure_reasons == [reason]
    assert result.metrics.official_candidate_count == 1
    assert deps.official_collector.calls == 1


def test_rerun_does_not_duplicate_state(deps) -> None:
    first = Pipeline(**deps.as_kwargs()).run(NOW)
    second = Pipeline(**deps.as_kwargs()).run(NOW)

    assert [item.event_id for item in first.state.events] == [
        item.event_id for item in second.state.events
    ]
    assert [item.project_id for item in first.state.projects] == [
        item.project_id for item in second.state.projects
    ]
    assert len(second.state.events) == len(second.state.projects) == 1


def test_ambiguous_match_is_pending_not_merged(deps) -> None:
    project = Project(
        project_id="p1",
        name="Laser terminal",
        organization="Space Institute",
        category=Category.LASER_COMMUNICATION,
        status="open",
    )
    deps.repository.state = StateBundle(projects=[project])
    deps.matcher.decision = MatchDecision(
        relation="suspected", project_id="p1", reason="similar", score=0.84
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert any(item.reason == "suspected_project_match" for item in result.state.pending)
    assert result.state.projects[0].event_ids == []
    assert result.state.events == []


def test_candidate_failure_does_not_stop_next_and_records_failed_domain(deps) -> None:
    broken_url = "https://broken.example.cn/private?api_key=super-secret"
    deps.official_collector.rows = [candidate(broken_url), candidate()]
    deps.fetcher.errors[broken_url] = FetchError("response body super-secret")

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.verified_count == 1
    assert result.metrics.pending_count == 1
    assert "broken.example.cn" in result.metrics.failed_domains
    assert len(result.state.events) == 1


def test_invalid_verified_payload_is_pending_and_does_not_stop_next(deps) -> None:
    deps.official_collector.rows = [candidate(), candidate(SECOND_URL)]
    deps.analyzer.results[OFFICIAL_URL] = analysis().model_copy(
        update={"organization": None}
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.verified_count == 1
    assert result.metrics.pending_count == 1
    assert len(result.state.events) == 1
    assert result.state.pending[0].reason == "verified_payload_invalid"


def test_unverified_decision_never_enters_formal_state(deps) -> None:
    deps.verifier.decisions[OFFICIAL_URL] = VerificationDecision(
        status=VerificationStatus.REJECTED,
        reason="out_of_scope",
        source_grade=SourceGrade.A,
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.state.events == []
    assert result.state.financings == []
    assert result.state.pending == []
    assert result.metrics.verified_count == result.metrics.pending_count == 0


def test_stage_update_keeps_old_event_chain(deps) -> None:
    old = stored_event()
    project = Project(
        project_id="p1",
        name="Laser terminal",
        organization="Space Institute",
        category=Category.LASER_COMMUNICATION,
        status="open",
        event_ids=[old.event_id],
        current_stage=EventType.TENDER,
        first_published_at=old.published_at,
        latest_event_at=old.published_at,
    )
    deps.repository.state = StateBundle(events=[old], projects=[project])
    deps.matcher.decision = MatchDecision(
        relation="same_project", project_id="p1", reason="exact", score=1
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    updated = result.state.projects[0]
    assert updated.event_ids[0] == old.event_id
    assert len(updated.event_ids) == 2
    assert updated.current_stage is EventType.AWARD
    assert updated.status == "awarded"
    assert updated.needs_recheck is False
    assert result.metrics.status_update_count == 1


@pytest.mark.parametrize(
    ("event_type", "status", "needs_recheck"),
    [
        (EventType.PROCUREMENT_INTENTION, "upcoming", True),
        (EventType.TENDER, "open", True),
        (EventType.INQUIRY, "open", True),
        (EventType.COMPARISON, "open", True),
        (EventType.REBID, "open", True),
        (EventType.CHANGE, "open", True),
        (EventType.EXTENSION, "open", True),
        (EventType.CANDIDATE, "evaluating", True),
        (EventType.AWARD, "awarded", False),
        (EventType.FAILED, "failed", True),
        (EventType.TERMINATION, "terminated", False),
    ],
)
def test_new_project_status_mapping(
    deps, event_type: EventType, status: str, needs_recheck: bool
) -> None:
    deps.analyzer.results[OFFICIAL_URL] = analysis(event_type=event_type)

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    project = result.state.projects[0]
    assert project.current_stage is event_type
    assert project.status == status
    assert project.needs_recheck is needs_recheck


def test_older_event_is_appended_without_rolling_back_stage(deps) -> None:
    latest = stored_event(event_type=EventType.AWARD)
    project = Project(
        project_id="p1",
        name="Laser terminal",
        organization="Space Institute",
        category=Category.LASER_COMMUNICATION,
        status="awarded",
        event_ids=[latest.event_id],
        current_stage=EventType.AWARD,
        first_published_at=latest.published_at,
        latest_event_at=latest.published_at,
        needs_recheck=False,
    )
    deps.repository.state = StateBundle(events=[latest], projects=[project])
    deps.matcher.decision = MatchDecision(
        relation="same_project", project_id="p1", reason="exact", score=1
    )
    deps.analyzer.results[OFFICIAL_URL] = analysis(
        published_at=datetime(2026, 6, 1, tzinfo=BEIJING),
        event_type=EventType.TENDER,
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    updated = result.state.projects[0]
    assert len(updated.event_ids) == 2
    assert updated.current_stage is EventType.AWARD
    assert updated.status == "awarded"
    assert result.metrics.status_update_count == 0


def test_newer_same_stage_event_is_not_a_status_update(deps) -> None:
    old = stored_event(event_type=EventType.TENDER)
    project = Project(
        project_id="p1",
        name="Laser terminal",
        organization="Space Institute",
        category=Category.LASER_COMMUNICATION,
        status="open",
        event_ids=[old.event_id],
        current_stage=EventType.TENDER,
        first_published_at=old.published_at,
        latest_event_at=old.published_at,
    )
    deps.repository.state = StateBundle(events=[old], projects=[project])
    deps.matcher.decision = MatchDecision(
        relation="same_project", project_id="p1", reason="exact", score=1
    )
    deps.analyzer.results[OFFICIAL_URL] = analysis(event_type=EventType.TENDER)

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.state.projects[0].latest_event_at == datetime(
        2026, 7, 21, tzinfo=BEIJING
    )
    assert result.metrics.status_update_count == 0
    assert result.changed_project_ids == ["p1"]


def test_late_discovered_old_notice_is_in_changed_set_with_actual_date(deps) -> None:
    old_date = datetime(2025, 12, 1, tzinfo=BEIJING)
    deps.analyzer.results[OFFICIAL_URL] = analysis(published_at=old_date)

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    inserted = result.state.events[0]
    assert inserted.event_id in result.changed_event_ids
    assert inserted.published_at == old_date


def test_near_date_financing_source_merge_refreshes_rolling_report(deps) -> None:
    pipeline = Pipeline(**deps.as_kwargs())
    first_seen = datetime(2026, 7, 19, 9, 30, tzinfo=BEIJING)
    deps.official_collector.rows = [candidate(discovered_at=first_seen)]
    deps.analyzer.results[OFFICIAL_URL] = analysis(
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        title="Space Institute A round",
        published_at=datetime(2026, 7, 18, tzinfo=BEIJING),
    )

    original = pipeline.run(first_seen)

    original_id = original.state.financings[0].financing_id
    assert original.changed_financing_ids == [original_id]
    deps.repository.state = original.state.model_copy(
        update={
            "financings": [
                original.state.financings[0].model_copy(
                    update={"source_published_at": {}}
                )
            ]
        }
    )

    deps.official_collector.rows = [candidate(SECOND_URL)]
    deps.analyzer.results[SECOND_URL] = analysis(
        SECOND_URL,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        title="Space Institute A round",
        published_at=datetime(2026, 7, 21, 12, tzinfo=BEIJING),
    )

    result = pipeline.run(NOW)

    assert result.state.events == []
    assert len(result.state.financings) == 1
    assert result.state.financings[0].financing_id == original_id
    assert set(result.state.financings[0].source_urls) == {OFFICIAL_URL, SECOND_URL}
    assert result.metrics.verified_count == 1
    assert result.metrics.deduplicated_count == 1
    assert result.changed_event_ids == []
    assert result.changed_financing_ids == [original_id]
    assert result.state.financings[0].announced_at == datetime(
        2026, 7, 18, tzinfo=BEIJING
    )
    assert result.state.financings[0].source_published_at == {
        OFFICIAL_URL: datetime(2026, 7, 18, tzinfo=BEIJING),
        SECOND_URL: datetime(2026, 7, 21, 12, tzinfo=BEIJING),
    }
    report = ReportRenderer(18000).render(result).markdown
    top_section = report.split(
        "## 今日最值得看", maxsplit=1
    )[1].split("## 过去24小时新增/变化", maxsplit=1)[0]
    assert "Space Institute" in top_section
    assert OFFICIAL_URL in top_section
    assert SECOND_URL in top_section
    assert report.count("Space Institute") == 1


def test_financing_index_merges_same_verified_source_bundle_despite_optional_drift() -> None:
    shared_sources = [
        "https://m.pedaily.cn/news/566658",
        "https://www.chinaventure.com.cn/news/114-20260716-392303.html",
    ]
    existing = Financing(
        financing_id="first",
        company="光邮星空",
        announced_at=datetime(2026, 7, 16, tzinfo=BEIJING),
        round_name="Pre-A+轮",
        financing_subtype="round_equity",
        amount_disclosed=False,
        investors=[],
        source_url=shared_sources[0],
        source_urls=shared_sources,
        verification_status=VerificationStatus.VERIFIED,
    )
    incoming = existing.model_copy(
        update={
            "financing_id": "second",
            "announced_at": datetime(2026, 7, 21, tzinfo=BEIJING),
            "amount_disclosed": True,
            "amount_cny": 100_000_000,
            "investors": ["中关村科学城"],
            "source_url": shared_sources[1],
        }
    )

    assert _financing_index([existing], incoming) == 0


def test_financing_index_merges_legal_name_and_combined_round_from_same_sources() -> None:
    shared_sources = [
        "https://m.pedaily.cn/news/566658",
        "https://www.chinaventure.com.cn/news/114-20260716-392303.html",
    ]
    existing = Financing(
        financing_id="short-subround",
        company="光邮星空",
        announced_at=datetime(2026, 7, 21, tzinfo=BEIJING),
        round_name="Pre-A+轮",
        financing_subtype="round_equity",
        investors=[],
        source_url=shared_sources[0],
        source_urls=shared_sources,
        verification_status=VerificationStatus.VERIFIED,
    )
    incoming = existing.model_copy(
        update={
            "financing_id": "legal-combined",
            "company": "北京光邮星空科技有限公司",
            "announced_at": datetime(2026, 7, 16, tzinfo=BEIJING),
            "round_name": "Pre-A和Pre-A+轮",
            "source_url": shared_sources[1],
        }
    )

    assert _financing_index([existing], incoming) == 0


def test_same_financing_terms_outside_corroboration_window_stay_separate(deps) -> None:
    deps.official_collector.rows = [candidate(), candidate(SECOND_URL)]
    deps.analyzer.results[OFFICIAL_URL] = analysis(
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        title="Space Institute A round",
        published_at=datetime(2026, 3, 1, tzinfo=BEIJING),
    )
    deps.analyzer.results[SECOND_URL] = analysis(
        SECOND_URL,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        title="Space Institute A round repeat",
        published_at=datetime(2026, 7, 21, tzinfo=BEIJING),
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert len(result.state.financings) == 2
    assert len(result.changed_financing_ids) == 2


def test_cross_date_financing_with_insufficient_terms_stays_separate(deps) -> None:
    deps.official_collector.rows = [candidate(), candidate(SECOND_URL)]
    deps.analyzer.results[OFFICIAL_URL] = analysis(
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        published_at=datetime(2026, 3, 1, tzinfo=BEIJING),
    ).model_copy(
        update={"financing_round": None, "amount": None, "investors": []}
    )
    deps.analyzer.results[SECOND_URL] = analysis(
        SECOND_URL,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        published_at=datetime(2026, 7, 21, tzinfo=BEIJING),
    ).model_copy(
        update={"financing_round": None, "amount": None, "investors": []}
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert len(result.state.financings) == 2
    assert len(result.changed_financing_ids) == 2


@pytest.mark.parametrize(
    ("first_update", "second_update"),
    [
        ({"financing_round": "A"}, {"financing_round": "B"}),
        ({"investors": ["Capital One"]}, {"investors": ["Capital Two"]}),
    ],
)
def test_cross_date_financing_with_conflicting_terms_stays_separate(
    deps, first_update: dict[str, object], second_update: dict[str, object]
) -> None:
    deps.official_collector.rows = [candidate(), candidate(SECOND_URL)]
    deps.analyzer.results[OFFICIAL_URL] = analysis(
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        published_at=datetime(2026, 3, 1, tzinfo=BEIJING),
    ).model_copy(update=first_update)
    deps.analyzer.results[SECOND_URL] = analysis(
        SECOND_URL,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        published_at=datetime(2026, 7, 21, tzinfo=BEIJING),
    ).model_copy(update=second_update)

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert len(result.state.financings) == 2
    assert len(result.changed_financing_ids) == 2


def test_financing_change_ids_are_reconstructed_on_same_window_retry(deps) -> None:
    deps.analyzer.results[OFFICIAL_URL] = analysis(
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        title="Space Institute A round",
    )
    pipeline = Pipeline(**deps.as_kwargs())

    first = pipeline.run(NOW)
    second = pipeline.run(NOW)

    assert first.changed_financing_ids == [first.state.financings[0].financing_id]
    assert second.changed_financing_ids == first.changed_financing_ids
    assert first.changed_event_ids == second.changed_event_ids == []
    assert second.state.financings[0].source_published_at == {
        OFFICIAL_URL: datetime(2026, 7, 21, tzinfo=BEIJING)
    }


def test_same_url_changed_content_creates_versioned_event_and_stable_retry(deps) -> None:
    pipeline = Pipeline(**deps.as_kwargs())

    first = pipeline.run(NOW)
    original = first.state.model_dump(mode="json")
    first_event = first.state.events[0]
    first_project = first.state.projects[0]

    rediscovered_at = datetime(2026, 7, 22, 10, 30, tzinfo=BEIJING)
    deps.official_collector.rows = [candidate(discovered_at=rediscovered_at)]
    deps.fetcher.pages[OFFICIAL_URL] = page().model_copy(
        update={"content_hash": "1" * 64, "fetched_at": rediscovered_at}
    )

    changed = pipeline.run(rediscovered_at)

    assert changed.state.model_dump(mode="json") != original
    assert len(changed.state.events) == 2
    assert len({event.content_version_id for event in changed.state.events}) == 2
    assert {event.content_hash for event in changed.state.events} == {"0" * 64, "1" * 64}
    assert changed.state.projects[0].project_id == first_project.project_id
    assert changed.state.projects[0].first_seen_at == first_event.first_seen_at
    assert changed.state.projects[0].updated_at == rediscovered_at
    assert changed.state.projects[0].project_id in changed.changed_project_ids

    stable_before_retry = changed.state.model_dump(mode="json")
    retry = pipeline.run(rediscovered_at)

    assert retry.state.model_dump(mode="json") == stable_before_retry
    assert retry.changed_project_ids == changed.changed_project_ids


def test_degraded_analysis_and_usage_metrics_are_reported(deps) -> None:
    deps.analyzer.results[OFFICIAL_URL] = analysis(degraded=True)
    deps.analyzer.token_increment = 321
    deps.trend_summarizer.token_increment = 20

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.model_coverage_degraded is True
    assert result.metrics.deepseek_tokens == 341
    assert result.metrics.search_api_usage == 1


def test_reused_pipeline_reports_provider_usage_per_run_delta(deps) -> None:
    deps.analyzer.token_increment = 11
    deps.trend_summarizer.token_increment = 7
    pipeline = Pipeline(**deps.as_kwargs())

    first = pipeline.run(NOW)
    second = pipeline.run(NOW)

    assert first.metrics.deepseek_tokens == second.metrics.deepseek_tokens == 18
    assert first.metrics.search_api_usage == second.metrics.search_api_usage == 1
    assert deps.analyzer.deepseek_tokens == 22
    assert deps.trend_summarizer.deepseek_tokens == 14
    assert deps.search_provider.usage_count == 2


def test_shared_primary_analyzer_and_trend_counter_is_not_doubled(deps) -> None:
    class SharedPrimary(FakeAnalyzer):
        def summarize_trends(self, state: StateBundle, window) -> TrendSummary:
            self.deepseek_tokens += 7
            return TrendSummary(
                window_start=window[0],
                window_end=window[1],
                summary="shared trend",
                event_count=len(state.events),
            )

    shared = SharedPrimary()
    shared.token_increment = 11
    deps.analyzer = ResilientAnalyzer(shared, FakeAnalyzer())
    deps.trend_summarizer = shared

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert shared.deepseek_tokens == 18
    assert result.metrics.deepseek_tokens == 18


def test_candidate_dedupe_search_counts_and_official_failures(deps) -> None:
    deps.search_provider.rows = [
        candidate(
            source="bocha",
            title="Laser communication terminal award",
            summary="Laser communication terminal procurement",
            category_hint=Category.LASER_COMMUNICATION,
            source_published_at=NOW,
        )
    ]
    deps.official_collector.rows = [candidate()]
    deps.official_collector.failed_domains = frozenset({"failed.gov.cn"})

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.search_count == 1
    assert result.metrics.candidate_count == 2
    assert result.metrics.official_candidate_count == 1
    assert result.metrics.deduplicated_count == 1
    assert result.metrics.failed_domains == ["failed.gov.cn"]


def test_pipeline_uses_agentic_research_result_and_exposes_trace(deps) -> None:
    item = candidate(
        "https://search.example.cn/agent-result",
        source="bocha",
        title="星间激光通信终端采购公告",
        summary="某研究院发布空间激光通信终端采购项目。",
        category_hint=Category.LASER_COMMUNICATION,
        source_published_at=NOW,
    )
    trace = ResearchTraceItem(
        round_index=1,
        query="星间激光通信终端采购公告 中国 境内",
        category=Category.LASER_COMMUNICATION,
        intent="project_followup",
        result_count=1,
        new_candidate_count=1,
        budget_remaining=7,
        outcome="ok",
    )

    class FakeResearcher:
        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=(item,),
                trace=(trace,),
                budget=12,
                budget_used=5,
                search_count=5,
                agent_round_count=1,
                duplicate_query_count=2,
                degraded=False,
                error_reasons=[],
                stop_reason="model_completed",
            )

    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeResearcher()
    deps.official_collector.rows = []

    result = Pipeline(**arguments).run(NOW)

    assert deps.search_provider.calls == 0
    assert result.metrics.search_count == 5
    assert result.metrics.search_budget == 12
    assert result.metrics.search_budget_used == 5
    assert result.metrics.agent_round_count == 1
    assert result.metrics.duplicate_query_count == 2
    assert result.metrics.agent_stop_reason == "model_completed"
    assert result.research_trace == [
        {
            "round_index": 1,
            "query": trace.query,
            "category": Category.LASER_COMMUNICATION.value,
            "intent": "project_followup",
            "result_count": 1,
            "new_candidate_count": 1,
            "budget_remaining": 7,
            "outcome": "ok",
        }
    ]


def test_pipeline_uses_at_most_three_elastic_queries_and_reverifies_target(deps):
    primary_url = "https://media-a.example/laser-terminal"
    official_followup_url = "https://official.example.cn/notices/laser-terminal"
    primary = candidate(
        primary_url,
        source="search:bocha",
        title="中国境内星间激光通信终端采购公告",
        summary="某研究院发布星间激光通信终端采购招标公告。",
        category_hint=Category.LASER_COMMUNICATION,
        source_published_at=NOW - timedelta(days=2),
    )
    followup = candidate(
        official_followup_url,
        source="search:bocha",
        title="中国境内星间激光通信终端采购公告",
        summary="某研究院发布星间激光通信终端采购招标公告。",
        category_hint=Category.LASER_COMMUNICATION,
        source_published_at=NOW - timedelta(days=2),
    )

    class FakeResearcher:
        deepseek_tokens = 0

        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=(primary,),
                trace=(),
                budget=12,
                budget_used=12,
                search_count=12,
                agent_round_count=2,
                duplicate_query_count=0,
                degraded=False,
                error_reasons=[],
                stop_reason="budget_exhausted",
            )

    class ReverifyAfterFollowup(FakeVerifier):
        def verify(self, result, fetched, corroborating=()):
            related = list(corroborating)
            self.corroborating_by_url[fetched.final_url] = related
            if fetched.final_url == official_followup_url or any(
                other_page.final_url == official_followup_url
                for _other_analysis, other_page in related
            ):
                return VerificationDecision(
                    status=VerificationStatus.VERIFIED,
                    reason="verified_tender",
                    source_grade=SourceGrade.A,
                    evidence=result.evidence,
                )
            return VerificationDecision(
                status=VerificationStatus.PENDING,
                reason="classification_evidence_invalid",
                source_grade=SourceGrade.B,
                evidence=result.evidence,
            )

    deps.official_collector.rows = []
    deps.analyzer.results[primary_url] = analysis(
        primary_url,
        event_type=EventType.TENDER,
        title=primary.title,
    )
    deps.analyzer.results[official_followup_url] = analysis(
        official_followup_url,
        event_type=EventType.TENDER,
        title=followup.title,
    )
    deps.verifier = ReverifyAfterFollowup()
    deps.search_provider.rows = [followup]
    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeResearcher()
    arguments["verification_followup"] = VerificationFollowupPlanner(
        financing_b_domains=("stcn.com", "pedaily.cn", "cls.cn"),
        elastic_budget=3,
        pool_days=90,
        max_targets=1,
    )

    result = Pipeline(**arguments).run(NOW)

    assert result.metrics.search_budget_used == 13
    assert result.metrics.elastic_search_calls == 1
    assert result.metrics.discovery_channel_calls == 4
    assert result.metrics.verification_channel_calls == 9
    assert result.metrics.verification_targets_count == 1
    assert result.metrics.verification_new_source_count == 1
    assert result.metrics.verification_duplicate_source_count == 0
    assert result.metrics.verified_count >= 1
    assert len(result.state.events) >= 1
    elastic_trace = [
        item
        for item in result.research_trace
        if item["intent"] == "verification_elastic"
    ]
    assert len(elastic_trace) == 1
    assert elastic_trace[0]["post_verification_status"] == "verified"


def test_pipeline_keeps_matching_official_result_beyond_selection_limit(deps):
    primary_url = "https://news.qq.com/article/light-post"
    official_url = "https://m.zgccity.com/view/h5/news/204.html"
    primary = candidate(
        primary_url,
        source="search:bocha",
        title="光邮星空完成Pre-A+轮融资",
        summary="光邮星空聚焦空间激光通信并完成Pre-A+轮融资。",
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=NOW - timedelta(days=12),
    )
    official = candidate(
        official_url,
        source="search:bocha",
        title="中关村科学城公司投资光邮星空",
        summary=(
            "中关村科学城公司完成对空间激光通信企业光邮星空Pre-A+轮投资。"
        ),
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=None,
    )
    distracting_rows = [
        candidate(
            f"https://media.example/financing/{index}",
            source="search:bocha",
            title=f"卫星企业{index}完成A轮融资",
            summary=f"卫星企业{index}完成A轮融资并用于卫星制造。",
            category_hint=Category.COMMERCIAL_SPACE_FINANCING,
            source_published_at=NOW - timedelta(days=1),
        )
        for index in range(10)
    ]

    class FakeResearcher:
        deepseek_tokens = 0

        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=(primary,),
                trace=(),
                budget=12,
                budget_used=12,
                search_count=12,
                agent_round_count=2,
                duplicate_query_count=0,
                degraded=False,
                error_reasons=[],
                stop_reason="budget_exhausted",
            )

    class VerifyWithOfficialInvestor(FakeVerifier):
        def verify(self, result, fetched, corroborating=()):
            related = list(corroborating)
            self.corroborating_by_url[fetched.final_url] = related
            if fetched.final_url == official_url or any(
                other_page.final_url == official_url
                for _other_analysis, other_page in related
            ):
                return VerificationDecision(
                    status=VerificationStatus.VERIFIED,
                    reason="verified_financing_official",
                    source_grade=SourceGrade.A,
                    evidence=result.evidence,
                )
            return VerificationDecision(
                status=VerificationStatus.PENDING,
                reason="financing_requires_official_or_two_independent_b_sources",
                source_grade=SourceGrade.C,
                evidence=result.evidence,
            )

    primary_analysis = analysis(
        primary_url,
        event_type=EventType.FINANCING,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        title=primary.title,
        published_at=NOW - timedelta(days=12),
    )
    primary_analysis.organization = "光邮星空"
    primary_analysis.financing_round = "Pre-A+轮"
    primary_analysis.investors = ["中关村科学城", "九合创投"]
    official_analysis = primary_analysis.model_copy(
        update={"source_url": official_url, "title": official.title}
    )

    deps.official_collector.rows = []
    deps.analyzer.results[primary_url] = primary_analysis
    deps.analyzer.results[official_url] = official_analysis
    deps.verifier = VerifyWithOfficialInvestor()
    deps.search_provider.rows = [*distracting_rows, official]
    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeResearcher()
    arguments["verification_followup"] = VerificationFollowupPlanner(
        financing_b_domains=("stcn.com", "pedaily.cn", "cls.cn"),
        official_investor_domains={
            "zgccity.com": [
                "北京中关村科学城创新发展有限公司",
                "中关村科学城公司",
                "中关村科学城",
            ]
        },
        elastic_budget=3,
        pool_days=90,
        max_targets=3,
    )

    result = Pipeline(**arguments).run(NOW)

    assert official_url in deps.fetcher.calls, result.research_trace
    assert result.metrics.elastic_search_calls == 1
    assert result.metrics.verification_new_source_count == 11
    assert result.metrics.verified_count >= 1
    elastic_trace = [
        item
        for item in result.research_trace
        if item["intent"] == "verification_elastic"
    ]
    assert "site:zgccity.com" in elastic_trace[0]["query"]
    assert elastic_trace[0]["allocation_reason"] == "official_source_match"
    assert elastic_trace[0]["preferred_domains"] == ["zgccity.com"]
    assert "中关村科学城" in elastic_trace[0]["matched_aliases"]
    assert elastic_trace[0]["post_verification_status"] == "verified"


def test_pipeline_distributes_empty_followups_two_plus_one(deps):
    first_url = "https://www.stcn.com/article/first"
    second_url = "https://news.qq.com/article/second"
    first = candidate(
        first_url,
        source="search:bocha",
        title="龙擎空天完成Pre-A+轮融资",
        summary="龙擎空天完成Pre-A+轮融资并用于低轨卫星产品研发。",
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=NOW - timedelta(days=2),
    )
    second = candidate(
        second_url,
        source="search:bocha",
        title="谱星航天完成Pre-A轮融资",
        summary="谱星航天完成Pre-A轮融资并用于卫星制造。",
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=NOW - timedelta(days=1),
    )

    class FakeResearcher:
        deepseek_tokens = 0

        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=(first, second),
                trace=(),
                budget=12,
                budget_used=12,
                search_count=12,
                agent_round_count=2,
                duplicate_query_count=0,
                degraded=False,
                error_reasons=[],
                stop_reason="budget_exhausted",
            )

    first_analysis = analysis(
        first_url,
        event_type=EventType.FINANCING,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        title=first.title,
        published_at=NOW - timedelta(days=2),
    )
    first_analysis.organization = "龙擎空天"
    first_analysis.financing_round = "Pre-A+轮"
    second_analysis = analysis(
        second_url,
        event_type=EventType.FINANCING,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        title=second.title,
        published_at=NOW - timedelta(days=1),
    )
    second_analysis.organization = "谱星航天"
    second_analysis.financing_round = "Pre-A轮"

    deps.official_collector.rows = []
    deps.analyzer.results[first_url] = first_analysis
    deps.analyzer.results[second_url] = second_analysis
    deps.verifier.decisions[first_url] = VerificationDecision(
        status=VerificationStatus.PENDING,
        reason="financing_requires_official_or_two_independent_b_sources",
        source_grade=SourceGrade.B,
    )
    deps.verifier.decisions[second_url] = VerificationDecision(
        status=VerificationStatus.PENDING,
        reason="financing_requires_official_or_two_independent_b_sources",
        source_grade=SourceGrade.C,
    )
    deps.search_provider.rows = []
    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeResearcher()
    arguments["verification_followup"] = VerificationFollowupPlanner(
        financing_b_domains=("stcn.com", "pedaily.cn", "cls.cn"),
        elastic_budget=3,
        pool_days=90,
        max_targets=3,
    )

    result = Pipeline(**arguments).run(NOW)

    elastic_trace = [
        item
        for item in result.research_trace
        if item["intent"] == "verification_elastic"
    ]
    assert [item["target_url"] for item in elastic_trace] == [
        first_url,
        second_url,
        first_url,
    ]
    assert [item["allocation_reason"] for item in elastic_trace] == [
        "highest_promotion_potential",
        "cover_distinct_target",
        "retry_same_target",
    ]
    assert result.metrics.elastic_search_calls == 3
    assert result.metrics.verification_targets_count == 2
    assert result.metrics.search_budget_used == 15


def test_pipeline_covers_three_distinct_events_with_three_elastic_calls(deps):
    rows = [
        candidate(
            "https://www.stcn.com/article/longqing",
            source="search:bocha",
            title="龙擎空天完成Pre-A+轮融资",
            summary="龙擎空天完成Pre-A+轮融资并用于低轨卫星产品研发。",
            category_hint=Category.COMMERCIAL_SPACE_FINANCING,
            source_published_at=NOW - timedelta(days=3),
        ),
        candidate(
            "https://news.qq.com/article/puxing",
            source="search:bocha",
            title="谱星航天完成Pre-A轮融资",
            summary="谱星航天完成Pre-A轮融资并用于商业航天卫星制造。",
            category_hint=Category.COMMERCIAL_SPACE_FINANCING,
            source_published_at=NOW - timedelta(days=2),
        ),
        candidate(
            "https://finance.ifeng.com/article/weiguang",
            source="search:bocha",
            title="微光启航完成天使++轮融资",
            summary="微光启航完成天使++轮融资并用于商业航天液体火箭研发。",
            category_hint=Category.COMMERCIAL_SPACE_FINANCING,
            source_published_at=NOW - timedelta(days=1),
        ),
    ]

    class FakeResearcher:
        deepseek_tokens = 0

        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=tuple(rows),
                trace=(),
                budget=12,
                budget_used=12,
                search_count=12,
                agent_round_count=2,
                duplicate_query_count=0,
                degraded=False,
                error_reasons=[],
                stop_reason="budget_exhausted",
            )

    details = (
        ("龙擎空天", "Pre-A+轮", SourceGrade.B),
        ("谱星航天", "Pre-A轮", SourceGrade.C),
        ("微光启航", "天使++轮", SourceGrade.C),
    )
    for row, (organization, round_name, grade) in zip(rows, details):
        row_analysis = analysis(
            row.url,
            event_type=EventType.FINANCING,
            category=Category.COMMERCIAL_SPACE_FINANCING,
            title=row.title,
            published_at=row.source_published_at,
        )
        row_analysis.organization = organization
        row_analysis.financing_round = round_name
        deps.analyzer.results[row.url] = row_analysis
        deps.verifier.decisions[row.url] = VerificationDecision(
            status=VerificationStatus.PENDING,
            reason="financing_requires_official_or_two_independent_b_sources",
            source_grade=grade,
        )

    deps.official_collector.rows = []
    deps.search_provider.rows = []
    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeResearcher()
    arguments["verification_followup"] = VerificationFollowupPlanner(
        financing_b_domains=("stcn.com", "pedaily.cn", "cls.cn"),
        elastic_budget=3,
        pool_days=90,
        max_targets=3,
    )

    result = Pipeline(**arguments).run(NOW)

    elastic_trace = [
        item
        for item in result.research_trace
        if item["intent"] == "verification_elastic"
    ]
    assert len(elastic_trace) == 3
    assert {item["target_url"] for item in elastic_trace} == {
        row.url for row in rows
    }
    assert [item["allocation_reason"] for item in elastic_trace[1:]] == [
        "cover_distinct_target",
        "cover_distinct_target",
    ]
    assert result.metrics.verification_targets_count == 3
    assert result.metrics.search_budget_used == 15


@pytest.mark.parametrize(
    ("prior_no_new", "expected_calls"),
    [(0, 2), (1, 1)],
)
def test_pipeline_stops_single_target_after_two_no_new_queries(
    deps,
    prior_no_new,
    expected_calls,
):
    primary_url = "https://m.pedaily.cn/news/566658"
    primary = candidate(
        primary_url,
        source="search:bocha",
        title="光邮星空连续完成Pre-A和Pre-A+轮融资",
        summary=(
            "北京光邮星空科技有限公司聚焦高速星地激光通信并完成两轮融资，"
            "九合创投领投，同创伟业、中关村科学城跟投。"
        ),
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=NOW - timedelta(days=9),
    )

    class FakeResearcher:
        deepseek_tokens = 0

        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=(primary,),
                trace=(),
                budget=12,
                budget_used=12,
                search_count=12,
                agent_round_count=2,
                duplicate_query_count=0,
                degraded=False,
                error_reasons=[],
                stop_reason="budget_exhausted",
            )

    primary_analysis = analysis(
        primary_url,
        event_type=EventType.FINANCING,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        title=primary.title,
        published_at=NOW - timedelta(days=9),
    )
    primary_analysis.organization = "光邮星空"
    primary_analysis.financing_round = "Pre-A+轮"
    primary_analysis.amount = None
    primary_analysis.amount_disclosed = None
    primary_analysis.investors = []
    primary_analysis.evidence = [
        Evidence(
            field="organization",
            quote="北京光邮星空科技有限公司",
            source_url=primary_url,
        ),
        Evidence(
            field="published_at",
            quote=(NOW - timedelta(days=9)).strftime("%Y年%m月%d日"),
            source_url=primary_url,
        ),
        Evidence(
            field="financing_round",
            quote="Pre-A和Pre-A+轮融资",
            source_url=primary_url,
        ),
    ]

    deps.official_collector.rows = []
    if prior_no_new:
        deps.repository.state = StateBundle(
            pending=[
                PendingItem(
                    item_id="guangyou-pending",
                    title=primary.title,
                    reason="financing_missing_required_evidence",
                    source_url=primary_url,
                    discovered_at=NOW - timedelta(days=1),
                    consecutive_no_new_sources=prior_no_new,
                )
            ]
        )
    deps.analyzer.results[primary_url] = primary_analysis
    deps.verifier.decisions[primary_url] = VerificationDecision(
        status=VerificationStatus.PENDING,
        reason="financing_missing_required_evidence",
        source_grade=SourceGrade.B,
    )
    deps.search_provider.rows = []
    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeResearcher()
    arguments["verification_followup"] = VerificationFollowupPlanner(
        financing_b_domains=("stcn.com", "pedaily.cn", "cls.cn"),
        official_investor_domains={
            "zgccity.com": ["中关村科学城"],
        },
        elastic_budget=3,
        pool_days=90,
        max_targets=3,
        stop_after_no_new=2,
    )

    result = Pipeline(**arguments).run(NOW)

    elastic_trace = [
        item
        for item in result.research_trace
        if item["intent"] == "verification_elastic"
    ]
    assert len(elastic_trace) == expected_calls
    assert result.metrics.elastic_search_calls == expected_calls
    assert result.metrics.search_budget_used == 12 + expected_calls
    assert "site:zgccity.com" in elastic_trace[0]["query"]
    assert "融资金额" in elastic_trace[0]["query"]
    assert "金额未披露" in elastic_trace[0]["query"]
    assert elastic_trace[0]["clue_layers"] == ["candidate"]
    assert elastic_trace[0]["missing_evidence_fields"] == ["amount"]
    if expected_calls == 2:
        assert elastic_trace[1]["allocation_reason"] == "retry_same_target"
    assert elastic_trace[-1]["stop_reason"] == "no_new_source_threshold"
    pending = next(
        item for item in result.state.pending if item.source_url == primary_url
    )
    assert pending.consecutive_no_new_sources == 2


def test_pipeline_date_gap_uses_candidate_date_without_writeback(deps):
    primary_url = "https://www.chinaventure.com.cn/news/date-missing"
    candidate_date = NOW - timedelta(days=15)
    primary = candidate(
        primary_url,
        source="search:bocha",
        title="微光启航完成亿元级天使++轮融资",
        summary=(
            "北京微光启航科技有限公司近日完成亿元级天使++轮融资，"
            "资金用于液体火箭和发动机研制。"
        ),
        category_hint=Category.COMMERCIAL_SPACE_FINANCING,
        source_published_at=candidate_date,
    )

    class FakeResearcher:
        deepseek_tokens = 0

        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=(primary,),
                trace=(),
                budget=12,
                budget_used=12,
                search_count=12,
                agent_round_count=1,
                duplicate_query_count=0,
                degraded=False,
                error_reasons=[],
                stop_reason="budget_exhausted",
            )

    primary_analysis = analysis(
        primary_url,
        event_type=EventType.FINANCING,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        title=primary.title,
        published_at=candidate_date,
    )
    primary_analysis.organization = "微光启航"
    primary_analysis.published_at = None
    primary_analysis.financing_round = "天使++轮"

    deps.official_collector.rows = []
    deps.analyzer.results[primary_url] = primary_analysis
    deps.verifier.decisions[primary_url] = VerificationDecision(
        status=VerificationStatus.PENDING,
        reason="missing_required_fields:published_at",
        source_grade=SourceGrade.B,
    )
    deps.search_provider.rows = []
    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeResearcher()
    arguments["verification_followup"] = VerificationFollowupPlanner(
        financing_b_domains=("stcn.com", "pedaily.cn", "cls.cn"),
        elastic_budget=3,
        pool_days=90,
        max_targets=3,
        stop_after_no_new=2,
    )

    result = Pipeline(**arguments).run(NOW)

    elastic_trace = [
        item
        for item in result.research_trace
        if item["intent"] == "verification_elastic"
    ]
    assert len(elastic_trace) == 2
    assert result.metrics.search_budget_used == 14
    assert elastic_trace[0]["missing_evidence_fields"] == ["published_at"]
    assert "发布日期" in elastic_trace[0]["query"]
    assert "发布时间" in elastic_trace[0]["query"]
    assert "公告时间" in elastic_trace[0]["query"]
    assert primary_analysis.published_at is None
    diagnostic = next(
        item
        for item in result.candidate_diagnostics
        if item.source_url == primary_url
    )
    assert diagnostic.status == "pending"
    assert diagnostic.missing_fields == ["published_at"]
    assert diagnostic.elastic_eligible is True
    assert diagnostic.elastic_attempted is True
    assert diagnostic.elastic_ineligible_reason is None


def test_pipeline_backfill_keeps_relevant_candidates_up_to_90_days(deps) -> None:
    item = candidate(
        "https://search.example.cn/backfill-result",
        source="bocha",
        title="高能激光反无人机系统招标公告",
        summary="某单位发布高能激光反无人机装备采购项目。",
        category_hint=Category.LASER_WEAPON,
        source_published_at=NOW - timedelta(days=60),
    )

    class FakeBackfillResearcher:
        def discover(self, now, projects):
            return AgenticDiscoveryResult(
                candidates=(item,),
                trace=(),
                budget=40,
                budget_used=4,
                search_count=4,
                agent_round_count=0,
                duplicate_query_count=0,
                degraded=False,
                error_reasons=[],
                stop_reason="model_completed",
                mode="backfill",
            )

    arguments = deps.as_kwargs()
    arguments["researcher"] = FakeBackfillResearcher()
    deps.official_collector.rows = []

    result = Pipeline(**arguments).run(NOW)

    assert [row.url for row in result.discovery_candidates] == [item.url]
    assert result.metrics.fallback_window_days == 90
    assert result.metrics.fallback_8_30d_count == 1


def test_pipeline_filters_search_noise_before_fetch_and_marks_information_available(
    deps,
) -> None:
    deps.planner.queries = [
        SimpleNamespace(kind="incremental", text=f"query-{index}")
        for index in range(4)
    ]
    deps.official_collector.rows = []
    deps.search_provider.rows = [
        candidate(
            f"https://search.example.cn/relevant/{index}",
            source="bocha",
            title=f"星间激光通信终端采购公告 {index}",
            summary="空间激光通信终端采购项目",
            category_hint=Category.LASER_COMMUNICATION,
            source_published_at=NOW,
        )
        for index in range(5)
    ]
    deps.search_provider.rows.append(
        candidate(
            "https://search.example.cn/noise",
            source="bocha",
            title="空间激光通信激光打印机采购",
            summary="激光打印机和硒鼓采购项目",
            category_hint=Category.LASER_COMMUNICATION,
            source_published_at=NOW,
        )
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.search_count == 4
    assert result.metrics.raw_search_count == 24
    assert result.metrics.relevance_pass_count == 20
    assert result.metrics.final_candidate_count == 5
    assert result.metrics.information_available is True
    assert [item.url for item in result.discovery_candidates] == [
        f"https://search.example.cn/relevant/{index}" for index in range(5)
    ]
    assert len(deps.fetcher.calls) == 5
    assert all("noise" not in url for url in deps.fetcher.calls)


def test_pipeline_preserves_search_metadata_when_fetch_fails(deps) -> None:
    deps.planner.queries = [
        SimpleNamespace(kind="incremental", text=f"query-{index}")
        for index in range(4)
    ]
    deps.official_collector.rows = []
    source_date = datetime(2026, 7, 21, 8, tzinfo=BEIJING)
    item = candidate(
        "https://search.example.cn/unreachable",
        source="bocha",
        title="高能激光反无人机系统招标",
        summary="激光反无人机装备采购",
        category_hint=Category.LASER_WEAPON,
        source_published_at=source_date,
    )
    deps.search_provider.rows = [item]
    deps.fetcher.errors[item.url] = FetchError("TLS validation failed")

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.fetch_failure_count == 1
    assert result.metrics.information_available is False
    assert len(result.state.pending) == 1
    pending = result.state.pending[0]
    assert pending.category_hint is Category.LASER_WEAPON
    assert pending.source_published_at == source_date
    assert pending.summary == item.summary
    diagnostic = result.candidate_diagnostics[0]
    assert diagnostic.stage == "fetch"
    assert diagnostic.status == "failed"
    assert diagnostic.reason == "fetch_failed"
    assert diagnostic.selected_for_report is True


def test_trend_failure_uses_deterministic_degraded_fallback(deps) -> None:
    deps.trend_summarizer.error = AnalyzerError("model unavailable")
    deps.analyzer.results[OFFICIAL_URL] = analysis(
        published_at=datetime(2026, 7, 22, 8, tzinfo=BEIJING)
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.trend_summary.degraded is True
    assert result.trend_summary.event_count == 1
    assert "award" in result.trend_summary.summary
    assert result.metrics.model_coverage_degraded is True


def test_pending_id_is_stable_and_logs_exclude_secrets_and_body(deps) -> None:
    url = "https://broken.example.cn/notice?id=5&api_key=super-secret"
    deps.official_collector.rows = [candidate(url)]
    deps.fetcher.errors[normalize_url(url)] = FetchError(
        "PRIVATE PAGE BODY super-secret"
    )

    first = Pipeline(**deps.as_kwargs()).run(NOW)
    second = Pipeline(**deps.as_kwargs()).run(NOW)

    assert len(first.state.pending) == len(second.state.pending) == 1
    assert first.state.pending[0].item_id == second.state.pending[0].item_id
    log_text = "\n".join(deps.logger.messages)
    assert "broken.example.cn" in log_text
    assert "super-secret" not in log_text
    assert "PRIVATE PAGE BODY" not in log_text
    assert "api_key" not in log_text
    diagnostic_text = first.candidate_diagnostics[0].model_dump_json()
    assert "super-secret" not in diagnostic_text
    assert "PRIVATE PAGE BODY" not in diagnostic_text
    assert "api_key" not in diagnostic_text


def test_repeated_pending_url_replaces_reason_without_duplicate(deps) -> None:
    deps.repository.state = StateBundle(
        pending=[
            PendingItem(
                item_id="old-id",
                title="Old title",
                reason="source_unavailable",
                source_url=OFFICIAL_URL,
                discovered_at=datetime(2026, 7, 21, tzinfo=BEIJING),
                verification_attempts=2,
                consecutive_no_new_sources=1,
                attempted_queries=["existing verification query"],
            )
        ]
    )
    deps.verifier.decisions[OFFICIAL_URL] = pending_decision(
        "missing_required_fields"
    )

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert len(result.state.pending) == 1
    assert result.state.pending[0].reason == "missing_required_fields"
    assert result.state.pending[0].discovered_at == NOW
    assert result.state.pending[0].verification_attempts == 2
    assert result.state.pending[0].consecutive_no_new_sources == 1
    assert result.state.pending[0].attempted_queries == [
        "existing verification query"
    ]


def test_verified_rerun_removes_resolved_pending_item(deps) -> None:
    deps.verifier.decisions[OFFICIAL_URL] = pending_decision()
    first = Pipeline(**deps.as_kwargs()).run(NOW)
    assert len(first.state.pending) == 1

    deps.verifier.decisions.pop(OFFICIAL_URL)
    second = Pipeline(**deps.as_kwargs()).run(NOW)

    assert second.state.pending == []
    assert len(second.state.events) == 1


def test_repository_commits_once_per_successful_run(deps) -> None:
    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert len(deps.repository.commits) == 1
    assert deps.repository.commits[0] == result.state


def test_programming_type_error_is_not_swallowed_or_committed(deps) -> None:
    deps.analyzer.errors[OFFICIAL_URL] = TypeError("programming bug")

    with pytest.raises(TypeError, match="programming bug"):
        Pipeline(**deps.as_kwargs()).run(NOW)

    assert deps.repository.commits == []


def test_programming_runtime_error_is_not_treated_as_search_degradation(deps) -> None:
    deps.search_provider.error = RuntimeError("quota calculation bug")

    with pytest.raises(RuntimeError, match="quota calculation bug"):
        Pipeline(**deps.as_kwargs()).run(NOW)

    assert deps.repository.commits == []
