"""Deterministic source grading and evidence-based verification rules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
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
    _FINANCING_CORROBORATION_WINDOW = timedelta(days=45)

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
        missing_fields = [
            name
            for name, missing in (
                ("title", not analysis.title.strip()),
                (
                    "organization",
                    not analysis.organization or not analysis.organization.strip(),
                ),
                ("published_at", analysis.published_at is None),
                ("category", analysis.category is None),
                ("event_type", analysis.event_type is None),
            )
            if missing
        ]
        if missing_fields:
            return self._decision(
                VerificationStatus.PENDING,
                f"missing_required_fields:{','.join(missing_fields)}",
                grade,
            )
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
            financing_failure = (
                self._financing_claim_failure(analysis)
                if grade is SourceGrade.A
                else self._financing_source_event_failure(analysis)
            )
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
        if not country_supported:
            return "classification_country_evidence_invalid"
        scope_evidence = [
            *by_field["in_scope"],
            *by_field["category"],
            *by_field["event_type"],
        ]
        scope_text = "\n".join(item.quote for item in scope_evidence)
        page_category_supported = (
            RuleFallbackAnalyzer._category(_normalize(scope_text))
            is analysis.category
        )
        page_event_supported = (
            self._evidence_event_type(scope_text, analysis.category)
            is analysis.event_type
        )
        if not page_category_supported:
            return "classification_category_evidence_invalid"
        if not page_event_supported:
            return "classification_event_evidence_invalid"
        in_scope_text = "\n".join(
            item.quote for item in by_field["in_scope"]
        )
        scope_supported = (
            analysis.category is not None
            and self._contains_any(
                in_scope_text,
                RuleFallbackAnalyzer._CATEGORY_TERMS[analysis.category],
            )
            or self._evidence_event_type(in_scope_text, analysis.category)
            is analysis.event_type
        )
        if not scope_supported:
            return "classification_scope_evidence_invalid"
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
    def _financing_evidence_gaps(
        cls,
        analysis: AnalysisResult,
    ) -> tuple[str, ...]:
        gaps: list[str] = []
        if not analysis.organization or not cls._field_has_quote_containing(
            analysis.evidence,
            "organization",
            analysis.organization or "",
        ):
            gaps.append("organization")
        if analysis.published_at is None or not cls._field_has_date(
            analysis.evidence,
            analysis.published_at,
        ):
            gaps.append("published_at")

        if analysis.amount:
            if analysis.amount_disclosed is False or not any(
                item.field == "amount"
                and cls._amount_key(item.quote)
                == cls._amount_key(analysis.amount)
                for item in analysis.evidence
            ):
                gaps.append("amount")
        elif analysis.amount_disclosed is not False or not any(
            item.field == "amount" and cls._is_undisclosed(item.quote)
            for item in analysis.evidence
        ):
            gaps.append("amount")

        subtype = analysis.financing_subtype or (
            "round_equity" if analysis.financing_round else None
        )
        if subtype == "round_equity":
            if not analysis.financing_round or not any(
                item.field == "financing_round"
                and cls._contains_round(item.quote, analysis.financing_round)
                for item in analysis.evidence
            ):
                gaps.append("financing_round")
        elif subtype is None or not any(
            item.field == "financing_subtype"
            and cls._financing_subtype_supported(item.quote, subtype)
            for item in analysis.evidence
        ):
            gaps.append("financing_subtype")

        if analysis.investors and any(
            not cls._field_has_quote_containing(
                analysis.evidence,
                "investors",
                investor,
            )
            for investor in analysis.investors
        ):
            gaps.append("investors")
        return tuple(gaps)

    @classmethod
    def _financing_source_event_failure(
        cls,
        analysis: AnalysisResult,
    ) -> str | None:
        """Validate one B source without treating omitted attributes as facts."""

        evidence_fields = {item.field for item in analysis.evidence}
        subtype = analysis.financing_subtype or (
            "round_equity" if analysis.financing_round else None
        )
        required = {"organization", "published_at"}
        if subtype == "round_equity":
            required.add("financing_round")
        else:
            required.add("financing_subtype")
        if subtype is None:
            return "financing_source_event_evidence_missing"
        missing_required = required - evidence_fields
        missing_reason = {
            "organization": "financing_source_organization_evidence_invalid",
            "published_at": (
                "financing_source_publication_date_evidence_invalid"
            ),
            "financing_round": "financing_source_round_evidence_invalid",
            "financing_subtype": "financing_source_subtype_evidence_invalid",
        }
        for field in (
            "organization",
            "published_at",
            "financing_round",
            "financing_subtype",
        ):
            if field in missing_required:
                return missing_reason[field]
        if missing_required:
            return "financing_source_event_evidence_missing"
        if not cls._field_has_quote_containing(
            analysis.evidence, "organization", analysis.organization or ""
        ):
            return "financing_source_organization_evidence_invalid"
        if analysis.published_at is None or not cls._field_has_date(
            analysis.evidence, analysis.published_at
        ):
            return "financing_source_publication_date_evidence_invalid"
        if subtype == "round_equity":
            if not analysis.financing_round or not any(
                item.field == "financing_round"
                and cls._contains_round(item.quote, analysis.financing_round)
                for item in analysis.evidence
            ):
                return "financing_source_round_evidence_invalid"
        elif not any(
            item.field == "financing_subtype"
            and cls._financing_subtype_supported(item.quote, subtype)
            for item in analysis.evidence
        ):
            return "financing_source_subtype_evidence_invalid"
        if analysis.amount:
            if analysis.amount_disclosed is False or not any(
                item.field == "amount"
                and cls._amount_key(item.quote) == cls._amount_key(analysis.amount)
                for item in analysis.evidence
            ):
                return "financing_source_amount_evidence_invalid"
        elif analysis.amount_disclosed is False and not any(
            item.field == "amount" and cls._is_undisclosed(item.quote)
            for item in analysis.evidence
        ):
            return "financing_source_amount_evidence_invalid"
        for investor in analysis.investors:
            if not cls._field_has_quote_containing(
                analysis.evidence, "investors", investor
            ):
                return "financing_source_investor_evidence_invalid"
        if analysis.business_area and not cls._field_has_quote_containing(
            analysis.evidence, "business_area", analysis.business_area
        ):
            return "financing_source_business_area_evidence_invalid"
        if not any(
            item.field == "event_type"
            and cls._contains_any(item.quote, _event_type_terms(EventType.FINANCING))
            for item in analysis.evidence
        ):
            return "financing_source_action_evidence_invalid"
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
        conflict_reason: str | None = None
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
            if not self._organizations_match(
                analysis.organization, other_analysis.organization
            ):
                continue
            saw_analyzed_source = True
            if self._analysis_page_failure(other_analysis, other, other_grade):
                continue
            if conflict := self._financing_corroboration_conflict(
                analysis, other_analysis
            ):
                conflict_reason = conflict_reason or conflict
                continue
            return (
                "verified_financing_two_independent_sources",
                self._source_record(other_analysis, other, other_grade),
            )
        if conflict_reason is not None:
            return conflict_reason, None
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
        ) or self._financing_source_event_failure(analysis)

    @classmethod
    def _financing_corroboration_conflict(
        cls, primary: AnalysisResult, other: AnalysisResult
    ) -> str | None:
        if primary.published_at is None or other.published_at is None:
            return "financing_source_event_evidence_missing"
        if (
            abs(primary.published_at.date() - other.published_at.date())
            > cls._FINANCING_CORROBORATION_WINDOW
        ):
            return "financing_corroboration_date_outside_window"
        primary_subtype = primary.financing_subtype or (
            "round_equity" if primary.financing_round else None
        )
        other_subtype = other.financing_subtype or (
            "round_equity" if other.financing_round else None
        )
        if primary_subtype != other_subtype:
            return "financing_corroboration_attribute_conflict"
        if not cls._rounds_compatible(
            primary.financing_round, other.financing_round
        ):
            return "financing_corroboration_round_conflict"
        if (
            primary.amount
            and other.amount
            and cls._amount_key(primary.amount) != cls._amount_key(other.amount)
        ):
            return "financing_corroboration_amount_conflict"
        return None

    @classmethod
    def _organizations_match(
        cls, primary: str | None, other: str | None
    ) -> bool:
        return bool(primary and other) and cls._organization_key(
            primary
        ) == cls._organization_key(other)

    @classmethod
    def _organization_key(cls, value: str) -> str:
        normalized = cls._normalize_claim(value)
        for suffix in (
            "股份有限公司",
            "有限责任公司",
            "科技有限公司",
            "有限公司",
            "公司",
        ):
            normalized = normalized.removesuffix(suffix.casefold())
        for prefix in (
            "北京",
            "上海",
            "深圳",
            "广州",
            "杭州",
            "南京",
            "武汉",
            "西安",
            "成都",
            "重庆",
            "天津",
            "苏州",
            "无锡",
        ):
            if normalized.startswith(prefix.casefold()) and len(normalized) > len(prefix) + 2:
                normalized = normalized[len(prefix) :]
                break
        return normalized

    @classmethod
    def _rounds_compatible(cls, primary: str | None, other: str | None) -> bool:
        primary_rounds = cls._round_tokens(primary)
        other_rounds = cls._round_tokens(other)
        if not primary_rounds or not other_rounds:
            return cls._normalize_round(primary) == cls._normalize_round(other)
        return primary_rounds.issubset(other_rounds) or other_rounds.issubset(
            primary_rounds
        )

    @classmethod
    def _round_tokens(cls, value: str | None) -> frozenset[str]:
        if not value:
            return frozenset()
        normalized = unicodedata.normalize("NFKC", value).casefold()
        normalized = re.sub(r"\s+", "", normalized)
        matches = re.findall(
            r"pre-?[a-d]\+{0,2}|series-?[a-d]\+{0,2}|天使\+{0,2}|种子|"
            r"(?<![a-z])[a-d]\+{0,2}(?![a-z])",
            normalized,
        )
        return frozenset(
            item.replace("-", "").removeprefix("series") for item in matches
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


def financing_evidence_gaps(
    analysis: AnalysisResult,
) -> tuple[str, ...]:
    """Return deterministic missing/ungrounded financing evidence fields."""

    return RuleVerifier._financing_evidence_gaps(analysis)
