"""Deterministic selection and query planning for near-verification candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Iterable
from urllib.parse import urlsplit

from .discovery import SearchQuery
from .models import (
    AnalysisResult,
    Candidate,
    Category,
    PendingItem,
    SourceGrade,
    VerificationStatus,
)
from .verifier import VerificationDecision


_ELIGIBLE_REASONS = frozenset(
    {
        "financing_requires_official_or_two_independent_b_sources",
        "financing_requires_independent_sources",
        "financing_corroboration_insufficient",
        "financing_corroboration_conflict",
        "classification_evidence_missing",
        "classification_evidence_invalid",
        "classification_rule_disagreement",
    }
)


def pending_reason_allows_followup(reason: str) -> bool:
    return reason in _ELIGIBLE_REASONS


@dataclass(frozen=True)
class FollowupTarget:
    candidate: Candidate
    analysis: AnalysisResult
    decision: VerificationDecision
    pending: PendingItem | None = None


@dataclass(frozen=True)
class PlannedFollowup:
    target_url: str
    trigger_reason: str
    query: SearchQuery


class VerificationFollowupPlanner:
    """Spend only the elastic budget on deterministic, high-potential follow-ups."""

    def __init__(
        self,
        *,
        financing_b_domains: Iterable[str],
        elastic_budget: int = 3,
        pool_days: int = 90,
        max_targets: int = 1,
        stop_after_no_new: int = 2,
    ) -> None:
        if not 0 <= elastic_budget <= 3:
            raise ValueError("verification elastic budget must be between 0 and 3")
        if not 30 <= pool_days <= 90:
            raise ValueError("verification pool must be between 30 and 90 days")
        if not 1 <= max_targets <= 3:
            raise ValueError("verification max targets must be between 1 and 3")
        if stop_after_no_new < 1:
            raise ValueError("verification stop threshold must be positive")
        self._financing_b_domains = tuple(
            dict.fromkeys(domain.strip().lower().rstrip(".") for domain in financing_b_domains)
        )
        self._elastic_budget = elastic_budget
        self._pool_days = pool_days
        self._max_targets = max_targets
        self._stop_after_no_new = stop_after_no_new

    @property
    def elastic_budget(self) -> int:
        return self._elastic_budget

    @property
    def pool_days(self) -> int:
        return self._pool_days

    def plan(
        self,
        now: datetime,
        targets: Iterable[FollowupTarget],
    ) -> tuple[PlannedFollowup, ...]:
        if now.tzinfo is None:
            raise ValueError("verification planning time must include timezone")
        eligible = sorted(
            (target for target in targets if self._eligible(now, target)),
            key=self._sort_key,
        )[: self._max_targets]
        if not eligible or self._elastic_budget == 0:
            return ()

        planned: list[PlannedFollowup] = []
        seen_queries: set[str] = set()
        for target in eligible:
            attempted = {
                _normalize_query(item)
                for item in (target.pending.attempted_queries if target.pending else ())
            }
            for query in self._queries(target):
                normalized = _normalize_query(query.text)
                if normalized in seen_queries or normalized in attempted:
                    continue
                seen_queries.add(normalized)
                planned.append(
                    PlannedFollowup(
                        target_url=target.candidate.url,
                        trigger_reason=target.decision.reason,
                        query=query,
                    )
                )
                if len(planned) >= self._elastic_budget:
                    return tuple(planned)
        return tuple(planned)

    def _eligible(self, now: datetime, target: FollowupTarget) -> bool:
        analysis = target.analysis
        published_at = analysis.published_at or target.candidate.source_published_at
        if (
            target.decision.status is not VerificationStatus.PENDING
            or target.decision.reason not in _ELIGIBLE_REASONS
            or not analysis.in_china
            or not analysis.in_scope
            or analysis.category is None
            or analysis.event_type is None
            or not analysis.organization
            or published_at is None
            or published_at < now - timedelta(days=self._pool_days)
            or published_at > now
        ):
            return False
        return not (
            target.pending is not None
            and target.pending.consecutive_no_new_sources
            >= self._stop_after_no_new
        )

    @staticmethod
    def _sort_key(target: FollowupTarget) -> tuple[object, ...]:
        grade_rank = {
            SourceGrade.B: 0,
            SourceGrade.A: 1,
            SourceGrade.C: 2,
        }[target.decision.source_grade]
        published_at = target.analysis.published_at or target.candidate.source_published_at
        attempts = target.pending.verification_attempts if target.pending else 0
        return (
            grade_rank,
            attempts,
            -published_at.timestamp() if published_at is not None else 0,
            target.candidate.url,
        )

    def _queries(self, target: FollowupTarget) -> tuple[SearchQuery, ...]:
        analysis = target.analysis
        organization = _query_value(analysis.organization or "")
        category = analysis.category or target.candidate.category_hint
        if category is None:
            return ()
        if category is Category.COMMERCIAL_SPACE_FINANCING:
            round_name = _query_value(analysis.financing_round or "")
            event_terms = " ".join(value for value in (organization, round_name, "融资") if value)
            raw = [
                f"{event_terms} 官网 投资机构 官方披露",
                *(
                    f"site:{domain} {event_terms}"
                    for domain in self._financing_b_domains
                    if not _url_matches_domain(target.candidate.url, domain)
                ),
            ]
        else:
            title = _query_value(analysis.title)
            event_terms = " ".join(value for value in (organization, title) if value)
            raw = (
                f"{event_terms} 官方公告",
                f"{event_terms} 项目编号",
                f"{event_terms} 中标 变更 延期 终止",
            )
        return tuple(
            SearchQuery(
                kind="project_followup",
                text=f"{text} 最近90天 中国境内",
                category=category,
            )
            for text in raw
        )


def _query_value(value: str) -> str:
    return " ".join(value.replace('"', " ").replace("“", " ").replace("”", " ").split())


def _normalize_query(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _url_matches_domain(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    normalized_domain = domain.lower().rstrip(".")
    return bool(
        host
        and normalized_domain
        and (host == normalized_domain or host.endswith(f".{normalized_domain}"))
    )
