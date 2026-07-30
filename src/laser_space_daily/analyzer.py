"""Grounded model analysis with deterministic, conservative fallbacks."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
import json
import re
import unicodedata
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from openai import APIError
from pydantic import Field, ValidationError, model_validator

from .fetcher import FetchedPage
from .models import (
    AnalysisResult,
    Category,
    DomainModel,
    Event,
    EventType,
    Evidence,
    Project,
    StateBundle,
    TrendSummary,
    VerificationStatus,
)


BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_FLASH_MODEL = "deepseek-v4-flash"
DEFAULT_PRO_MODEL = "deepseek-v4-pro"
MAX_BODY_CHARS = 50_000


class AnalyzerError(RuntimeError):
    """Base class for controlled analyzer failures."""


class UngroundedOutput(AnalyzerError):
    """Raised when model output contains a claim absent from the fetched page."""


class AnalysisExhausted(AnalyzerError):
    """Raised after all bounded model attempts fail."""


class ModelResponseError(AnalyzerError):
    """Raised when an OpenAI-compatible response does not contain text JSON."""


class MatchSuggestion(DomainModel):
    relation: Literal["same_project", "suspected", "independent"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    requires_human_review: bool = True

    @model_validator(mode="after")
    def force_review(self) -> "MatchSuggestion":
        if not self.requires_human_review:
            raise ValueError("model suggestions always require human review")
        return self


_CONTROLLED_MODEL_ERRORS = (
    APIError,
    AnalyzerError,
    ConnectionError,
    httpx.TransportError,
    OSError,
    TimeoutError,
    ValidationError,
)


ANALYSIS_SYSTEM_PROMPT = """你是中国激光与商业航天情报分析器。必须只输出符合给定 JSON 结构的对象。
事实边界：只有用户提供的 final URL、页面标题和正文是事实。不得推断或补全 URL、日期、金额、公司、采购人、投资方、轮次或项目编号；未知值必须为 null 或空数组。每条 evidence.quote 必须逐字摘自正文，source_url 必须等于给定 final URL。
范围边界：仅包含中国境内的四类事件：(1) 激光通信系统；(2) 激光武器/定向能系统；(3) 光电吊舱/光电转塔；(4) 商业航天股权融资。前三类须处于采购意向、招标、询价、比选、变更、延期、终止、候选、中标、废标、重招等采购生命周期。通用激光器、探测器、转台等上游零部件只有在正文明确说明用于上述母系统时才纳入。商业航天融资排除普通银行贷款/授信和上市公司常规再融资。
不要使用外部知识。标题和所有结构化事实均须可在页面标题或正文中核验。"""


MATCH_SYSTEM_PROMPT = """你是项目匹配助手。只依据给定的新事件字段和候选项目摘要输出 JSON。
不得修改或合并项目。输出 relation、confidence、reason；所有建议必须由人工复核。信息不足时选择 suspected 并降低置信度。"""


class DeepSeekAnalyzer:
    """OpenAI-compatible DeepSeek adapter with strict validation and grounding."""

    def __init__(
        self,
        client: Any,
        *,
        flash_model: str = DEFAULT_FLASH_MODEL,
        pro_model: str = DEFAULT_PRO_MODEL,
        max_attempts: int = 2,
        max_body_chars: int = MAX_BODY_CHARS,
    ) -> None:
        if max_attempts <= 0 or max_attempts > 2:
            raise ValueError("max_attempts must be positive and at most two")
        if max_body_chars <= 0:
            raise ValueError("max_body_chars must be positive")
        self._client = client
        self._flash_model = flash_model
        self._pro_model = pro_model
        self._max_attempts = max_attempts
        self._max_body_chars = max_body_chars
        self._deepseek_tokens = 0

    @property
    def deepseek_tokens(self) -> int:
        """Return total model tokens reported by responses handled so far."""
        return self._deepseek_tokens

    def analyze(self, page: FetchedPage) -> AnalysisResult:
        payload = {
            "source_url": page.final_url,
            "page_title": page.title,
            "body_text": page.text[: self._max_body_chars],
        }
        last_error: BaseException | None = None
        for _attempt in range(self._max_attempts):
            try:
                content = self._complete(
                    model=self._flash_model,
                    system_prompt=(
                        ANALYSIS_SYSTEM_PROMPT
                        + "\nJSON Schema: "
                        + json.dumps(
                            AnalysisResult.model_json_schema(),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    ),
                    payload=payload,
                )
                result = AnalysisResult.model_validate_json(content, strict=True)
                result = guard_grounded_output(result, page)
                return _apply_financing_exclusions(result, page)
            except _CONTROLLED_MODEL_ERRORS as exc:
                last_error = exc
        raise AnalysisExhausted("analysis failed after bounded model attempts") from last_error

    def suggest_match(
        self, event: Event, projects: Sequence[Project]
    ) -> MatchSuggestion:
        payload = {
            "event": {
                "event_id": event.event_id,
                "category": event.category.value,
                "title": event.title,
                "organization": event.organization,
                "published_at": event.published_at.isoformat(),
                "event_type": event.event_type.value,
                "source_url": event.source_url,
            },
            "candidate_projects": [
                {
                    "project_id": project.project_id,
                    "name": project.name,
                    "organization": project.organization,
                    "category": project.category.value,
                }
                for project in projects
            ],
        }
        try:
            content = self._complete(
                model=self._pro_model,
                system_prompt=(
                    MATCH_SYSTEM_PROMPT
                    + "\nJSON Schema: "
                    + json.dumps(
                        MatchSuggestion.model_json_schema(),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ),
                payload=payload,
            )
            return MatchSuggestion.model_validate_json(content, strict=True)
        except _CONTROLLED_MODEL_ERRORS:
            return _conservative_match()

    def summarize_trends(
        self,
        state: StateBundle,
        window: tuple[datetime, datetime],
    ) -> TrendSummary:
        window_start, window_end = window
        if window_end < window_start:
            raise ValueError("window end precedes window start")

        verified_events = [
            event
            for event in state.events
            if event.verification_status is VerificationStatus.VERIFIED
            and window_start <= event.published_at <= window_end
        ]
        verified_financings = [
            financing
            for financing in state.financings
            if financing.verification_status is VerificationStatus.VERIFIED
            and window_start <= financing.announced_at <= window_end
        ]
        counts = Counter(event.category for event in verified_events)
        if verified_financings:
            counts[Category.COMMERCIAL_SPACE_FINANCING] += len(verified_financings)
        event_count = len(verified_events) + len(verified_financings)
        category_counts = dict(counts)
        return TrendSummary(
            window_start=window_start,
            window_end=window_end,
            summary=_rule_trend_summary(event_count, category_counts),
            event_count=event_count,
            category_counts=category_counts,
        )

    def _complete(self, *, model: str, system_prompt: str, payload: dict) -> str:
        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        usage = getattr(response, "usage", None)
        total_tokens = (
            usage.get("total_tokens")
            if isinstance(usage, dict)
            else getattr(usage, "total_tokens", None)
        )
        if isinstance(total_tokens, (int, float)) and not isinstance(
            total_tokens, bool
        ):
            self._deepseek_tokens += max(0, int(total_tokens))
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ModelResponseError("model response has no message content") from exc
        if not isinstance(content, str):
            raise ModelResponseError("model response content is not text")
        return content


class RuleFallbackAnalyzer:
    """Deterministic high-precision keyword fallback."""

    _CATEGORY_TERMS = {
        Category.LASER_COMMUNICATION: (
            "激光通信",
            "空间激光通信",
            "激光通信终端",
            "光通信终端",
            "laser communication",
            "laser terminal",
        ),
        Category.LASER_WEAPON: (
            "激光武器",
            "激光反无人机",
            "激光反制",
            "高能激光武器",
            "定向能激光",
            "laser weapon",
        ),
        Category.EO_TURRET: (
            "光电吊舱",
            "光电转塔",
            "eo turret",
            "electro-optical turret",
        ),
        Category.COMMERCIAL_SPACE_FINANCING: (
            "商业航天",
            "运载火箭公司",
            "运载火箭",
            "液体火箭",
            "卫星公司",
            "卫星制造",
            "卫星运营",
            "低轨卫星",
            "星载",
            "卫星激光通信",
            "星地激光通信",
            "空间激光通信",
            "航天企业",
            "commercial space",
        ),
    }
    _PROCUREMENT_TERMS = (
        "采购意向",
        "采购",
        "招标",
        "询价",
        "比选",
        "变更",
        "延期",
        "终止",
        "候选",
        "中标",
        "废标",
        "重新招标",
        "tender",
        "procurement",
        "award",
    )
    _FINANCING_TERMS = (
        "融资",
        "股权投资",
        "战略投资",
        "天使轮",
        "种子轮",
        "pre-a轮",
        "a轮",
        "b轮",
        "c轮",
        "financing",
        "funding round",
    )
    _FINANCING_EXCLUSIONS = (
        "银行授信",
        "银行贷款",
        "信贷",
        "借款",
        "常规再融资",
        "上市公司再融资",
        "定向增发",
        "配股",
        "可转债",
    )
    _KEY_SUBSYSTEM_TERMS = (
        "跟瞄",
        "捕获跟踪",
        "转台",
        "指向机构",
        "激光器",
        "探测器",
        "光机组件",
    )

    def analyze(self, page: FetchedPage) -> AnalysisResult:
        text = f"{page.title}\n{page.text}"
        normalized = _normalize(text)
        subject_text = _event_subject_text(page.title, page.text)
        in_china = _is_domestic_event_subject(
            page.title,
            subject_text,
            approved_domestic_source=_is_approved_domestic_source(page.final_url),
        )
        category = self._category(normalized)
        if (
            category is not None
            and category is not Category.COMMERCIAL_SPACE_FINANCING
            and _contains_any(normalized, self._KEY_SUBSYSTEM_TERMS)
            and not self._has_explicit_parent_purpose(normalized, category)
        ):
            category = None
        excluded_financing = (
            category is Category.COMMERCIAL_SPACE_FINANCING
            and _is_excluded_financing(normalized)
        )
        lifecycle = (
            _contains_any(normalized, self._FINANCING_TERMS)
            if category is Category.COMMERCIAL_SPACE_FINANCING
            else _contains_any(normalized, self._PROCUREMENT_TERMS)
        )
        in_scope = bool(in_china and category and lifecycle and not excluded_financing)
        if not in_scope:
            category = None

        event_type = _event_type(normalized, category) if in_scope else None
        organization, organization_quote = (
            _extract_financing_organization(text)
            if category is Category.COMMERCIAL_SPACE_FINANCING
            else _extract_organization(page.text)
        )
        published_at, date_quote = _extract_date(page.text)
        financing_round, financing_round_quote = (
            _extract_financing_round(text)
            if category is Category.COMMERCIAL_SPACE_FINANCING
            else (None, None)
        )
        amount, amount_disclosed, amount_quote = (
            _extract_financing_amount(text)
            if category is Category.COMMERCIAL_SPACE_FINANCING
            else (None, None, None)
        )
        deadlines = (
            _extract_deadlines(page.text)
            if category is not Category.COMMERCIAL_SPACE_FINANCING
            else {}
        )
        evidence: list[Evidence] = []
        if in_china:
            country_quote = _domestic_subject_quote(
                subject_text
            ) or _first_line_matching(
                subject_text,
                ("中国境内", "我国境内", "国内项目", "中国", "我国", "国内"),
            ) or _first_domestic_line(subject_text)
            evidence.append(
                Evidence(
                    field="in_china",
                    quote=country_quote or page.final_url,
                    source_url=page.final_url,
                )
            )
        if in_scope and category is not None and event_type is not None:
            category_quote = _first_line_matching(
                text, self._CATEGORY_TERMS[category]
            )
            event_quote = _first_line_matching(
                text, _event_type_terms(event_type)
            )
            combined_scope_quote = next(
                (
                    line.strip()
                    for line in text.splitlines()
                    if line.strip()
                    and _contains_any(_normalize(line), self._CATEGORY_TERMS[category])
                    and _contains_any(_normalize(line), _event_type_terms(event_type))
                ),
                None,
            )
            scope_quote = (
                combined_scope_quote
                or category_quote
                or event_quote
                or _first_nonempty_line(page.text)
            )
            evidence.extend(
                (
                    Evidence(
                        field="in_scope",
                        quote=scope_quote,
                        source_url=page.final_url,
                    ),
                    Evidence(
                        field="category",
                        quote=category_quote or scope_quote,
                        source_url=page.final_url,
                    ),
                    Evidence(
                        field="event_type",
                        quote=event_quote or scope_quote,
                        source_url=page.final_url,
                    ),
                )
            )
        if page.title.strip():
            evidence.append(
                Evidence(
                    field="title",
                    quote=page.title.strip(),
                    source_url=page.final_url,
                )
            )
        if organization and organization_quote:
            evidence.append(
                Evidence(
                    field="organization",
                    quote=organization_quote,
                    source_url=page.final_url,
                )
            )
        if published_at and date_quote:
            evidence.append(
                Evidence(
                    field="published_at",
                    quote=date_quote,
                    source_url=page.final_url,
                )
            )
        if financing_round and financing_round_quote:
            evidence.append(
                Evidence(
                    field="financing_round",
                    quote=financing_round_quote,
                    source_url=page.final_url,
                )
            )
        if amount_quote:
            evidence.append(
                Evidence(
                    field="amount",
                    quote=amount_quote,
                    source_url=page.final_url,
                )
            )
        for deadline_name, (_, _, quote) in deadlines.items():
            evidence.append(
                Evidence(
                    field=f"{deadline_name}_deadline",
                    quote=quote,
                    source_url=page.final_url,
                )
            )
        return AnalysisResult(
            in_china=in_china,
            in_scope=in_scope,
            category=category,
            event_type=event_type,
            title=page.title.strip() or _first_nonempty_line(page.text),
            organization=organization,
            published_at=published_at,
            amount=amount,
            amount_disclosed=amount_disclosed,
            financing_round=financing_round,
            financing_subtype=(
                "round_equity" if financing_round is not None else None
            ),
            keywords=_matched_keywords(normalized, category, self._CATEGORY_TERMS),
            evidence=evidence,
            registration_deadline=(
                deadlines["registration"][0]
                if "registration" in deadlines
                else None
            ),
            bid_submission_deadline=(
                deadlines["bid_submission"][0]
                if "bid_submission" in deadlines
                else None
            ),
            opening_deadline=(
                deadlines["opening"][0] if "opening" in deadlines else None
            ),
            deadline_precision={
                name: precision
                for name, (_, precision, _) in deadlines.items()
            },
            source_url=page.final_url,
            degraded=True,
        )

    @classmethod
    def _category(cls, normalized: str) -> Category | None:
        if _contains_any(normalized, cls._FINANCING_TERMS) and _contains_any(
            normalized,
            cls._CATEGORY_TERMS[Category.COMMERCIAL_SPACE_FINANCING],
        ):
            return Category.COMMERCIAL_SPACE_FINANCING
        for category in (
            Category.LASER_COMMUNICATION,
            Category.LASER_WEAPON,
            Category.EO_TURRET,
        ):
            if _contains_any(normalized, cls._CATEGORY_TERMS[category]):
                return category
        if _contains_any(
            normalized,
            cls._CATEGORY_TERMS[Category.COMMERCIAL_SPACE_FINANCING],
        ):
            return Category.COMMERCIAL_SPACE_FINANCING
        return None

    @classmethod
    def _has_explicit_parent_purpose(
        cls, normalized: str, category: Category
    ) -> bool:
        parent_terms = cls._CATEGORY_TERMS[category]
        purpose_markers = ("用于", "应用于", "面向", "服务于", "配套")
        if any(
            _normalize(marker) + _normalize(parent) in normalized
            for marker in purpose_markers
            for parent in parent_terms
        ):
            return True
        return any(
            _normalize(parent) + _normalize("配套") + _normalize(subsystem)
            in normalized
            for parent in parent_terms
            for subsystem in cls._KEY_SUBSYSTEM_TERMS
        )


class ResilientAnalyzer:
    """Use deterministic analysis after exhaustion and to fill safe omissions."""

    def __init__(self, primary: Any, fallback: RuleFallbackAnalyzer) -> None:
        self._primary = primary
        self._fallback = fallback

    def analyze(self, page: FetchedPage) -> AnalysisResult:
        try:
            primary = self._primary.analyze(page)
        except AnalysisExhausted:
            return self._fallback.analyze(page)
        return self._enrich_missing_fields(primary, self._fallback.analyze(page))

    @staticmethod
    def _enrich_missing_fields(
        primary: AnalysisResult,
        fallback: AnalysisResult,
    ) -> AnalysisResult:
        if not primary.in_china or not primary.in_scope or not fallback.in_scope:
            return primary
        if (
            primary.category is not None
            and fallback.category is not None
            and primary.category is not fallback.category
        ):
            return primary
        if (
            primary.event_type is not None
            and fallback.event_type is not None
            and primary.event_type is not fallback.event_type
        ):
            return primary

        updates: dict[str, Any] = {}
        filled_evidence_fields: set[str] = set()
        simple_fields = (
            "category",
            "event_type",
            "organization",
            "published_at",
            "financing_round",
            "financing_subtype",
            "registration_deadline",
            "bid_submission_deadline",
            "opening_deadline",
        )
        for field_name in simple_fields:
            if getattr(primary, field_name) is None:
                fallback_value = getattr(fallback, field_name)
                if fallback_value is not None:
                    updates[field_name] = fallback_value
                    filled_evidence_fields.add(field_name)

        if primary.amount is None and primary.amount_disclosed is None:
            if fallback.amount is not None or fallback.amount_disclosed is not None:
                updates["amount"] = fallback.amount
                updates["amount_disclosed"] = fallback.amount_disclosed
                filled_evidence_fields.add("amount")

        deadline_precision = dict(primary.deadline_precision)
        for name, precision in fallback.deadline_precision.items():
            if name not in deadline_precision:
                deadline_precision[name] = precision
        if deadline_precision != primary.deadline_precision:
            updates["deadline_precision"] = deadline_precision

        evidence = list(primary.evidence)
        existing = {
            (item.field, item.quote, item.source_url)
            for item in evidence
        }
        supplemental_evidence_fields = set(filled_evidence_fields)
        if (
            primary.category is fallback.category
            and primary.event_type is fallback.event_type
            and primary.in_china is fallback.in_china
            and primary.in_scope is fallback.in_scope
        ):
            supplemental_evidence_fields.update(
                {"in_china", "in_scope", "category", "event_type"}
            )
        if (
            primary.published_at is not None
            and fallback.published_at is not None
            and primary.published_at.date() == fallback.published_at.date()
        ):
            # The deterministic fallback reads the publication date extracted
            # from page metadata/JSON-LD/visible headers. Preserve that quote as
            # evidence even when the model already supplied the same date.
            supplemental_evidence_fields.add("published_at")
        evidence_changed = False
        for item in fallback.evidence:
            if (
                item.field in supplemental_evidence_fields
                and (item.field, item.quote, item.source_url) not in existing
            ):
                evidence.append(item)
                existing.add((item.field, item.quote, item.source_url))
                evidence_changed = True
        if not updates and not evidence_changed:
            return primary
        updates["evidence"] = evidence
        updates["degraded"] = True
        return primary.model_copy(update=updates)

    @property
    def deepseek_tokens(self) -> int:
        """Expose primary analyzer usage without coupling callers to its type."""
        value = getattr(self._primary, "deepseek_tokens", 0)
        return value if isinstance(value, int) and value >= 0 else 0


def guard_grounded_output(
    result: AnalysisResult, page: FetchedPage
) -> AnalysisResult:
    """Reject structured claims that cannot be tied to the fetched final page."""

    if result.source_url != page.final_url:
        raise UngroundedOutput("source_url")
    for item in result.evidence:
        if (
            item.source_url != page.final_url
            or not item.quote.strip()
            or (
                item.quote not in page.text
                and item.quote not in page.title
                and not (
                    item.field == "in_china" and item.quote == page.final_url
                )
            )
        ):
            raise UngroundedOutput("evidence")

    for field_name in ("organization", "amount", "financing_round", "business_area"):
        value = getattr(result, field_name)
        if value is not None and (
            not value.strip()
            or not _claim_is_grounded(value, field_name, result.evidence, page.text)
            or (
                field_name == "business_area"
                and not any(
                    item.field == "business_area"
                    and _normalize(value) in _normalize(item.quote)
                    for item in result.evidence
                )
            )
        ):
            raise UngroundedOutput(field_name)
    for investor in result.investors:
        if not investor.strip() or not _claim_is_grounded(
            investor, "investors", result.evidence, page.text
        ):
            raise UngroundedOutput("investors")
    for project_code in result.project_codes:
        if not project_code.strip() or not _claim_is_grounded(
            project_code, "project_codes", result.evidence, page.text
        ):
            raise UngroundedOutput("project_codes")
    if result.published_at is not None:
        page_text = _normalize(page.text)
        if not any(
            _normalize(form) in page_text for form in _date_forms(result.published_at)
        ):
            raise UngroundedOutput("published_at")
    if not result.title.strip() or _normalize_claim(result.title) not in _normalize_claim(
        f"{page.title}\n{page.text}"
    ):
        raise UngroundedOutput("title")
    return result


def _claim_is_grounded(
    value: str, field_name: str, evidence: Sequence[Evidence], body_text: str
) -> bool:
    needle = _normalize(value)
    if needle in _normalize(body_text):
        return True
    aliases = {field_name}
    if field_name == "investors":
        aliases.add("investor")
    if field_name == "project_codes":
        aliases.add("project_code")
    return any(
        item.field in aliases and needle in _normalize(item.quote) for item in evidence
    )


def _apply_financing_exclusions(
    result: AnalysisResult, page: FetchedPage
) -> AnalysisResult:
    normalized = _normalize(f"{page.title}\n{page.text}")
    is_financing = (
        result.category is Category.COMMERCIAL_SPACE_FINANCING
        or result.event_type is EventType.FINANCING
    )
    if is_financing and _is_excluded_financing(normalized):
        return result.model_copy(
            update={
                "in_scope": False,
                "category": None,
                "event_type": None,
                "financing_round": None,
                "investors": [],
            }
        )
    return result


def _conservative_match() -> MatchSuggestion:
    return MatchSuggestion(
        relation="suspected",
        confidence=0,
        reason="模型输出不可用，需人工复核",
    )


def _rule_trend_summary(
    event_count: int, category_counts: dict[Category, int]
) -> str:
    if event_count == 0:
        return "统计期内无已核验事件。"
    details = "、".join(
        f"{category.value} {count}条"
        for category, count in sorted(
            category_counts.items(), key=lambda item: item[0].value
        )
    )
    return f"统计期内共有{event_count}条已核验事件：{details}。"


def _event_type(normalized: str, category: Category | None) -> EventType | None:
    if category is Category.COMMERCIAL_SPACE_FINANCING:
        return EventType.FINANCING
    mappings = (
        (("采购意向",), EventType.PROCUREMENT_INTENTION),
        (("重新招标", "重招", "rebid"), EventType.REBID),
        (("中标候选", "候选"), EventType.CANDIDATE),
        (("中标", "成交", "award"), EventType.AWARD),
        (("废标", "失败"), EventType.FAILED),
        (("询价",), EventType.INQUIRY),
        (("比选",), EventType.COMPARISON),
        (("变更",), EventType.CHANGE),
        (("延期",), EventType.EXTENSION),
        (("终止",), EventType.TERMINATION),
        (("招标", "采购", "tender", "procurement"), EventType.TENDER),
    )
    for terms, event_type in mappings:
        if _contains_any(normalized, terms):
            return event_type
    return None


def _event_type_terms(event_type: EventType) -> tuple[str, ...]:
    return {
        EventType.PROCUREMENT_INTENTION: ("采购意向",),
        EventType.REBID: ("重新招标", "重招", "rebid"),
        EventType.CANDIDATE: ("中标候选", "候选"),
        EventType.AWARD: ("中标", "成交", "award"),
        EventType.FAILED: ("废标", "失败"),
        EventType.INQUIRY: ("询价",),
        EventType.COMPARISON: ("比选",),
        EventType.CHANGE: ("变更",),
        EventType.EXTENSION: ("延期",),
        EventType.TERMINATION: ("终止",),
        EventType.TENDER: ("招标", "采购", "tender", "procurement"),
        EventType.FINANCING: RuleFallbackAnalyzer._FINANCING_TERMS,
    }.get(event_type, ())


def _first_line_matching(text: str, terms: Sequence[str]) -> str | None:
    return next(
        (
            line.strip()
            for line in text.splitlines()
            if line.strip() and _contains_any(_normalize(line), terms)
        ),
        None,
    )


def _first_domestic_line(text: str) -> str | None:
    return next(
        (
            line.strip()
            for line in text.splitlines()
            if line.strip()
            and _has_domestic_signal(line)
            and not _has_explicit_foreign_signal(line)
        ),
        None,
    )


def _event_subject_text(title: str, body: str) -> str:
    selected: list[str] = []
    total = 0
    for raw in (title, *body.splitlines()):
        for segment in re.split(r"(?<=[。！？!?；;])\s*", raw):
            value = segment.strip()
            if not value or value in selected:
                continue
            if len(value) > 600:
                value = value[:600].rstrip()
            selected.append(value)
            total += len(value)
            if len(selected) >= 8 or total >= 1_800:
                return "\n".join(selected)
    return "\n".join(selected)


def _is_domestic_event_subject(
    title: str,
    subject_text: str,
    *,
    approved_domestic_source: bool,
) -> bool:
    if _has_strong_domestic_event_subject(title):
        return True
    if _has_foreign_event_subject(title):
        return False
    if _has_strong_domestic_event_subject(subject_text):
        return True
    if _has_foreign_event_subject(subject_text):
        return False
    return (
        _has_domestic_signal(subject_text) or approved_domestic_source
    ) and not _has_explicit_foreign_signal(subject_text)


def _has_strong_domestic_event_subject(text: str) -> bool:
    if not _has_event_action(text):
        return False
    if _domestic_subject_quote(text) is not None:
        return True
    return any(
        cue in text
        for cue in (
            "中国境内",
            "我国境内",
            "国内项目",
            "中国商业航天企业",
            "国内商业航天企业",
        )
    )


def _has_foreign_event_subject(text: str) -> bool:
    return _has_explicit_foreign_signal(text) and _has_event_action(text)


def _has_event_action(text: str) -> bool:
    normalized = _normalize(text)
    return _contains_any(
        normalized,
        (
            *RuleFallbackAnalyzer._PROCUREMENT_TERMS,
            *RuleFallbackAnalyzer._FINANCING_TERMS,
        ),
    )


def _domestic_subject_quote(text: str) -> str | None:
    locations = "|".join(re.escape(item) for item in _DOMESTIC_LOCATION_PREFIXES)
    matches = (
        item.group(0).strip()
        for item in re.finditer(
            rf"(?:{locations})(?:市)?[\u4e00-\u9fffA-Za-z0-9·]{{2,40}}?"
            r"(?:股份有限公司|有限责任公司|有限公司|公安局|部队|单位|研究院|研究所|中心|大学)",
            text,
        )
    )
    return min(
        (item for item in matches if item),
        key=_domestic_subject_rank,
        default=None,
    )


def _domestic_subject_rank(value: str) -> tuple[int, int, str]:
    if value.endswith(("股份有限公司", "有限责任公司", "有限公司")):
        entity_rank = 0
    elif value.endswith(("公安局", "部队", "单位")):
        entity_rank = 1
    elif value.endswith(("研究院", "研究所", "中心")):
        entity_rank = 2
    else:
        entity_rank = 3
    return entity_rank, len(value), value


def _extract_organization(text: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"(?:采购人|招标人|采购单位|组织机构|Organization)\s*[:：]\s*([^\r\n。；;]{2,100})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None
    value = match.group(1).strip()
    return value, match.group(0).strip()


_FINANCING_COMPANY_ACTION = re.compile(
    r"(?P<company>[\u4e00-\u9fffA-Za-z0-9·]{2,60}?)"
    r"(?:（[^）\r\n]{0,40}）|\([^)\r\n]{0,40}\))?"
    r"[\s，,：:]*"
    r"(?:已于近日|于近日|于近期|近日|日前|连续|再度|再|已|正式|宣布)?"
    r"(?:获|获得|完成).{0,20}?(?:融资|投资)",
    flags=re.IGNORECASE,
)
_FINANCING_COMPANY_PREFIXES = (
    "商业航天企业",
    "商业航天公司",
    "星地激光通信企业",
    "空间激光通信企业",
    "激光通信企业",
    "火箭新锐公司",
    "航天新锐公司",
    "卫星公司",
)
_FINANCING_COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "科技有限公司",
    "有限公司",
)
_DOMESTIC_LOCATION_PREFIXES = (
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
_FINANCING_COMPANY_LEADING_CONTEXT = re.compile(
    r"^(?:"
    r"\d{4}年\d{1,2}月\d{1,2}日"
    r"|\d{1,2}月\d{1,2}日"
    r"|据[^，,。]{1,20}(?:消息|报道)"
    r")[，,、：:\s]*"
)
_FINANCING_COMPANY_TRAILING_MODIFIERS = (
    "已于近日",
    "于近日",
    "于近期",
    "近日",
    "日前",
    "连续",
    "再度",
    "宣布",
    "正式",
    "再",
    "已",
)
_FINANCING_COMPANY_TRAILING_DESCRIPTORS = (
    "商业航天企业",
    "商业航天公司",
    "商业航天",
    "星地激光通信企业",
    "空间激光通信企业",
    "激光通信企业",
    "激光通信",
)
_FINANCING_COMPANY_INVALID_TOKENS = (
    "资本加持",
    "融资消息",
    "融资事件",
    "投资消息",
    "完成融资",
    "获得融资",
)
_FINANCING_ROUND_PATTERN = re.compile(
    r"(?i)(pre[\s-]?[a-d]\+{0,2}|[a-d]\+{0,2}|"
    r"天使\+{0,2}|种子|战略投资|战略)\s*轮"
)
_FINANCING_AMOUNT_PATTERN = re.compile(
    r"(?:人民币)?(?:"
    r"(?:近|超|逾|约|数)(?:\d+(?:\.\d+)?)?(?:亿|千万|百万|万)元"
    r"|(?:\d+(?:\.\d+)?|[一二三四五六七八九十百]+)"
    r"(?:亿|千万|百万|万)元"
    r"|(?:亿|千万|百万|万)元级"
    r")(?:人民币)?"
)
_UNDISCLOSED_AMOUNT_PATTERN = re.compile(
    r"(?:具体)?(?:融资)?金额(?:暂)?未披露"
)


def _extract_financing_organization(text: str) -> tuple[str | None, str | None]:
    candidates: list[tuple[tuple[int, int, int], str, str]] = []
    for line in (item.strip() for item in text.splitlines() if item.strip()):
        for matched in _FINANCING_COMPANY_ACTION.finditer(line):
            raw_company = matched.group("company").strip(" ，,：:丨|")
            company = _FINANCING_COMPANY_LEADING_CONTEXT.sub("", raw_company)
            changed = True
            while changed:
                changed = False
                for prefix in _FINANCING_COMPANY_PREFIXES:
                    if company.startswith(prefix):
                        company = company[len(prefix) :]
                        changed = True
                        break
            had_legal_suffix = False
            for suffix in _FINANCING_COMPANY_SUFFIXES:
                if company.endswith(suffix):
                    company = company[: -len(suffix)]
                    had_legal_suffix = True
                    break
            for prefix in _DOMESTIC_LOCATION_PREFIXES:
                if company.startswith(prefix):
                    company = company[len(prefix) :]
                    break
            changed = True
            while changed:
                changed = False
                for modifier in (
                    *_FINANCING_COMPANY_TRAILING_MODIFIERS,
                    *_FINANCING_COMPANY_TRAILING_DESCRIPTORS,
                ):
                    if company.endswith(modifier):
                        company = company[: -len(modifier)]
                        changed = True
                        break
            company = company.strip(" ，,：:丨|")
            if not 2 <= len(company) <= 40:
                continue
            if (
                company.startswith(("再获", "获", "获得", "完成"))
                or any(token in company for token in _FINANCING_COMPANY_INVALID_TOKENS)
            ):
                continue
            candidates.append(
                (
                    (
                        0 if had_legal_suffix else 1,
                        0 if len(company) <= 16 else 1,
                        matched.start(),
                    ),
                    company,
                    line,
                )
            )
    if candidates:
        _, company, quote = min(candidates, key=lambda item: item[0])
        return company, quote
    return None, None


def _extract_financing_round(text: str) -> tuple[str | None, str | None]:
    matched = _FINANCING_ROUND_PATTERN.search(text)
    if not matched:
        return None, None
    raw = re.sub(r"\s+", "", matched.group(0))
    raw = re.sub(r"(?i)^pre-", "Pre-", raw)
    return raw, matched.group(0)


def _extract_financing_amount(
    text: str,
) -> tuple[str | None, bool | None, str | None]:
    disclosed = _FINANCING_AMOUNT_PATTERN.search(text)
    if disclosed and disclosed.group(0):
        value = disclosed.group(0).strip()
        return value, True, value
    undisclosed = _UNDISCLOSED_AMOUNT_PATTERN.search(text)
    if undisclosed:
        return None, False, undisclosed.group(0)
    return None, None, None


def _extract_date(text: str) -> tuple[datetime | None, str | None]:
    patterns = (
        r"(?P<year>20\d{2})-(?P<month>0?[1-9]|1[0-2])-(?P<day>0?[1-9]|[12]\d|3[01])",
        r"(?P<year>20\d{2})/(?P<month>0?[1-9]|1[0-2])/(?P<day>0?[1-9]|[12]\d|3[01])",
        r"(?P<year>20\d{2})\.(?P<month>0?[1-9]|1[0-2])\.(?P<day>0?[1-9]|[12]\d|3[01])",
        r"(?P<year>20\d{2})年(?P<month>0?[1-9]|1[0-2])月(?P<day>0?[1-9]|[12]\d|3[01])日",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            value = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                tzinfo=BEIJING,
            )
        except ValueError:
            continue
        return value, match.group(0)
    return None, None


def _extract_deadlines(
    text: str,
) -> dict[str, tuple[datetime, Literal["date", "minute", "second"], str]]:
    labels = {
        "registration": r"(?:报名截止时间|报名时间截止|报名截止|登记截止时间)",
        "bid_submission": r"(?:投标截止时间|投标截止|响应文件提交截止时间|递交截止时间)",
        "opening": r"(?:开标时间|开启时间)",
    }
    date_pattern = (
        r"(?P<year>20\d{2})[-/.年](?P<month>1[0-2]|0?[1-9])[-/.月]"
        r"(?P<day>3[01]|[12]\d|0?[1-9])(?!\d)日?"
    )
    time_pattern = (
        r"(?:\s+(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)"
        r"(?::(?P<second>[0-5]\d))?)?"
    )
    extracted: dict[
        str, tuple[datetime, Literal["date", "minute", "second"], str]
    ] = {}
    for name, label in labels.items():
        match = re.search(
            rf"{label}\s*[:：]?\s*{date_pattern}{time_pattern}", text
        )
        if not match:
            continue
        precision: Literal["date", "minute", "second"]
        if match.group("second") is not None:
            precision = "second"
        elif match.group("hour") is not None:
            precision = "minute"
        else:
            precision = "date"
        try:
            value = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour") or 0),
                int(match.group("minute") or 0),
                int(match.group("second") or 0),
                tzinfo=BEIJING,
            )
        except ValueError:
            continue
        extracted[name] = (value, precision, match.group(0).strip())
    return extracted


def _date_forms(value: datetime) -> set[str]:
    year, month, day = value.year, value.month, value.day
    return {
        f"{year:04d}-{month:02d}-{day:02d}",
        f"{year:04d}/{month:02d}/{day:02d}",
        f"{year:04d}.{month:02d}.{day:02d}",
        f"{year}年{month}月{day}日",
        f"{year}年{month:02d}月{day:02d}日",
    }


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _normalize_claim(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def _contains_any(normalized: str, terms: Sequence[str]) -> bool:
    return any(_normalize(term) in normalized for term in terms)


def _is_excluded_financing(normalized: str) -> bool:
    if _contains_any(normalized, RuleFallbackAnalyzer._FINANCING_EXCLUSIONS):
        return True
    bank_credit = "银行" in normalized and _contains_any(
        normalized, ("授信", "贷款", "信贷", "借款")
    )
    listed_refinancing = "上市公司" in normalized and _contains_any(
        normalized, ("再融资", "定向增发", "配股", "可转债")
    )
    return bank_credit or listed_refinancing


def _matched_keywords(
    normalized: str,
    category: Category | None,
    terms: dict[Category, tuple[str, ...]],
) -> list[str]:
    if category is None:
        return []
    return [term for term in terms[category] if _normalize(term) in normalized]


def _first_nonempty_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _is_approved_domestic_source(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host.endswith(".gov.cn") or host.endswith(".mil.cn")


def _has_domestic_signal(text: str) -> bool:
    if any(term in text for term in ("中国境内", "我国境内", "国内项目")):
        return True
    if re.search(
        r"(?:中国|我国|国内).{0,8}(?:企业|机构|单位|政府|军队|项目)", text
    ) is not None:
        return True
    locations = "|".join(re.escape(item) for item in _DOMESTIC_LOCATION_PREFIXES)
    return re.search(
        rf"(?:{locations})(?:市)?[\u4e00-\u9fffA-Za-z0-9·]{{2,40}}"
        r"(?:有限公司|研究院|研究所|公安局|大学|中心)",
        text,
    ) is not None


def _has_explicit_foreign_signal(text: str) -> bool:
    return any(
        cue in text
        for cue in (
            "美国",
            "美军",
            "美国国防部",
            "英国",
            "俄罗斯",
            "俄军",
            "法国",
            "德国",
            "日本",
            "韩国",
            "印度",
            "以色列",
            "乌克兰",
            "北约",
            "欧盟",
            "境外",
            "国外",
        )
    )
