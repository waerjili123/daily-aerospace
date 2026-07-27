from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from laser_space_daily.fetcher import FetchError, FetchedPage
from laser_space_daily.analyzer import AnalyzerError, ResilientAnalyzer
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
    PendingItem,
    Project,
    SourceGrade,
    SourceRecord,
    StateBundle,
    TrendSummary,
    VerificationStatus,
)
from laser_space_daily.pipeline import Pipeline
from laser_space_daily.report import ReportRenderer
from laser_space_daily.verifier import VerificationDecision


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

    def search(self, query):
        self.calls += 1
        self.usage_count += 1
        if self.error:
            raise self.error
        return list(self.rows)


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
    deps.official_collector.rows = [candidate(), unreachable]
    deps.verifier.decisions[SECOND_URL] = pending_decision()

    result = Pipeline(**deps.as_kwargs()).run(NOW)

    assert result.metrics.verified_count == 1
    assert result.metrics.pending_count == 1
    assert len(result.state.events) == 1
    assert result.state.pending[0].reason == "source_unavailable"
    assert result.state.pending[0].summary == "Search-provider fallback summary"


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
    financing_section = ReportRenderer(18000).render(result).markdown.split(
        "## 商业航天融资", maxsplit=1
    )[1].split("## 今日重点跟进", maxsplit=1)[0]
    assert "Space Institute" in financing_section
    assert OFFICIAL_URL in financing_section
    assert SECOND_URL in financing_section


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


def test_repeated_pending_url_replaces_reason_without_duplicate(deps) -> None:
    deps.repository.state = StateBundle(
        pending=[
            PendingItem(
                item_id="old-id",
                title="Old title",
                reason="source_unavailable",
                source_url=OFFICIAL_URL,
                discovered_at=datetime(2026, 7, 21, tzinfo=BEIJING),
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
