"""Resilient, dependency-injected orchestration for one intelligence run."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
import re
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import Field, ValidationError

from .analyzer import AnalyzerError
from .discovery import (
    DiscoveryConfigurationError,
    DiscoveryQuotaError,
    DiscoveryUnavailableError,
    dedupe_candidates,
    normalize_url,
    select_search_candidates,
)
from .fetcher import FetchError
from .matching import (
    content_version_id,
    event_fingerprint,
    financing_fingerprint,
    normalize_text,
    stable_event_id,
    stable_project_id,
)
from .models import (
    AnalysisResult,
    Candidate,
    Category,
    DomainModel,
    Event,
    EventType,
    Financing,
    PendingItem,
    Project,
    RunMetrics,
    StateBundle,
    TrendSummary,
    VerificationStatus,
)
from .timebox import daily_window, rolling_start
from .verification_followup import (
    FollowupTarget,
    pending_reason_allows_followup,
)


class RunResult(DomainModel):
    state: StateBundle
    metrics: RunMetrics
    trend_summary: TrendSummary
    window_start: datetime
    window_end: datetime
    rolling_start: datetime
    changed_event_ids: list[str] = Field(default_factory=list)
    changed_project_ids: list[str] = Field(default_factory=list)
    changed_financing_ids: list[str] = Field(default_factory=list)
    discovery_candidates: list[Candidate] = Field(default_factory=list)
    research_trace: list[dict[str, Any]] = Field(default_factory=list)


_OFFICIAL_COLLECTION_ERRORS = (
    ConnectionError,
    httpx.HTTPError,
    OSError,
    TimeoutError,
    UnicodeError,
)
_CANDIDATE_ERRORS = (
    AnalyzerError,
    ConnectionError,
    FetchError,
    httpx.HTTPError,
    OSError,
    TimeoutError,
    UnicodeError,
    ValidationError,
)
_TREND_ERRORS = (
    AnalyzerError,
    ConnectionError,
    httpx.HTTPError,
    OSError,
    TimeoutError,
    UnicodeError,
    ValidationError,
)

_STAGE_STATUS: dict[EventType, tuple[str, bool]] = {
    EventType.PROCUREMENT_INTENTION: ("upcoming", True),
    EventType.TENDER: ("open", True),
    EventType.INQUIRY: ("open", True),
    EventType.COMPARISON: ("open", True),
    EventType.REBID: ("open", True),
    EventType.CHANGE: ("open", True),
    EventType.EXTENSION: ("open", True),
    EventType.CANDIDATE: ("evaluating", True),
    EventType.AWARD: ("awarded", False),
    EventType.FAILED: ("failed", True),
    EventType.TERMINATION: ("terminated", False),
}

# Independent reports of one financing commonly lag by a few days. Wider gaps are
# treated as possible repeat rounds even when company, round, amount and investors match.
FINANCING_CORROBORATION_WINDOW_DAYS = 7


class Pipeline:
    """Coordinate discovery through persistence without owning external clients."""

    def __init__(
        self,
        *,
        repository: Any,
        planner: Any,
        search_provider: Any,
        official_collector: Any,
        fetcher: Any,
        analyzer: Any,
        verifier: Any,
        matcher: Any,
        trend_summarizer: Any,
        logger: Any,
        researcher: Any | None = None,
        verification_followup: Any | None = None,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._search_provider = search_provider
        self._official_collector = official_collector
        self._fetcher = fetcher
        self._analyzer = analyzer
        self._verifier = verifier
        self._matcher = matcher
        self._trend_summarizer = trend_summarizer
        self._logger = logger
        self._researcher = researcher
        self._verification_followup = verification_followup

    def run(self, now: datetime) -> RunResult:
        deepseek_usage = _usage_snapshot(
            (self._analyzer, self._trend_summarizer, self._researcher),
            "deepseek_tokens",
            "total_tokens",
        )
        search_api_usage = _usage_snapshot(
            (self._search_provider,), "usage_count", "search_api_usage", "usage"
        )
        state = self._repository.load()
        window_start, window_end = daily_window(now)
        rolling_window_start = rolling_start(now)
        metrics = RunMetrics(started_at=now)
        failed_domains: set[str] = set()
        errors: list[str] = []

        research_trace: list[dict[str, Any]] = []
        if self._researcher is not None:
            research = self._researcher.discover(now, state.projects)
            search_rows = list(research.candidates)
            metrics.search_count = research.search_count
            metrics.search_budget = research.budget
            metrics.search_budget_used = research.budget_used
            metrics.discovery_channel_calls = min(4, research.budget_used)
            metrics.verification_channel_calls = max(0, research.budget_used - 4)
            metrics.agent_round_count = research.agent_round_count
            metrics.duplicate_query_count = research.duplicate_query_count
            metrics.agent_search_degraded = research.degraded
            metrics.agent_stop_reason = research.stop_reason
            if research.stop_reason == "model_error":
                metrics.model_coverage_degraded = True
            for reason in research.error_reasons:
                errors.append(f"agentic_discovery:{reason}")
            research_trace = [
                {
                    "round_index": item.round_index,
                    "query": item.query,
                    "category": item.category.value,
                    "intent": item.intent,
                    "result_count": item.result_count,
                    "new_candidate_count": item.new_candidate_count,
                    "budget_remaining": item.budget_remaining,
                    "outcome": item.outcome,
                }
                for item in research.trace
            ]
        else:
            queries = self._planner.plan(now, state.projects)
            search_rows = []
            for query in queries:
                metrics.search_count += 1
                try:
                    search_rows.extend(self._search_provider.search(query))
                except (
                    DiscoveryConfigurationError,
                    DiscoveryQuotaError,
                    DiscoveryUnavailableError,
                ) as error:
                    metrics.search_coverage_degraded = True
                    reason = getattr(error, "reason", "request_rejected")
                    metrics.search_failure_reasons.append(str(reason))
                    errors.append(f"search_api:{reason}")
                    self._safe_log("search_api_failed", error, None)

        official_rows = []
        try:
            official_rows = list(self._official_collector.collect())
        except _OFFICIAL_COLLECTION_ERRORS as error:
            metrics.search_coverage_degraded = True
            errors.append(f"official:{type(error).__name__}")
            self._safe_log("official_collection_failed", error, None)
        collector_failures = getattr(
            self._official_collector, "failed_domains", frozenset()
        )
        failed_domains.update(
            _normalize_domain(str(domain)) for domain in collector_failures
        )
        if collector_failures:
            metrics.search_coverage_degraded = True

        is_backfill = (
            self._researcher is not None
            and getattr(research, "mode", "daily") == "backfill"
        )
        selection = select_search_candidates(
            search_rows,
            now,
            minimum=40 if is_backfill else 5,
            maximum=40 if is_backfill else 10,
            fallback_max_days=90 if is_backfill else 30,
        )
        metrics.raw_search_count = selection.raw_search_count
        metrics.valid_shape_count = selection.valid_shape_count
        metrics.relevance_pass_count = selection.relevance_pass_count
        metrics.recent_7d_count = selection.recent_7d_count
        metrics.fallback_8_30d_count = selection.fallback_8_30d_count
        metrics.fallback_window_days = 90 if is_backfill else 30
        metrics.unknown_date_count = selection.unknown_date_count
        metrics.event_filter_rejected_count = selection.filter_rejected_count
        metrics.event_duplicate_count = selection.event_duplicate_count
        metrics.final_candidate_count = len(selection.candidates)
        metrics.information_available = (
            metrics.search_count >= 4 and metrics.final_candidate_count >= 5
        )

        selected_search_rows = list(selection.candidates)
        verification_pool_rows: list[Candidate] = []
        if self._verification_followup is not None:
            pool_start = now - timedelta(
                days=self._verification_followup.pool_days
            )
            verification_pool_rows = [
                Candidate(
                    title=item.title,
                    url=item.source_url,
                    summary=item.summary,
                    discovered_at=item.discovered_at,
                    discovery_source="verification_pool",
                    category_hint=item.category_hint,
                    source_published_at=item.source_published_at,
                )
                for item in state.pending
                if pending_reason_allows_followup(item.reason)
                and (
                    item.source_published_at or item.discovered_at
                ) >= pool_start
                and (
                    item.source_published_at or item.discovered_at
                ) <= now
            ]
        all_rows = [
            *selected_search_rows,
            *selection.corroborating_candidates,
            *official_rows,
            *verification_pool_rows,
        ]
        candidates = dedupe_candidates(all_rows)
        metrics.candidate_count = len(search_rows) + len(official_rows)
        metrics.official_candidate_count = len(official_rows)
        metrics.deduplicated_count = len(all_rows) - len(candidates)
        metrics.sources_checked = len(candidates)

        events = list(state.events)
        projects = list(state.projects)
        financings = list(state.financings)
        pending_by_id = {item.item_id: item for item in state.pending}
        changed_event_ids: list[str] = []
        changed_project_ids: list[str] = []
        changed_financing_ids: list[str] = []

        fetched_by_url: dict[str, Any] = {}
        for item in candidates:
            try:
                fetched_by_url[item.url] = self._fetcher.fetch(item)
            except _CANDIDATE_ERRORS as error:
                fetched_by_url[item.url] = error
                metrics.fetch_failure_count += 1

        analyzed_by_url: dict[str, Any] = {}
        for item in candidates:
            fetched = fetched_by_url[item.url]
            if isinstance(fetched, BaseException):
                analyzed_by_url[item.url] = fetched
                continue
            try:
                analyzed = self._analyzer.analyze(fetched)
                analyzed_by_url[item.url] = analyzed
                if analyzed.degraded:
                    metrics.model_coverage_degraded = True
            except _CANDIDATE_ERRORS as error:
                analyzed_by_url[item.url] = error

        followup_plans = ()
        followup_target_urls: set[str] = set()
        followup_new_urls: set[str] = set()
        if self._verification_followup is not None:
            prior_pending = {
                normalize_url(item.source_url): item
                for item in state.pending
            }
            targets: list[FollowupTarget] = []
            for item in candidates:
                fetched = fetched_by_url[item.url]
                analyzed = analyzed_by_url[item.url]
                if isinstance(fetched, BaseException) or isinstance(
                    analyzed, BaseException
                ):
                    continue
                try:
                    decision = self._verifier.verify(
                        analyzed,
                        fetched,
                        (
                            (analyzed_by_url[url], page)
                            for url, page in fetched_by_url.items()
                            if url != item.url
                            and not isinstance(page, BaseException)
                            and not isinstance(analyzed_by_url[url], BaseException)
                        ),
                    )
                except _CANDIDATE_ERRORS:
                    continue
                targets.append(
                    FollowupTarget(
                        candidate=item,
                        analysis=analyzed,
                        decision=decision,
                        pending=prior_pending.get(normalize_url(item.url)),
                    )
                )
            followup_plans = self._verification_followup.plan(now, targets)

        if followup_plans:
            metrics.verification_targets_count = len(
                {item.target_url for item in followup_plans}
            )
            metrics.elastic_trigger_reasons = list(
                dict.fromkeys(item.trigger_reason for item in followup_plans)
            )
            followup_target_urls = {
                normalize_url(item.target_url) for item in followup_plans
            }
            followup_rows: list[Candidate] = []
            for planned in followup_plans:
                try:
                    rows = list(
                        self._search_provider.search(
                            planned.query,
                            freshness="oneYear",
                            count=10,
                        )
                    )
                    followup_rows.extend(rows)
                    outcome = "ok"
                except (
                    DiscoveryConfigurationError,
                    DiscoveryQuotaError,
                    DiscoveryUnavailableError,
                ) as error:
                    metrics.search_coverage_degraded = True
                    reason = getattr(error, "reason", "request_rejected")
                    metrics.search_failure_reasons.append(str(reason))
                    errors.append(f"verification_search:{reason}")
                    rows = []
                    outcome = f"error:{reason}"
                metrics.elastic_search_calls += 1
                metrics.search_count += 1
                metrics.search_budget_used += 1
                metrics.verification_channel_calls += 1
                research_trace.append(
                    {
                        "round_index": -1,
                        "query": planned.query.text,
                        "category": planned.query.category.value,
                        "intent": "verification_elastic",
                        "result_count": len(rows),
                        "new_candidate_count": 0,
                        "budget_remaining": (
                            self._verification_followup.elastic_budget
                            - metrics.elastic_search_calls
                        ),
                        "outcome": outcome,
                        "target_url": planned.target_url,
                        "trigger_reason": planned.trigger_reason,
                    }
                )

            followup_limit = min(10, len(followup_rows))
            followup_selection = select_search_candidates(
                followup_rows,
                now,
                minimum=followup_limit,
                maximum=followup_limit,
                fallback_max_days=self._verification_followup.pool_days,
            )
            existing_urls = {normalize_url(item.url) for item in candidates}
            new_candidates = [
                item
                for item in dedupe_candidates(
                    [
                        *followup_selection.candidates,
                        *followup_selection.corroborating_candidates,
                    ]
                )
                if normalize_url(item.url) not in existing_urls
            ]
            followup_new_urls = {
                normalize_url(item.url) for item in new_candidates
            }
            metrics.verification_new_source_count = len(new_candidates)
            metrics.verification_duplicate_source_count = max(
                0, len(followup_rows) - len(new_candidates)
            )
            for trace_item in research_trace:
                if trace_item.get("intent") == "verification_elastic":
                    trace_item["new_candidate_count"] = len(new_candidates)
            candidates.extend(new_candidates)
            metrics.sources_checked = len(candidates)

            for item in new_candidates:
                try:
                    fetched_by_url[item.url] = self._fetcher.fetch(item)
                except _CANDIDATE_ERRORS as error:
                    fetched_by_url[item.url] = error
                    metrics.fetch_failure_count += 1
                    analyzed_by_url[item.url] = error
                    continue
                try:
                    analyzed = self._analyzer.analyze(fetched_by_url[item.url])
                    analyzed_by_url[item.url] = analyzed
                    if analyzed.degraded:
                        metrics.model_coverage_degraded = True
                except _CANDIDATE_ERRORS as error:
                    analyzed_by_url[item.url] = error

        for item in candidates:
            try:
                fetched = fetched_by_url[item.url]
                if isinstance(fetched, BaseException):
                    raise fetched
                result = analyzed_by_url[item.url]
                if isinstance(result, BaseException):
                    raise result
                corroborating = (
                    (analyzed_by_url[url], page)
                    for url, page in fetched_by_url.items()
                    if url != item.url
                    and not isinstance(page, BaseException)
                    and not isinstance(analyzed_by_url[url], BaseException)
                )
                decision = self._verifier.verify(result, fetched, corroborating)
            except _CANDIDATE_ERRORS as error:
                reason = _failure_reason(error)
                self._put_pending(pending_by_id, item, reason, now)
                metrics.pending_count += 1
                hostname = _hostname(item.url)
                if hostname:
                    failed_domains.add(hostname)
                errors.append(f"candidate:{type(error).__name__}:{hostname or 'unknown'}")
                self._safe_log("candidate_failed", error, item.url)
                continue

            if decision.status is not VerificationStatus.PENDING:
                _clear_pending_for_url(pending_by_id, item.url)
            if decision.status is VerificationStatus.REJECTED:
                continue
            if decision.status is VerificationStatus.PENDING:
                self._put_pending(pending_by_id, item, decision.reason, now)
                metrics.pending_count += 1
                continue
            if decision.status is not VerificationStatus.VERIFIED:
                continue
            if not _verified_payload_valid(result):
                self._put_pending(
                    pending_by_id, item, "verified_payload_invalid", now
                )
                metrics.pending_count += 1
                continue

            metrics.verified_count += 1
            if result.category is Category.COMMERCIAL_SPACE_FINANCING:
                financing = _make_financing(result, decision, item, fetched)
                duplicate_index = _financing_index(financings, financing)
                if duplicate_index is None:
                    financings.append(financing)
                    changed_financing_ids.append(financing.financing_id)
                else:
                    existing = financings[duplicate_index]
                    merged = _merge_financing(existing, financing)
                    financings[duplicate_index] = merged
                    if set(merged.source_content_version_ids) != set(
                        existing.source_content_version_ids
                    ):
                        _append_once(changed_financing_ids, merged.financing_id)
                    metrics.deduplicated_count += 1
                continue

            event = _make_event(result, decision, item, fetched)
            if _event_exists(events, event):
                metrics.deduplicated_count += 1
                continue

            match = self._matcher.match(event, projects)
            if match.relation == "suspected":
                self._put_pending(
                    pending_by_id, item, "suspected_project_match", now
                )
                metrics.pending_count += 1
                continue

            previous_events = list(events)
            events.append(event)
            metrics.events_created += 1
            changed_event_ids.append(event.event_id)
            if match.relation == "same_project" and match.project_id:
                index = _project_index(projects, match.project_id)
                if index is None:
                    self._put_pending(
                        pending_by_id, item, "missing_matched_project", now
                    )
                    metrics.pending_count += 1
                    events.pop()
                    metrics.events_created -= 1
                    changed_event_ids.pop()
                    continue
                updated, status_changed = _update_project(
                    projects[index], event, previous_events
                )
                projects[index] = updated
                _append_once(changed_project_ids, updated.project_id)
                if status_changed:
                    metrics.status_update_count += 1
            else:
                project = _new_project(event)
                existing_index = _project_index(projects, project.project_id)
                if existing_index is None:
                    projects.append(project)
                    metrics.new_project_count += 1
                    _append_once(changed_project_ids, project.project_id)
                else:
                    updated, status_changed = _update_project(
                        projects[existing_index], event, previous_events
                    )
                    projects[existing_index] = updated
                    _append_once(changed_project_ids, updated.project_id)
                    if status_changed:
                        metrics.status_update_count += 1

        if followup_target_urls:
            for item_id, pending_item in list(pending_by_id.items()):
                if normalize_url(pending_item.source_url) not in followup_target_urls:
                    continue
                attempted = list(pending_item.attempted_queries)
                for planned in followup_plans:
                    if normalize_url(planned.target_url) == normalize_url(
                        pending_item.source_url
                    ) and planned.query.text not in attempted:
                        attempted.append(planned.query.text)
                pending_by_id[item_id] = pending_item.model_copy(
                    update={
                        "verification_attempts": (
                            pending_item.verification_attempts + 1
                        ),
                        "last_verification_at": now,
                        "consecutive_no_new_sources": (
                            0
                            if followup_new_urls
                            else pending_item.consecutive_no_new_sources + 1
                        ),
                        "attempted_queries": attempted,
                    }
                )

        resulting_state = StateBundle(
            events=sorted(events, key=_event_sort_key),
            projects=sorted(projects, key=lambda project: project.project_id),
            financings=sorted(
                financings,
                key=lambda financing: (
                    financing.fingerprint or financing_fingerprint(financing),
                    financing.financing_id,
                ),
            ),
            pending=sorted(pending_by_id.values(), key=lambda item: item.item_id),
        )
        changed_event_ids = _changed_ids_in_window(
            resulting_state.events,
            changed_event_ids,
            window_start,
            window_end,
        )
        changed_project_ids = _changed_ids_in_window(
            resulting_state.projects,
            changed_project_ids,
            window_start,
            window_end,
        )
        changed_financing_ids = _changed_ids_in_window(
            resulting_state.financings,
            changed_financing_ids,
            window_start,
            window_end,
        )
        try:
            trend_summary = self._trend_summarizer.summarize_trends(
                resulting_state, (rolling_window_start, window_end)
            )
            if trend_summary.degraded:
                metrics.model_coverage_degraded = True
        except _TREND_ERRORS as error:
            metrics.model_coverage_degraded = True
            errors.append(f"trend:{type(error).__name__}")
            self._safe_log("trend_summary_failed", error, None)
            trend_summary = _fallback_trend_summary(
                resulting_state, rolling_window_start, window_end
            )

        metrics.deepseek_tokens = _usage_delta(
            deepseek_usage, "deepseek_tokens", "total_tokens"
        )
        metrics.search_api_usage = _usage_delta(
            search_api_usage, "usage_count", "search_api_usage", "usage"
        )
        metrics.search_failure_reasons = list(
            dict.fromkeys(metrics.search_failure_reasons)
        )
        metrics.failed_domains = sorted(failed_domains)
        metrics.errors = errors
        metrics.finished_at = datetime.now(tz=now.tzinfo)
        self._repository.commit(resulting_state)
        return RunResult(
            state=resulting_state,
            metrics=metrics,
            trend_summary=trend_summary,
            window_start=window_start,
            window_end=window_end,
            rolling_start=rolling_window_start,
            changed_event_ids=changed_event_ids,
            changed_project_ids=changed_project_ids,
            changed_financing_ids=changed_financing_ids,
            discovery_candidates=selected_search_rows,
            research_trace=research_trace,
        )

    @staticmethod
    def _put_pending(
        pending: dict[str, PendingItem], item: Any, reason: str, now: datetime
    ) -> None:
        normalized_url = normalize_url(item.url)
        previous: PendingItem | None = None
        for existing_id, existing in list(pending.items()):
            if normalize_url(existing.source_url) == normalized_url:
                previous = existing
                del pending[existing_id]
        item_id = _pending_id(item.url, reason)
        pending[item_id] = PendingItem(
            item_id=item_id,
            title=item.title,
            summary=str(getattr(item, "summary", "") or ""),
            reason=reason,
            source_url=normalized_url,
            discovered_at=now,
            category_hint=getattr(item, "category_hint", None),
            source_published_at=getattr(item, "source_published_at", None),
            verification_attempts=(
                previous.verification_attempts if previous else 0
            ),
            last_verification_at=(
                previous.last_verification_at if previous else None
            ),
            consecutive_no_new_sources=(
                previous.consecutive_no_new_sources if previous else 0
            ),
            attempted_queries=(
                list(previous.attempted_queries) if previous else []
            ),
        )

    def _safe_log(
        self, code: str, error: BaseException, source_url: str | None
    ) -> None:
        hostname = _hostname(source_url) if source_url else "unknown"
        self._logger.warning(
            f"pipeline_warning code={code} error={type(error).__name__} host={hostname}"
        )


def _make_event(result: AnalysisResult, decision: Any, candidate: Any, page: Any) -> Event:
    version_id = content_version_id(page.final_url, page.content_hash)
    event = Event(
        event_id="pending-stable-id",
        category=cast(Category, result.category),
        title=result.title,
        organization=cast(str, result.organization),
        published_at=cast(datetime, result.published_at),
        source_url=result.source_url,
        source_grade=decision.source_grade,
        verification_status=VerificationStatus.VERIFIED,
        event_type=cast(EventType, result.event_type),
        evidence=list(decision.evidence),
        analysis=result,
        discovered_at=candidate.discovered_at,
        content_hash=page.content_hash,
        content_version_id=version_id,
        first_seen_at=candidate.discovered_at,
        updated_at=candidate.discovered_at,
    )
    return event.model_copy(update={"event_id": stable_event_id(event)})


def _make_financing(
    result: AnalysisResult, decision: Any, candidate: Any, page: Any
) -> Financing:
    amount = _amount_cny(result.amount)
    source_records = list(getattr(decision, "source_records", ()))
    source_urls = sorted(
        {result.source_url, *(record.source_url for record in source_records)}
    )
    source_published_at = {
        record.source_url: record.published_at for record in source_records
    }
    source_published_at.setdefault(
        result.source_url, cast(datetime, result.published_at)
    )
    primary_version = content_version_id(page.final_url, page.content_hash)
    source_content_hashes = {page.final_url: page.content_hash}
    source_content_version_ids = {primary_version}
    normalized_records = []
    for record in source_records:
        record_version = record.content_version_id or content_version_id(
            record.source_url, record.content_hash
        )
        normalized_records.append(
            record.model_copy(update={"content_version_id": record_version})
        )
        source_content_hashes[record.source_url] = record.content_hash
        source_content_version_ids.add(record_version)
    financing = Financing(
        financing_id="pending-stable-id",
        company=cast(str, result.organization),
        announced_at=cast(datetime, result.published_at),
        round_name=result.financing_round,
        financing_subtype=result.financing_subtype,
        amount_cny=amount,
        amount_disclosed=(
            result.amount_disclosed
            if result.amount_disclosed is not None
            else amount is not None
        ),
        investors=list(result.investors),
        business_area=result.business_area,
        source_url=result.source_url,
        source_urls=source_urls,
        source_published_at=source_published_at,
        evidence=sorted(
            decision.evidence,
            key=lambda item: (item.field, item.quote, item.source_url),
        ),
        source_records=normalized_records,
        verification_status=VerificationStatus.VERIFIED,
        discovered_at=candidate.discovered_at,
        content_hash=page.content_hash,
        content_version_id=primary_version,
        source_content_hashes=source_content_hashes,
        source_content_version_ids=sorted(source_content_version_ids),
        first_seen_at=candidate.discovered_at,
        updated_at=candidate.discovered_at,
    )
    fingerprint = financing_fingerprint(financing)
    return financing.model_copy(
        update={
            "financing_id": str(
                uuid5(NAMESPACE_URL, f"laser-space-daily:financing:{fingerprint}")
            ),
            "fingerprint": fingerprint,
        }
    )


def _merge_financing(existing: Financing, incoming: Financing) -> Financing:
    source_urls = sorted(
        {
            existing.source_url,
            incoming.source_url,
            *existing.source_urls,
            *incoming.source_urls,
        }
    )
    evidence_by_key = {
        (item.field, item.quote, item.source_url): item
        for item in [*existing.evidence, *incoming.evidence]
    }
    source_published_at = dict(existing.source_published_at)
    source_published_at.setdefault(existing.source_url, existing.announced_at)
    for source_url, published_at in incoming.source_published_at.items():
        previous = source_published_at.get(source_url)
        if previous is None or _datetime_key(published_at) > _datetime_key(previous):
            source_published_at[source_url] = published_at
    source_records_by_key = {
        (record.source_url, record.content_hash): record
        for record in [*existing.source_records, *incoming.source_records]
    }
    source_content_hashes = {
        **existing.source_content_hashes,
        **incoming.source_content_hashes,
    }
    source_content_version_ids = sorted(
        {
            *existing.source_content_version_ids,
            *incoming.source_content_version_ids,
        }
    )
    has_new_version = set(source_content_version_ids) != set(
        existing.source_content_version_ids
    )
    return existing.model_copy(
        update={
            "source_urls": source_urls,
            "source_published_at": source_published_at,
            "evidence": [evidence_by_key[key] for key in sorted(evidence_by_key)],
            "source_records": [
                source_records_by_key[key] for key in sorted(source_records_by_key)
            ],
            "source_content_hashes": source_content_hashes,
            "source_content_version_ids": source_content_version_ids,
            "updated_at": (
                incoming.discovered_at if has_new_version else existing.updated_at
            ),
        }
    )


def _new_project(event: Event) -> Project:
    status, needs_recheck = _status_for(event.event_type, None)
    analysis = event.analysis
    deadlines, deadline_evidence, deadline_precision = _deadline_state(analysis)
    return Project(
        project_id=stable_project_id(event),
        name=event.title,
        organization=event.organization,
        category=event.category,
        status=status,
        event_ids=[event.event_id],
        project_codes=list(analysis.project_codes) if analysis else [],
        normalized_name=normalize_text(event.title),
        current_stage=event.event_type,
        amount=analysis.amount if analysis else None,
        first_published_at=event.published_at,
        latest_event_at=event.published_at,
        deadlines=deadlines,
        deadline_evidence=deadline_evidence,
        deadline_precision=deadline_precision,
        latest_source_url=event.source_url,
        needs_recheck=needs_recheck,
        year=event.published_at.year,
        first_seen_at=event.first_seen_at,
        updated_at=event.updated_at,
    )


def _update_project(
    project: Project, event: Event, prior_events: list[Event]
) -> tuple[Project, bool]:
    event_by_id = {item.event_id: item for item in prior_events}
    previous_latest = max(
        (event_by_id[item_id] for item_id in project.event_ids if item_id in event_by_id),
        key=_event_sort_key,
        default=None,
    )
    event_ids = sorted(
        {*project.event_ids, event.event_id},
        key=lambda item_id: _event_sort_key(
            event if item_id == event.event_id else event_by_id[item_id]
        ),
    )
    project_codes = sorted(
        {
            *project.project_codes,
            *((event.analysis.project_codes if event.analysis else [])),
        }
    )
    first_published_at = min(
        [
            value
            for value in (project.first_published_at, event.published_at)
            if value is not None
        ],
        key=_datetime_key,
    )
    is_latest = previous_latest is None or _event_sort_key(event) > _event_sort_key(
        previous_latest
    )
    updates: dict[str, Any] = {
        "event_ids": event_ids,
        "project_codes": project_codes,
        "first_published_at": first_published_at,
        "first_seen_at": project.first_seen_at or event.first_seen_at,
        "updated_at": event.updated_at or event.discovered_at or event.published_at,
    }
    status_changed = False
    if is_latest:
        status, needs_recheck = _status_for(event.event_type, project)
        incoming_deadlines, incoming_evidence, incoming_precision = _deadline_state(
            event.analysis
        )
        updates.update(
            {
                "status": status,
                "current_stage": event.event_type,
                "latest_event_at": event.published_at,
                "latest_source_url": event.source_url,
                "needs_recheck": needs_recheck,
                "amount": (
                    event.analysis.amount
                    if event.analysis and event.analysis.amount is not None
                    else project.amount
                ),
                "deadlines": {**project.deadlines, **incoming_deadlines},
                "deadline_evidence": {
                    **project.deadline_evidence,
                    **incoming_evidence,
                },
                "deadline_precision": {
                    **project.deadline_precision,
                    **incoming_precision,
                },
            }
        )
        status_changed = (
            project.status != status
            or project.current_stage is not event.event_type
        )
    return project.model_copy(update=updates), status_changed


def _deadline_state(
    analysis: AnalysisResult | None,
) -> tuple[dict[str, datetime], dict[str, Any], dict[str, str]]:
    if analysis is None:
        return {}, {}, {}
    fields = {
        "registration": analysis.registration_deadline,
        "bid_submission": analysis.bid_submission_deadline,
        "opening": analysis.opening_deadline,
    }
    deadlines = {name: value for name, value in fields.items() if value is not None}
    evidence_by_field = {
        item.field: item
        for item in analysis.evidence
        if item.field.endswith("_deadline")
    }
    deadline_evidence = {
        name: evidence_by_field[f"{name}_deadline"]
        for name in deadlines
        if f"{name}_deadline" in evidence_by_field
    }
    deadline_precision = {
        name: analysis.deadline_precision[name]
        for name in deadlines
        if name in analysis.deadline_precision
    }
    return deadlines, deadline_evidence, deadline_precision


def _status_for(event_type: EventType, project: Project | None) -> tuple[str, bool]:
    status, needs_recheck = _STAGE_STATUS.get(event_type, ("open", True))
    if event_type in {EventType.CHANGE, EventType.EXTENSION} and project is not None:
        status = project.status or "open"
    return status, needs_recheck


def _fallback_trend_summary(
    state: StateBundle, window_start: datetime, window_end: datetime
) -> TrendSummary:
    events = [
        item
        for item in state.events
        if item.verification_status is VerificationStatus.VERIFIED
        and _in_window(item.published_at, window_start, window_end)
    ]
    financings = [
        item
        for item in state.financings
        if item.verification_status is VerificationStatus.VERIFIED
        and _in_window(item.announced_at, window_start, window_end)
    ]
    category_counts = Counter(item.category for item in events)
    if financings:
        category_counts[Category.COMMERCIAL_SPACE_FINANCING] += len(financings)
    status_counts = Counter(item.event_type.value for item in events)
    if financings:
        status_counts[EventType.FINANCING.value] += len(financings)
    count = len(events) + len(financings)
    categories = ", ".join(
        f"{category.value}={value}"
        for category, value in sorted(
            category_counts.items(), key=lambda item: item[0].value
        )
    ) or "none"
    statuses = ", ".join(
        f"{status}={value}" for status, value in sorted(status_counts.items())
    ) or "none"
    return TrendSummary(
        window_start=window_start,
        window_end=window_end,
        summary=f"verified={count}; categories: {categories}; statuses: {statuses}",
        event_count=count,
        category_counts=dict(category_counts),
        degraded=True,
    )


def _pending_id(url: str, reason: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"laser-space-daily:pending:{normalize_url(url)}:{reason}",
        )
    )


def _clear_pending_for_url(pending: dict[str, PendingItem], url: str) -> None:
    normalized_url = normalize_url(url)
    for item_id, item in list(pending.items()):
        if normalize_url(item.source_url) == normalized_url:
            del pending[item_id]


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, FetchError):
        return "fetch_failed"
    if isinstance(error, AnalyzerError):
        return "analysis_failed"
    if isinstance(error, ValidationError):
        return "validation_failed"
    return "network_failed"


def _verified_payload_valid(result: AnalysisResult) -> bool:
    return bool(
        result.in_china
        and result.in_scope
        and result.category is not None
        and result.event_type is not None
        and result.title.strip()
        and result.organization
        and result.organization.strip()
        and result.published_at is not None
    )


def _event_exists(events: list[Event], candidate: Event) -> bool:
    fingerprint = event_fingerprint(candidate)
    return any(
        event.event_id == candidate.event_id
        or event_fingerprint(event) == fingerprint
        for event in events
    )


def _financing_index(
    financings: list[Financing], candidate: Financing
) -> int | None:
    fingerprint = financing_fingerprint(candidate)
    exact = [
        index
        for index, item in enumerate(financings)
        if financing_fingerprint(item) == fingerprint
    ]
    if exact:
        return exact[0]

    candidate_terms = _cross_date_financing_terms(candidate)
    if candidate_terms is None:
        return None
    same_terms = [
        index
        for index, item in enumerate(financings)
        if _cross_date_financing_terms(item) == candidate_terms
        and abs((item.announced_at.date() - candidate.announced_at.date()).days)
        <= FINANCING_CORROBORATION_WINDOW_DAYS
    ]
    return same_terms[0] if len(same_terms) == 1 else None


def _cross_date_financing_terms(
    financing: Financing,
) -> tuple[object, ...] | None:
    company = normalize_text(financing.company)
    round_name = normalize_text(financing.round_name or "")
    subtype = financing.financing_subtype or ("round_equity" if round_name else None)
    investors = tuple(
        sorted({normalize_text(name) for name in financing.investors if name.strip()})
    )
    amount = (
        format(financing.amount_cny, ".15g")
        if financing.amount_cny is not None
        else None
    )
    if not company or not subtype or (subtype == "round_equity" and not round_name):
        return None
    if financing.amount_disclosed and amount is None:
        return None
    if amount is None and not investors:
        return None
    return (
        company,
        subtype,
        round_name,
        financing.amount_disclosed,
        amount,
        investors,
    )


def _project_index(projects: list[Project], project_id: str) -> int | None:
    return next(
        (
            index
            for index, project in enumerate(projects)
            if project.project_id == project_id
        ),
        None,
    )


def _amount_cny(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.replace(",", "").replace("，", "")
    match = re.search(r"(\d+(?:\.\d+)?)", normalized)
    if not match:
        return None
    amount = float(match.group(1))
    if "亿" in normalized:
        amount *= 100_000_000
    elif "万" in normalized:
        amount *= 10_000
    return amount


def _usage_value(value: Any, *names: str) -> int:
    for name in names:
        raw = getattr(value, name, None)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return max(0, int(raw))
        if isinstance(raw, dict):
            total = raw.get("total_tokens", raw.get("usage", 0))
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                return max(0, int(total))
    return 0


def _usage_snapshot(values: tuple[Any, ...], *names: str) -> list[tuple[Any, int]]:
    snapshots: list[tuple[Any, int]] = []
    seen: set[int] = set()
    for value in values:
        source = _usage_source(value, names)
        if source is None or id(source) in seen:
            continue
        seen.add(id(source))
        snapshots.append((source, _usage_value(source, *names)))
    return snapshots


def _usage_source(value: Any, names: tuple[str, ...]) -> Any | None:
    current = value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        primary = getattr(current, "_primary", None)
        if primary is not None and any(hasattr(primary, name) for name in names):
            current = primary
            continue
        return current if any(hasattr(current, name) for name in names) else None
    return None


def _usage_delta(snapshots: list[tuple[Any, int]], *names: str) -> int:
    return sum(
        max(0, _usage_value(source, *names) - before)
        for source, before in snapshots
    )


def _hostname(url: str | None) -> str:
    if not url:
        return ""
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _normalize_domain(value: str) -> str:
    return value.lower().strip().rstrip(".")


def _datetime_key(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _event_sort_key(event: Event) -> tuple[datetime, str]:
    return _datetime_key(event.published_at), event.event_id


def _in_window(value: datetime, start: datetime, end: datetime) -> bool:
    comparable = _datetime_key(value)
    return _datetime_key(start) <= comparable <= _datetime_key(end)


def _changed_ids_in_window(
    items: list[Any],
    current_ids: list[str],
    start: datetime,
    end: datetime,
) -> list[str]:
    changed = set(current_ids)
    for item in items:
        timestamps = (item.first_seen_at, item.updated_at)
        if any(
            value is not None and _in_window(value, start, end)
            for value in timestamps
        ):
            item_id = getattr(
                item,
                "event_id",
                getattr(item, "project_id", getattr(item, "financing_id", "")),
            )
            if item_id:
                changed.add(item_id)
    return [
        getattr(
            item,
            "event_id",
            getattr(item, "project_id", getattr(item, "financing_id", "")),
        )
        for item in items
        if getattr(
            item,
            "event_id",
            getattr(item, "project_id", getattr(item, "financing_id", "")),
        )
        in changed
    ]


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
