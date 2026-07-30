"""Deterministic selection and query planning for near-verification candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import unicodedata
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
from .timebox import BEIJING_TIMEZONE
from .verifier import VerificationDecision, financing_evidence_gaps


_ELIGIBLE_REASONS = frozenset(
    {
        "financing_requires_official_or_two_independent_b_sources",
        "financing_requires_independent_sources",
        "financing_corroboration_insufficient",
        "financing_corroboration_conflict",
        "financing_missing_required_evidence",
        "missing_required_fields:published_at",
        "classification_evidence_missing",
        "classification_evidence_invalid",
        "classification_country_evidence_invalid",
        "classification_category_evidence_invalid",
        "classification_event_evidence_invalid",
        "classification_scope_evidence_invalid",
        "classification_rule_disagreement",
    }
)
_PLANNING_REFERENCE = datetime(2000, 1, 1, tzinfo=BEIJING_TIMEZONE)


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
    missing_evidence_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class OfficialSearchClue:
    domains: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    layers: tuple[str, ...] = ()


@dataclass(frozen=True)
class FollowupEligibility:
    eligible: bool
    reason: str


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

    @property
    def financing_b_domains(self) -> tuple[str, ...]:
        return self._financing_b_domains

    def event_key(
        self,
        target: FollowupTarget,
        peers: Iterable[FollowupTarget] = (),
    ) -> str:
        group = [
            item
            for item in (target, *tuple(peers))
            if _same_verification_event(target, item)
        ]
        return _verification_event_group_key(group)

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
            (target for target in targets if self._eligible(now, target, {})),
            key=self._sort_key,
        )
        eligible_groups: list[tuple[FollowupTarget, str]] = []
        grouped_targets: list[list[FollowupTarget]] = []
        for target in ranked:
            existing_group = next(
                (
                    group
                    for group in grouped_targets
                    if _same_verification_event(target, group[0])
                ),
                None,
            )
            if existing_group is not None:
                existing_group.append(target)
                continue
            grouped_targets.append([target])
        for group in grouped_targets:
            target_key = _verification_event_group_key(group)
            persisted_no_new = max(
                (
                    item.pending.consecutive_no_new_sources
                    for item in group
                    if item.pending is not None
                ),
                default=0,
            )
            effective_no_new = (no_new_counts or {}).get(
                target_key,
                persisted_no_new,
            )
            if effective_no_new < self._stop_after_no_new:
                eligible_groups.append((group[0], target_key))
                if len(eligible_groups) >= self._max_targets:
                    break
        if not eligible_groups or self._elastic_budget == 0:
            return None

        run_attempted = {_normalize_query(item) for item in attempted_queries}
        targeted = set(targeted_urls)
        untried_groups = [
            item
            for item in eligible_groups
            if item[1] not in targeted
        ]
        prefer_distinct = bool(targeted) and len(targeted) < self._max_targets
        ordered = (
            untried_groups
            if prefer_distinct and untried_groups
            else eligible_groups
        )

        for target, target_key in ordered:
            persisted_attempts = {
                _normalize_query(item)
                for item in (target.pending.attempted_queries if target.pending else ())
            }
            for query in self._queries(target):
                normalized = _normalize_query(query.text)
                if normalized in run_attempted or normalized in persisted_attempts:
                    continue
                clue = self._official_search_clue(target)
                missing_evidence_fields = _target_evidence_gaps(target)
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
                    missing_evidence_fields=missing_evidence_fields,
                )
        return None

    def _eligible(
        self,
        now: datetime,
        target: FollowupTarget,
        no_new_counts: Mapping[str, int] | None = None,
    ) -> bool:
        return self.eligibility(now, target, no_new_counts).eligible

    def eligibility(
        self,
        now: datetime,
        target: FollowupTarget,
        no_new_counts: Mapping[str, int] | None = None,
    ) -> FollowupEligibility:
        if now.tzinfo is None:
            raise ValueError("verification planning time must include timezone")
        analysis = target.analysis
        if target.decision.status is not VerificationStatus.PENDING:
            return FollowupEligibility(False, "status_not_pending")
        if target.decision.reason not in _ELIGIBLE_REASONS:
            return FollowupEligibility(False, "reason_not_supported")
        if (
            not analysis.in_china
            or not analysis.in_scope
            or analysis.category is None
            or analysis.event_type is None
        ):
            return FollowupEligibility(False, "classification_incomplete")
        if not analysis.organization:
            return FollowupEligibility(False, "organization_missing")
        published_at = _planning_datetime(
            analysis.published_at or target.candidate.source_published_at,
            now,
        )
        if (
            published_at is None
            or published_at < now - timedelta(days=self._pool_days)
            or published_at > now
        ):
            return FollowupEligibility(False, "published_at_outside_pool")
        target_key = self._target_key(target)
        effective_no_new = (no_new_counts or {}).get(
            target_key,
            target.pending.consecutive_no_new_sources if target.pending else 0,
        )
        if effective_no_new >= self._stop_after_no_new:
            return FollowupEligibility(False, "no_new_source_threshold")
        return FollowupEligibility(True, "eligible")

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
            "financing_missing_required_evidence": 1,
            "missing_required_fields:published_at": 1,
        }.get(target.decision.reason, 5)
        grade_rank = {
            SourceGrade.B: 0,
            SourceGrade.A: 1,
            SourceGrade.C: 2,
        }[target.decision.source_grade]
        evidence_gaps = _target_evidence_gaps(target)
        gap_rank = (
            0
            if target.decision.reason == "missing_required_fields:published_at"
            else (1 if len(evidence_gaps) <= 1 else 2)
        )
        return (
            0 if self._official_search_clue(target).domains else 1,
            gap_rank,
            missing_fields,
            source_gap_rank,
            grade_rank,
            attempts,
            -_planning_datetime(published_at, _PLANNING_REFERENCE).timestamp()
            if published_at is not None
            else 0,
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
            gap_terms = _gap_query_terms(_target_evidence_gaps(target))
            used_domains = {
                domain
                for source_url in (
                    target.candidate.url,
                    analysis.source_url,
                    *(item.source_url for item in analysis.evidence),
                )
                if (domain := _registered_source_domain(
                    source_url, self._financing_b_domains
                ))
            }
            untried_b_domains = [
                domain
                for domain in self._financing_b_domains
                if domain not in used_domains
            ]
            raw = []
            if official_domains:
                raw.append(
                    f"{event_terms} {gap_terms} 官网 投资机构 官方披露"
                    f"{official_suffix}"
                )
            raw.extend(
                f"{event_terms} {gap_terms} {investor_terms} site:{domain}"
                for domain in untried_b_domains
            )
            raw.extend(
                (
                    f"{event_terms} {gap_terms} {investor_terms}",
                    f"{event_terms} {gap_terms} 新闻 报道",
                    f"{event_terms} {gap_terms} 权威媒体 融资公告",
                )
            )
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
        return _verification_event_group_key([target])


_VERIFICATION_ROUND = re.compile(
    r"(?i)(pre[\s-]?[a-d]\+{0,2}|[a-d]\+{0,2}|"
    r"天使\+{0,2}|种子|战略投资|战略)"
)


def _planning_datetime(
    value: datetime | None,
    reference: datetime,
) -> datetime | None:
    if value is None:
        return None
    if reference.tzinfo is None:
        raise ValueError("planning datetime reference must include timezone")
    if value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value.astimezone(reference.tzinfo)
_ORGANIZATION_LEGAL_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "科技有限公司",
    "有限公司",
)
_ORGANIZATION_LOCATION_PREFIXES = (
    "北京",
    "上海",
    "深圳",
    "广州",
    "天津",
    "重庆",
    "合肥",
    "西安",
    "成都",
    "武汉",
    "南京",
    "杭州",
    "苏州",
    "无锡",
)
_ORGANIZATION_LEADING_DESCRIPTORS = (
    "商业航天企业",
    "商业航天公司",
    "星地激光通信企业",
    "空间激光通信企业",
    "激光通信企业",
    "火箭新锐公司",
    "航天新锐公司",
)
_ORGANIZATION_TRAILING_DESCRIPTORS = (
    "商业航天企业",
    "商业航天公司",
    "商业航天",
    "星地激光通信企业",
    "空间激光通信企业",
    "激光通信企业",
    "激光通信",
)


def _same_verification_event(
    left: FollowupTarget,
    right: FollowupTarget,
) -> bool:
    left_analysis = left.analysis
    right_analysis = right.analysis
    if left_analysis.category is not right_analysis.category:
        return False
    if left_analysis.event_type is not right_analysis.event_type:
        return False
    if not (
        _organization_aliases(
            left_analysis.organization or left.candidate.title
        )
        & _organization_aliases(
            right_analysis.organization or right.candidate.title
        )
    ):
        return False

    left_date = _verification_event_date(left)
    right_date = _verification_event_date(right)
    if left_date is None or right_date is None:
        return False
    left_rounds = _verification_rounds(left_analysis.financing_round)
    right_rounds = _verification_rounds(right_analysis.financing_round)
    maximum_gap_days = 7
    if (
        left_analysis.category is Category.COMMERCIAL_SPACE_FINANCING
        and left_rounds
        and right_rounds
        and not left_rounds.isdisjoint(right_rounds)
        and _financing_claims_compatible(left_analysis, right_analysis)
    ):
        maximum_gap_days = 30
    if abs((left_date.date() - right_date.date()).days) > maximum_gap_days:
        return False

    if left_rounds and right_rounds:
        return not left_rounds.isdisjoint(right_rounds)
    return _verification_title(left) == _verification_title(right)


def _verification_event_group_key(
    targets: Iterable[FollowupTarget],
) -> str:
    rows = tuple(targets)
    if not rows:
        return ""
    aliases = set().union(
        *(
            _organization_aliases(
                item.analysis.organization or item.candidate.title
            )
            for item in rows
        )
    )
    organization = min(aliases, key=lambda value: (len(value), value), default="")
    categories = sorted(
        {
            item.analysis.category.value
            for item in rows
            if item.analysis.category is not None
        }
    )
    event_types = sorted(
        {
            item.analysis.event_type.value
            for item in rows
            if item.analysis.event_type is not None
        }
    )
    rounds = sorted(
        set().union(
            *(
                _verification_rounds(item.analysis.financing_round)
                for item in rows
            )
        )
    )
    dates = sorted(
        value.date().isoformat()
        for item in rows
        if (value := _verification_event_date(item)) is not None
    )
    event_detail = ",".join(rounds)
    if not event_detail:
        event_detail = f"title:{min(_verification_title(item) for item in rows)}"
    return "|".join(
        (
            organization,
            ",".join(categories),
            ",".join(event_types),
            event_detail,
            dates[0] if dates else "date:unknown",
        )
    )


def _verification_event_date(target: FollowupTarget) -> datetime | None:
    return target.analysis.published_at or target.candidate.source_published_at


def _verification_rounds(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(
        re.sub(r"[\s-]+", "", matched.casefold())
        for matched in _VERIFICATION_ROUND.findall(
            unicodedata.normalize("NFKC", value)
        )
    )


def _organization_aliases(value: str) -> frozenset[str]:
    normalized = re.sub(
        r"[\s·•（）()“”\"'，,：:丨|]+",
        "",
        unicodedata.normalize("NFKC", value).casefold(),
    )
    changed = True
    while changed:
        changed = False
        for descriptor in _ORGANIZATION_LEADING_DESCRIPTORS:
            normalized_descriptor = descriptor.casefold()
            if normalized.startswith(normalized_descriptor):
                normalized = normalized[len(normalized_descriptor) :]
                changed = True
                break
    changed = True
    while changed:
        changed = False
        for descriptor in _ORGANIZATION_TRAILING_DESCRIPTORS:
            normalized_descriptor = descriptor.casefold()
            if normalized.endswith(normalized_descriptor):
                normalized = normalized[: -len(normalized_descriptor)]
                changed = True
                break
    if not normalized:
        return frozenset()
    aliases = {normalized}
    legal_core: str | None = None
    for suffix in _ORGANIZATION_LEGAL_SUFFIXES:
        normalized_suffix = suffix.casefold()
        if normalized.endswith(normalized_suffix):
            legal_core = normalized[: -len(normalized_suffix)]
            aliases.add(legal_core)
            break
    if legal_core:
        for prefix in _ORGANIZATION_LOCATION_PREFIXES:
            normalized_prefix = prefix.casefold()
            if not legal_core.startswith(normalized_prefix):
                continue
            short = legal_core[len(normalized_prefix) :]
            if short:
                aliases.add(short)
                if short.endswith("科技") and len(short) > len("科技") + 1:
                    aliases.add(short[: -len("科技")])
            break
    return frozenset(alias for alias in aliases if alias)


def _financing_claims_compatible(
    left: AnalysisResult,
    right: AnalysisResult,
) -> bool:
    left_amount = _normalize_name(left.amount or "")
    right_amount = _normalize_name(right.amount or "")
    if left_amount and right_amount and left_amount != right_amount:
        return False
    left_investors = {
        _normalize_name(value) for value in left.investors if _normalize_name(value)
    }
    right_investors = {
        _normalize_name(value) for value in right.investors if _normalize_name(value)
    }
    if left_investors and right_investors and left_investors.isdisjoint(right_investors):
        return False
    return True


def _registered_source_domain(
    source_url: str,
    registered_domains: Iterable[str],
) -> str | None:
    hostname = (urlsplit(source_url).hostname or "").lower().rstrip(".")
    return next(
        (
            domain
            for domain in registered_domains
            if hostname == domain or hostname.endswith(f".{domain}")
        ),
        None,
    )


def _verification_title(target: FollowupTarget) -> str:
    return re.sub(
        r"[\s*｜|_\-—:：·•（）()“”\"'，,]+",
        "",
        unicodedata.normalize(
            "NFKC",
            target.analysis.title or target.candidate.title,
        ).casefold(),
    )


def _query_value(value: str) -> str:
    return " ".join(value.replace('"', " ").replace("“", " ").replace("”", " ").split())


def _target_evidence_gaps(
    target: FollowupTarget,
) -> tuple[str, ...]:
    if target.decision.reason == "missing_required_fields:published_at":
        return ("published_at",)
    if (
        target.analysis.category is Category.COMMERCIAL_SPACE_FINANCING
        and target.decision.reason == "financing_missing_required_evidence"
    ):
        return financing_evidence_gaps(target.analysis)
    return ()


def _gap_query_terms(fields: Iterable[str]) -> str:
    terms = {
        "organization": "企业主体 公司全称",
        "published_at": "发布日期 发布时间 公告时间 官方披露",
        "amount": "融资金额 金额未披露 具体金额",
        "financing_round": "融资轮次 Pre-A Pre-A+",
        "financing_subtype": "融资类型 战略投资 增资 并购",
        "investors": "投资方 领投 跟投",
    }
    return " ".join(
        dict.fromkeys(terms[field] for field in fields if field in terms)
    )


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
