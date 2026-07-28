"""Deterministic source grading and evidence-based verification rules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from urllib.parse import urlsplit

from pydantic import Field

from .analyzer import (
    RuleFallbackAnalyzer,
    _event_type,
    _event_type_terms,
    _has_domestic_signal,
    _has_explicit_foreign_signal,
    _normalize,
)
from .fetcher import FetchedPage
from .models import (
    AnalysisResult,
    Category,
    DomainModel,
    Evidence,
    EventType,
    SourceRecord,
    SourceGrade,
    VerificationStatus,
)


class VerificationDecision(DomainModel):
    status: VerificationStatus
    reason: str
    source_grade: SourceGrade
    evidence: list[Evidence] = Field(default_factory=list)
    source_records: list[SourceRecord] = Field(default_factory=list)


class SourceRegistry:
    """Grade hosts from an explicit registry, preferring the narrowest match."""

    def __init__(
        self,
        domains: Mapping[str, SourceGrade | str],
        *,
        financing_company_domains: Mapping[str, str | Iterable[str]] | None = None,
        financing_investor_domains: Mapping[str, str | Iterable[str]] | None = None,
        financing_b_domains: Iterable[str] = (),
    ) -> None:
        self._domains = {
            self._normalize_domain(domain): SourceGrade(grade)
            for domain, grade in domains.items()
        }
        self._financing_companies = {
            self._normalize_domain(domain): self._aliases(company)
            for domain, company in (financing_company_domains or {}).items()
        }
        self._financing_investors = {
            self._normalize_domain(domain): self._aliases(investor)
            for domain, investor in (financing_investor_domains or {}).items()
        }
        if any(not company for company in self._financing_companies.values()):
            raise ValueError("financing company name must not be empty")
        self._financing_b_domains = {
            self._normalize_domain(domain) for domain in financing_b_domains
        }
        official_domains = set(self._financing_companies) | set(
            self._financing_investors
        )
        if (
            official_domains & self._financing_b_domains
            or set(self._financing_companies) & set(self._financing_investors)
        ):
            raise ValueError("financing source registries must be disjoint")

    def grade(self, url: str) -> SourceGrade:
        domain = self.registered_domain(url)
        return self._domains[domain] if domain is not None else SourceGrade.C

    def registered_domain(self, url: str) -> str | None:
        return self._registered_domain(url, self._domains)

    def financing_registered_domain(self, url: str) -> str | None:
        return self._registered_domain(
            url,
            {
                **{domain: SourceGrade.A for domain in self._financing_companies},
                **{domain: SourceGrade.A for domain in self._financing_investors},
                **{domain: SourceGrade.B for domain in self._financing_b_domains},
            },
        )

    def grade_financing(
        self,
        url: str,
        company: str | None,
        investors: Iterable[str] = (),
        evidence: Iterable[Evidence] = (),
    ) -> SourceGrade:
        domain = self.financing_registered_domain(url)
        if domain is None:
            return SourceGrade.C
        if domain in self._financing_b_domains:
            return SourceGrade.B
        if domain in self._financing_investors:
            aliases = self._financing_investors[domain]
            return SourceGrade.A if any(
                self._company_matches(investor, aliases)
                and any(
                    item.field == "investors"
                    and self._normalize_claim(investor)
                    in self._normalize_claim(item.quote)
                    for item in evidence
                )
                for investor in investors
            ) else SourceGrade.C
        registered_company = self._financing_companies[domain]
        return (
            SourceGrade.A
            if self._company_matches(company, registered_company)
            else SourceGrade.C
        )

    def is_known_financing_company(self, company: str | None) -> bool:
        return any(
            self._company_matches(company, registered)
            for registered in self._financing_companies.values()
        )

    @property
    def financing_domains(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self._financing_companies,
                    *self._financing_investors,
                    *self._financing_b_domains,
                }
            )
        )

    @staticmethod
    def _registered_domain(url: str, domains: Mapping[str, object]) -> str | None:
        host = urlsplit(url).hostname
        if not host:
            return None
        host = host.lower().rstrip(".")
        matches = [
            domain
            for domain in domains
            if host == domain or host.endswith("." + domain)
        ]
        return max(matches, key=len, default=None)

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        normalized = domain.strip().lower().rstrip(".")
        if not normalized or "://" in normalized or "/" in normalized:
            raise ValueError(f"invalid registered domain: {domain}")
        return normalized

    @classmethod
    def _company_matches(cls, claim: str | None, registered: Iterable[str]) -> bool:
        if not claim or not claim.strip():
            return False
        normalized_claim = cls._normalize_claim(claim)
        return normalized_claim in {
            cls._normalize_claim(alias) for alias in registered
        }

    @staticmethod
    def _aliases(value: str | Iterable[str]) -> tuple[str, ...]:
        raw = (value,) if isinstance(value, str) else tuple(value)
        aliases = tuple(
            dict.fromkeys(alias.strip() for alias in raw if alias.strip())
        )
        if not aliases:
            raise ValueError("financing registry aliases must not be empty")
        return aliases

    @staticmethod
    def _normalize_claim(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(
            character
            for character in normalized
            if not character.isspace()
            and not unicodedata.category(character).startswith("P")
        )


class RuleVerifier:
    def __init__(self, registry: SourceRegistry) -> None:
        self._registry = registry

    def verify(
        self,
        analysis: AnalysisResult,
        page: FetchedPage,
        corroborating: Iterable[FetchedPage] | None = None,
    ) -> VerificationDecision:
        financing_claim = (
            analysis.category is Category.COMMERCIAL_SPACE_FINANCING
            or analysis.event_type is EventType.FINANCING
        )
        grade = (
            self._registry.grade_financing(
                page.final_url,
                analysis.organization,
                analysis.investors,
                analysis.evidence,
            )
            if financing_claim
            else self._registry.grade(page.final_url)
        )

        if not analysis.in_china or not analysis.in_scope:
            return self._decision(VerificationStatus.REJECTED, "out_of_scope", grade)
        if (
            not analysis.title.strip()
            or not analysis.organization
            or not analysis.organization.strip()
            or analysis.published_at is None
            or analysis.category is None
            or analysis.event_type is None
        ):
            return self._decision(VerificationStatus.PENDING, "missing_required_fields", grade)
        if analysis.source_url != page.final_url:
            return self._decision(VerificationStatus.PENDING, "source_url_mismatch", grade)
        if any(
            evidence.source_url != page.final_url
            or not evidence.quote.strip()
            or (
                evidence.quote not in page.text
                and evidence.quote not in page.title
                and not (
                    evidence.field == "in_china"
                    and evidence.quote == page.final_url
                    and grade is SourceGrade.A
                )
            )
            for evidence in analysis.evidence
        ):
            return self._decision(VerificationStatus.PENDING, "evidence_not_grounded", grade)
        is_financing_category = (
            analysis.category is Category.COMMERCIAL_SPACE_FINANCING
        )
        is_financing_event = analysis.event_type is EventType.FINANCING
        if is_financing_category != is_financing_event:
            return self._decision(
                VerificationStatus.PENDING,
                "category_event_type_mismatch",
                grade,
            )
        if not 200 <= page.status_code < 300:
            return self._decision(VerificationStatus.PENDING, "source_unavailable", grade)

        if analysis.category is not Category.COMMERCIAL_SPACE_FINANCING:
            if grade is not SourceGrade.A:
                return self._decision(
                    VerificationStatus.PENDING,
                    "tender_requires_grade_a",
                    grade,
                    analysis.evidence,
                )
            classification_failure = self._classification_failure(
                analysis, page, grade
            )
            if classification_failure is not None:
                return self._decision(
                    VerificationStatus.PENDING,
                    classification_failure,
                    grade,
                    analysis.evidence,
                )
            deadline_failure = self._deadline_failure(analysis)
            if deadline_failure is not None:
                return self._decision(
                    VerificationStatus.PENDING,
                    deadline_failure,
                    grade,
                    analysis.evidence,
                )
            required = {"title", "organization", "published_at"}
            if not required.issubset({item.field for item in analysis.evidence}):
                return self._decision(
                    VerificationStatus.PENDING,
                    "tender_missing_required_evidence",
                    grade,
                    analysis.evidence,
                )
            if not self._evidence_supports_tender_claims(analysis):
                return self._decision(
                    VerificationStatus.PENDING,
                    "evidence_not_grounded",
                    grade,
                    analysis.evidence,
                )
            return self._decision(
                VerificationStatus.VERIFIED,
                "verified_tender",
                grade,
                analysis.evidence,
            )

        if grade in {SourceGrade.A, SourceGrade.B}:
            classification_failure = self._classification_failure(
                analysis, page, grade
            )
            if classification_failure is not None:
                return self._decision(
                    VerificationStatus.PENDING,
                    classification_failure,
                    grade,
                    analysis.evidence,
                )

        if grade in {SourceGrade.A, SourceGrade.B}:
            financing_failure = self._financing_claim_failure(analysis)
            if financing_failure is not None:
                return self._decision(
                    VerificationStatus.PENDING,
                    financing_failure,
                    grade,
                    analysis.evidence,
                )

        if grade is SourceGrade.A:
            record = self._source_record(analysis, page, grade)
            return self._decision(
                VerificationStatus.VERIFIED,
                "verified_financing_official_source",
                grade,
                analysis.evidence,
                [record],
            )
        if grade is SourceGrade.B:
            reason, supporting = self._independent_corroboration(
                analysis, page, corroborating or ()
            )
            if supporting is not None:
                records = [self._source_record(analysis, page, grade), supporting]
                combined_evidence = sorted(
                    {
                        (item.field, item.quote, item.source_url): item
                        for record in records
                        for item in record.evidence
                    }.values(),
                    key=lambda item: (item.source_url, item.field, item.quote),
                )
                return self._decision(
                    VerificationStatus.VERIFIED,
                    "verified_financing_two_independent_sources",
                    grade,
                    combined_evidence,
                    records,
                )
            return self._decision(
                VerificationStatus.PENDING,
                reason,
                grade,
                analysis.evidence,
            )
        return self._decision(
            VerificationStatus.PENDING,
            "financing_requires_official_or_two_independent_b_sources",
            grade,
            analysis.evidence,
        )

    def _classification_failure(
        self,
        analysis: AnalysisResult,
        page: FetchedPage,
        grade: SourceGrade,
    ) -> str | None:
        context: list[str] = []
        known_financing_company = self._registry.is_known_financing_company(
            analysis.organization
        )
        if grade is SourceGrade.A or known_financing_company:
            context.append("中国境内项目")
        if (
            analysis.category is Category.COMMERCIAL_SPACE_FINANCING
            and known_financing_company
        ):
            context.append("中国商业航天企业")
        deterministic_page = page.model_copy(
            update={"text": "\n".join([*context, page.text])}
        )
        deterministic = RuleFallbackAnalyzer().analyze(deterministic_page)
        if (
            deterministic.in_china is not analysis.in_china
            or deterministic.in_scope is not analysis.in_scope
            or deterministic.category is not analysis.category
            or deterministic.event_type is not analysis.event_type
        ):
            return "classification_rule_disagreement"

        by_field: dict[str, list[Evidence]] = {}
        for item in analysis.evidence:
            by_field.setdefault(item.field, []).append(item)
        required = {"in_china", "in_scope", "category", "event_type"}
        if not required.issubset(by_field):
            return "classification_evidence_missing"

        country_supported = any(
            (
                item.quote == page.final_url and grade is SourceGrade.A
            )
            or (
                _has_domestic_signal(item.quote)
                and not _has_explicit_foreign_signal(item.quote)
            )
            for item in by_field["in_china"]
        )
        category_supported = any(
            RuleFallbackAnalyzer._category(_normalize(item.quote))
            is analysis.category
            for item in by_field["category"]
        )
        event_supported = any(
            self._evidence_event_type(item.quote, analysis.category)
            is analysis.event_type
            for item in by_field["event_type"]
        )
        scope_supported = any(
            RuleFallbackAnalyzer._category(_normalize(item.quote))
            is analysis.category
            and self._evidence_event_type(item.quote, analysis.category)
            is analysis.event_type
            for item in by_field["in_scope"]
        )
        if not all(
            (country_supported, category_supported, event_supported, scope_supported)
        ):
            return "classification_evidence_invalid"
        return None

    @classmethod
    def _deadline_failure(cls, analysis: AnalysisResult) -> str | None:
        fields = {
            "registration": analysis.registration_deadline,
            "bid_submission": analysis.bid_submission_deadline,
            "opening": analysis.opening_deadline,
        }
        if any(name not in fields or fields[name] is None for name in analysis.deadline_precision):
            return "deadline_evidence_invalid"
        evidence_by_field: dict[str, list[Evidence]] = {}
        for item in analysis.evidence:
            evidence_by_field.setdefault(item.field, []).append(item)
        for name, value in fields.items():
            if value is None:
                continue
            precision = analysis.deadline_precision.get(name)
            deadline_evidence = evidence_by_field.get(f"{name}_deadline", [])
            if precision is None or not deadline_evidence:
                return "deadline_evidence_missing"
            if not any(
                cls._deadline_quote_supports(item.quote, value, precision)
                for item in deadline_evidence
            ):
                return "deadline_evidence_invalid"
        return None

    @classmethod
    def _deadline_quote_supports(
        cls, quote: str, value: datetime, precision: str
    ) -> bool:
        normalized = cls._normalize(quote)
        if not any(needle in normalized for needle in cls._date_needles(value)):
            return False
        if precision == "date":
            return True
        minute_forms = {
            f"{value.hour:02d}:{value.minute:02d}",
            f"{value.hour}:{value.minute:02d}",
            f"{value.hour}时{value.minute:02d}分",
        }
        if not any(cls._normalize(form) in normalized for form in minute_forms):
            return False
        if precision == "minute":
            return True
        second_forms = {
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}",
            f"{value.hour}:{value.minute:02d}:{value.second:02d}",
            f"{value.hour}时{value.minute:02d}分{value.second:02d}秒",
        }
        return any(cls._normalize(form) in normalized for form in second_forms)

    @staticmethod
    def _evidence_event_type(
        quote: str, category: Category | None
    ) -> EventType | None:
        if category is Category.COMMERCIAL_SPACE_FINANCING and not any(
            _normalize(term) in _normalize(quote)
            for term in _event_type_terms(EventType.FINANCING)
        ):
            return None
        return _event_type(_normalize(quote), category)

    @classmethod
    def _evidence_supports_tender_claims(cls, analysis: AnalysisResult) -> bool:
        return (
            cls._field_has_quote_containing(
                analysis.evidence, "title", analysis.title
            )
            and cls._field_has_quote_containing(
                analysis.evidence, "organization", analysis.organization or ""
            )
            and cls._field_has_date(analysis.evidence, analysis.published_at)
        )

    @classmethod
    def _financing_claim_failure(cls, analysis: AnalysisResult) -> str | None:
        evidence_fields = {item.field for item in analysis.evidence}
        subtype = analysis.financing_subtype or (
            "round_equity" if analysis.financing_round else None
        )
        required = {"organization", "published_at", "amount"}
        if subtype == "round_equity":
            required.add("financing_round")
        else:
            required.add("financing_subtype")
        if analysis.investors:
            required.add("investors")
        if subtype is None or not required.issubset(evidence_fields):
            return "financing_missing_required_evidence"
        if not cls._field_has_quote_containing(
            analysis.evidence, "organization", analysis.organization or ""
        ):
            return "evidence_not_grounded"
        if analysis.published_at is None or not cls._field_has_date(
            analysis.evidence, analysis.published_at
        ):
            return "evidence_not_grounded"
        if subtype == "round_equity":
            if not analysis.financing_round or not any(
                item.field == "financing_round"
                and cls._contains_round(item.quote, analysis.financing_round)
                for item in analysis.evidence
            ):
                return "evidence_not_grounded"
        elif not any(
            item.field == "financing_subtype"
            and cls._financing_subtype_supported(item.quote, subtype)
            for item in analysis.evidence
        ):
            return "evidence_not_grounded"
        if analysis.amount:
            if analysis.amount_disclosed is False or not any(
                item.field == "amount"
                and cls._amount_key(item.quote) == cls._amount_key(analysis.amount)
                for item in analysis.evidence
            ):
                return "evidence_not_grounded"
        elif analysis.amount_disclosed is not False or not any(
            item.field == "amount" and cls._is_undisclosed(item.quote)
            for item in analysis.evidence
        ):
            return "financing_missing_required_evidence"
        for investor in analysis.investors:
            if not cls._field_has_quote_containing(
                analysis.evidence, "investors", investor
            ):
                return "evidence_not_grounded"
        if analysis.business_area and not cls._field_has_quote_containing(
            analysis.evidence, "business_area", analysis.business_area
        ):
            return "evidence_not_grounded"
        if not any(
            item.field == "event_type"
            and cls._contains_any(item.quote, _event_type_terms(EventType.FINANCING))
            for item in analysis.evidence
        ):
            return "evidence_not_grounded"
        return None

    @classmethod
    def _financing_subtype_supported(cls, quote: str, subtype: str) -> bool:
        terms = {
            "strategic": ("战略融资", "战略投资"),
            "capital_increase": ("产业基金增资", "基金增资"),
            "merger_acquisition": ("并购融资", "并购投资"),
        }.get(subtype, ())
        return cls._contains_any(quote, terms)

    @classmethod
    def _field_has_quote_containing(
        cls, evidence: Iterable[Evidence], field: str, claim: str
    ) -> bool:
        normalized_claim = cls._normalize_claim(claim)
        return bool(normalized_claim) and any(
            item.field == field
            and normalized_claim in cls._normalize_claim(item.quote)
            for item in evidence
        )

    @classmethod
    def _field_has_date(
        cls, evidence: Iterable[Evidence], published_at: datetime
    ) -> bool:
        date_needles = cls._date_needles(published_at)
        return any(
            item.field == "published_at"
            and any(needle in cls._normalize(item.quote) for needle in date_needles)
            for item in evidence
        )

    def _independent_corroboration(
        self,
        analysis: AnalysisResult,
        page: FetchedPage,
        corroborating: Iterable[object],
    ) -> tuple[str, SourceRecord | None]:
        primary_domain = self._registry.financing_registered_domain(page.final_url)
        saw_duplicate = False
        saw_conflict = False
        saw_analyzed_source = False

        for candidate in corroborating:
            if (
                not isinstance(candidate, tuple)
                or len(candidate) != 2
                or not isinstance(candidate[0], AnalysisResult)
                or not isinstance(candidate[1], FetchedPage)
            ):
                continue
            other_analysis, other = candidate
            other_domain = self._registry.financing_registered_domain(other.final_url)
            other_grade = self._registry.grade_financing(
                other.final_url, other_analysis.organization
            )
            if (
                other.status_code < 200
                or other.status_code >= 300
                or other_grade is not SourceGrade.B
                or other_domain is None
            ):
                continue
            if (
                not self._independent_domains(primary_domain, other_domain)
                or other.content_hash == page.content_hash
            ):
                saw_duplicate = True
                continue
            if self._normalize_claim(analysis.organization or "") != self._normalize_claim(
                other_analysis.organization or ""
            ):
                continue
            saw_analyzed_source = True
            if self._analysis_page_failure(other_analysis, other, other_grade):
                continue
            if not self._critical_financing_fields_agree(analysis, other_analysis):
                saw_conflict = True
                continue
            return (
                "verified_financing_two_independent_sources",
                self._source_record(other_analysis, other, other_grade),
            )
        if saw_conflict:
            return "financing_corroboration_conflict", None
        if saw_duplicate:
            return "financing_requires_independent_sources", None
        if saw_analyzed_source:
            return "financing_corroboration_insufficient", None
        return "financing_requires_official_or_two_independent_b_sources", None

    def _analysis_page_failure(
        self,
        analysis: AnalysisResult,
        page: FetchedPage,
        grade: SourceGrade,
    ) -> str | None:
        if (
            not analysis.in_china
            or not analysis.in_scope
            or analysis.category is not Category.COMMERCIAL_SPACE_FINANCING
            or analysis.event_type is not EventType.FINANCING
            or analysis.published_at is None
            or not analysis.organization
            or analysis.source_url != page.final_url
        ):
            return "missing_or_out_of_scope"
        if any(
            item.source_url != page.final_url
            or not item.quote.strip()
            or (
                item.quote not in page.text
                and item.quote not in page.title
                and not (
                    item.field == "in_china"
                    and item.quote == page.final_url
                    and grade is SourceGrade.A
                )
            )
            for item in analysis.evidence
        ):
            return "evidence_not_grounded"
        return self._classification_failure(
            analysis, page, grade
        ) or self._financing_claim_failure(
            analysis
        )

    @classmethod
    def _critical_financing_fields_agree(
        cls, primary: AnalysisResult, other: AnalysisResult
    ) -> bool:
        return (
            cls._normalize_claim(primary.organization or "")
            == cls._normalize_claim(other.organization or "")
            and primary.published_at is not None
            and other.published_at is not None
            and primary.published_at.date() == other.published_at.date()
            and cls._normalize_round(primary.financing_round)
            == cls._normalize_round(other.financing_round)
            and (
                primary.financing_subtype
                or ("round_equity" if primary.financing_round else None)
            )
            == (
                other.financing_subtype
                or ("round_equity" if other.financing_round else None)
            )
            and cls._analysis_amount_key(primary) == cls._analysis_amount_key(other)
            and {
                cls._normalize_claim(investor) for investor in primary.investors
            }
            == {cls._normalize_claim(investor) for investor in other.investors}
        )

    @staticmethod
    def _source_record(
        analysis: AnalysisResult, page: FetchedPage, grade: SourceGrade
    ) -> SourceRecord:
        return SourceRecord(
            source_url=page.final_url,
            source_grade=grade,
            published_at=analysis.published_at,
            content_hash=page.content_hash,
            evidence=sorted(
                analysis.evidence,
                key=lambda item: (item.field, item.quote, item.source_url),
            ),
        )

    @classmethod
    def _analysis_amount_key(cls, analysis: AnalysisResult) -> tuple[bool, str | None]:
        disclosed = bool(analysis.amount) or analysis.amount_disclosed is True
        return disclosed, cls._amount_key(analysis.amount) if analysis.amount else None

    @classmethod
    def _amount_key(cls, value: str | None) -> str | None:
        if not value:
            return None
        normalized = unicodedata.normalize("NFKC", value).replace(",", "")
        match = re.search(r"(\d+(?:\.\d+)?)", normalized)
        if not match:
            return cls._normalize_claim(normalized)
        try:
            amount = Decimal(match.group(1))
        except InvalidOperation:
            return cls._normalize_claim(normalized)
        if "亿" in normalized:
            amount *= Decimal("100000000")
        elif "万" in normalized:
            amount *= Decimal("10000")
        return format(amount.normalize(), "f")

    @classmethod
    def _normalize_round(cls, value: str | None) -> str:
        normalized = cls._normalize_claim(value or "")
        for suffix in ("融资", "轮"):
            normalized = normalized.removesuffix(suffix)
        return normalized

    @classmethod
    def _is_undisclosed(cls, value: str) -> bool:
        normalized = cls._normalize_claim(value)
        return any(marker in normalized for marker in ("未披露", "未公布", "未透露"))

    @classmethod
    def _contains_any(cls, value: str, terms: Iterable[str]) -> bool:
        normalized = cls._normalize(value)
        return any(cls._normalize(term) in normalized for term in terms)

    @staticmethod
    def _independent_domains(primary: str | None, other: str) -> bool:
        if primary is None:
            return False
        return not (
            primary == other
            or primary.endswith("." + other)
            or other.endswith("." + primary)
        )

    @classmethod
    def _contains_round(cls, text: str, round_name: str | None) -> bool:
        if not round_name:
            return True
        normalized_round = cls._normalize(round_name)
        if len(normalized_round) == 1 and normalized_round.isascii() and normalized_round.isalnum():
            normalized_text = unicodedata.normalize("NFKC", text).casefold()
            return re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_round)}(?![a-z0-9])",
                normalized_text,
            ) is not None
        return normalized_round in cls._normalize(text)

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()

    @staticmethod
    def _normalize_claim(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(
            character
            for character in normalized
            if not character.isspace()
            and not unicodedata.category(character).startswith("P")
        )

    @classmethod
    def _date_needles(cls, value: datetime) -> set[str]:
        year, month, day = value.year, value.month, value.day
        forms = {
            f"{year:04d}-{month:02d}-{day:02d}",
            f"{year:04d}/{month:02d}/{day:02d}",
            f"{year:04d}.{month:02d}.{day:02d}",
            f"{year}年{month}月{day}日",
            f"{year}年{month:02d}月{day:02d}日",
        }
        return {cls._normalize(item) for item in forms}

    @staticmethod
    def _decision(
        status: VerificationStatus,
        reason: str,
        grade: SourceGrade,
        evidence: Iterable[Evidence] = (),
        source_records: Iterable[SourceRecord] = (),
    ) -> VerificationDecision:
        return VerificationDecision(
            status=status,
            reason=reason,
            source_grade=grade,
            evidence=list(evidence),
            source_records=list(source_records),
        )
