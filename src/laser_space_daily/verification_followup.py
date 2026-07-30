"""Deterministic selection and query planning for near-verification candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from collections.abc import Iterable, Mapping
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
        "classification_country_evidence_invalid",
        "classification_category_evidence_invalid",
        "classification_event_evidence_invalid",
        "classification_scope_evidence_invalid",
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
    target_key: str = ""
    allocation_reason: str = "highest_promotion_potential"
    preferred_domains: tuple[str, ...] = ()
    target_terms: tuple[str, ...] = ()
    matched_aliases: tuple[str, ...] = ()
    clue_layers: tuple[str, ...] = ()


@dataclass(frozen=True)
class OfficialSearchClue:
    domains: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    layers: tuple[str, ...] = ()


class VerificationFollowupPlanner:
    """Spend only the elastic budget on deterministic, high-potential follow-ups."""

    def __init__(
        self,
        *,
        financing_b_domains: Iterable[str],
        official_company_domains: Mapping[str, str | Iterable[str]] | None = None,
        official_investor_domains: Mapping[str, str | Iterable[str]] | None = None,
        elastic_budget: int = 3,
        pool_days: int = 90,
        max_targets: int = 3,
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
        self._official_company_domains = _normalize_domain_aliases(
            official_company_domains or {}
        )
        self._official_investor_domains = _normalize_domain_aliases(
            official_investor_domains or {}
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

    @property
    def stop_after_no_new(self) -> int:
        return self._stop_after_no_new

    def plan(
        self,
        now: datetime,
        targets: Iterable[FollowupTarget],
    ) -> tuple[PlannedFollowup, ...]:
        target_rows = tuple(targets)
        attempted_queries: list[str] = []
        targeted_urls: list[str] = []
        planned: list[PlannedFollowup] = []
        while len(planned) < self._elastic_budget:
            next_item = self.plan_next(
                now,
                target_rows,
                attempted_queries=attempted_queries,
                targeted_urls=targeted_urls,
            )
            if next_item is None:
                break
            planned.append(next_item)
            attempted_queries.append(next_item.query.text)
            targeted_urls.append(next_item.target_key)
        return tuple(planned)

    def plan_next(
        self,
        now: datetime,
        targets: Iterable[FollowupTarget],
        *,
        attempted_queries: Iterable[str] = (),
        targeted_urls: Iterable[str] = (),
        no_new_counts: Mapping[str, int] | None = None,
    ) -> PlannedFollowup | None:
        if now.tzinfo is None:
            raise ValueError("verification planning time must include timezone")
        ranked = sorted(
            (
                target
                for target in targets
                if self._eligible(now, target, no_new_counts or {})
            ),
            key=self._sort_key,
        )
        eligible: list[FollowupTarget] = []
        seen_target_keys: set[str] = set()
        for target in ranked:
            target_key = self._target_key(target)
            if target_key in seen_target_keys:
                continue
            seen_target_keys.add(target_key)
            eligible.append(target)
            if len(eligible) >= self._max_targets:
                break
        if not eligible or self._elastic_budget == 0:
            return None

        run_attempted = {_normalize_query(item) for item in attempted_queries}
        targeted = set(targeted_urls)
        untried_targets = [
            target
            for target in eligible
            if self._target_key(target) not in targeted
        ]
        prefer_distinct = bool(targeted) and len(targeted) < min(2, self._max_targets)
        ordered = untried_targets if prefer_distinct and untried_targets else eligible

        for target in ordered:
            persisted_attempts = {
                _normalize_query(item)
                for item in (target.pending.attempted_queries if target.pending else ())
            }
            for query in self._queries(target):
                normalized = _normalize_query(query.text)
                if normalized in run_attempted or normalized in persisted_attempts:
                    continue
                clue = self._official_search_clue(target)
                target_key = self._target_key(target)
                is_distinct = target_key not in targeted
                is_switch = bool(targeted) and is_distinct
                return PlannedFollowup(
                    target_url=target.candidate.url,
                    trigger_reason=target.decision.reason,
                    query=query,
                    target_key=target_key,
                    allocation_reason=(
                        "cover_distinct_target"
                        if is_switch
                        else (
                            "retry_same_target"
                            if not is_distinct
                            else (
                                "official_source_match"
                                if clue.domains
                                else "highest_promotion_potential"
                            )
                        )
                    ),
                    preferred_domains=clue.domains,
                    target_terms=tuple(
                        value
                        for value in (
                            target.analysis.organization,
                            target.analysis.financing_round,
                        )
                        if value
                    ),
                    matched_aliases=clue.aliases,
                    clue_layers=clue.layers,
                )
        return None

    def _eligible(
        self,
        now: datetime,
        target: FollowupTarget,
        no_new_counts: Mapping[str, int] | None = None,
    ) -> bool:
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
        target_key = self._target_key(target)
        effective_no_new = (no_new_counts or {}).get(
            target_key,
            target.pending.consecutive_no_new_sources if target.pending else 0,
        )
        return not (
            effective_no_new >= self._stop_after_no_new
        )

    def _sort_key(self, target: FollowupTarget) -> tuple[object, ...]:
        published_at = target.analysis.published_at or target.candidate.source_published_at
        attempts = target.pending.verification_attempts if target.pending else 0
        missing_fields = sum(
            value in (None, "", [])
            for value in (
                target.analysis.organization,
                target.analysis.published_at
                or target.candidate.source_published_at,
                target.analysis.category,
                target.analysis.event_type,
                target.analysis.financing_round or target.analysis.amount,
            )
        )
        source_gap_rank = {
            "financing_requires_official_or_two_independent_b_sources": 0,
            "financing_requires_independent_sources": 0,
            "financing_corroboration_insufficient": 0,
            "classification_country_evidence_invalid": 1,
            "classification_category_evidence_invalid": 1,
            "classification_event_evidence_invalid": 1,
            "classification_scope_evidence_invalid": 1,
            "classification_evidence_missing": 2,
            "classification_evidence_invalid": 2,
            "classification_rule_disagreement": 3,
            "financing_corroboration_conflict": 4,
        }.get(target.decision.reason, 5)
        grade_rank = {
            SourceGrade.B: 0,
            SourceGrade.A: 1,
            SourceGrade.C: 2,
        }[target.decision.source_grade]
        return (
            0 if self._official_search_clue(target).domains else 1,
            missing_fields,
            source_gap_rank,
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
            investors = " ".join(
                _query_value(value)
                for value in analysis.investors[:3]
                if _query_value(value)
            )
            investor_terms = (
                f"{investors} 投资方" if investors else "领投方 投资方"
            )
            official_domains = self._official_search_clue(target).domains
            official_suffix = (
                f" site:{official_domains[0]}" if official_domains else ""
            )
            raw = [
                f"{event_terms} 官网 投资机构 官方披露{official_suffix}",
                f"{event_terms} {investor_terms}",
                f"{event_terms} 新闻 报道",
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

    def _official_search_clue(
        self,
        target: FollowupTarget,
    ) -> OfficialSearchClue:
        analysis = target.analysis
        matches: list[tuple[str, str, str]] = []
        for domain, aliases in self._official_company_domains.items():
            alias = _matching_alias(analysis.organization or "", aliases)
            if alias:
                matches.append((domain, alias, "structured"))
        for domain, aliases in self._official_investor_domains.items():
            for investor in analysis.investors:
                alias = _matching_alias(investor, aliases)
                if alias:
                    matches.append((domain, alias, "structured"))

        evidence_text = "\n".join(
            item.quote
            for item in analysis.evidence
            if item.source_url == analysis.source_url
        )
        candidate_text = f"{target.candidate.title}\n{target.candidate.summary}"
        for domain, aliases in self._official_investor_domains.items():
            alias = _matching_alias_in_text(evidence_text, aliases)
            if alias:
                matches.append((domain, alias, "evidence"))
            alias = _matching_alias_in_text(candidate_text, aliases)
            if alias:
                matches.append((domain, alias, "candidate"))
        for domain, aliases in self._official_company_domains.items():
            alias = _matching_alias_in_text(evidence_text, aliases)
            if alias:
                matches.append((domain, alias, "evidence"))
            alias = _matching_alias_in_text(candidate_text, aliases)
            if alias:
                matches.append((domain, alias, "candidate"))

        return OfficialSearchClue(
            domains=tuple(dict.fromkeys(domain for domain, _alias, _layer in matches)),
            aliases=tuple(dict.fromkeys(alias for _domain, alias, _layer in matches)),
            layers=tuple(dict.fromkeys(layer for _domain, _alias, layer in matches)),
        )

    @staticmethod
    def _target_key(target: FollowupTarget) -> str:
        analysis = target.analysis
        values = (
            analysis.organization or target.candidate.title,
            analysis.category.value if analysis.category else "",
            analysis.event_type.value if analysis.event_type else "",
            analysis.financing_round or "",
        )
        return "|".join(_normalize_name(value) for value in values)


def _query_value(value: str) -> str:
    return " ".join(value.replace('"', " ").replace("“", " ").replace("”", " ").split())


def _normalize_query(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _normalize_domain_aliases(
    rows: Mapping[str, str | Iterable[str]],
) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for domain, value in rows.items():
        aliases = (value,) if isinstance(value, str) else tuple(value)
        clean_aliases = tuple(
            dict.fromkeys(alias.strip() for alias in aliases if alias.strip())
        )
        clean_domain = domain.strip().lower().rstrip(".")
        if clean_domain and clean_aliases:
            normalized[clean_domain] = clean_aliases
    return normalized


def _matching_alias(value: str, aliases: Iterable[str]) -> str | None:
    normalized_value = _normalize_name(value)
    if not normalized_value:
        return None
    matches = [
        (
            0 if normalized_alias == normalized_value else 1,
            len(alias),
            alias,
        )
        for alias in aliases
        if (normalized_alias := _normalize_name(alias))
        and (
            normalized_alias in normalized_value
            or normalized_value in normalized_alias
        )
    ]
    return min(matches)[2] if matches else None


def _matching_alias_in_text(text: str, aliases: Iterable[str]) -> str | None:
    normalized_text = _normalize_name(text)
    if not normalized_text:
        return None
    matches = [
        (len(alias), alias)
        for alias in aliases
        if (normalized_alias := _normalize_name(alias))
        and normalized_alias in normalized_text
    ]
    return min(matches)[1] if matches else None


def _normalize_name(value: str) -> str:
    return re.sub(
        r"(有限责任公司|股份有限公司|有限公司|公司)$",
        "",
        re.sub(r"[\s·•（）()“”\"']", "", value).casefold(),
    )


def _url_matches_domain(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    normalized_domain = domain.lower().rstrip(".")
    return bool(
        host
        and normalized_domain
        and (host == normalized_domain or host.endswith(f".{normalized_domain}"))
    )
