"""Discovery-only search and official-source collection for China intelligence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re
from typing import Iterable, Literal
import unicodedata
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, FeatureNotFound
from pydantic import BaseModel, ConfigDict
from soupsieve import SelectorSyntaxError

from .models import Candidate, Category, Project, SourceGrade
from .deadlines import deadline_is_expired
from .timebox import beijing_now


QueryKind = Literal["incremental", "project_followup", "rolling_recheck", "overdue_result"]
SearchFailureReason = Literal[
    "authentication",
    "quota_or_rate_limit",
    "network_or_timeout",
    "server_error",
    "request_rejected",
    "invalid_response",
]


class DiscoveryError(RuntimeError):
    """Base class for controlled discovery-provider failures."""

    default_reason: SearchFailureReason = "request_rejected"

    def __init__(
        self,
        message: str,
        *,
        reason: SearchFailureReason | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason or self.default_reason


class DiscoveryQuotaError(DiscoveryError):
    """Raised when a discovery provider rejects an attempt for quota/rate limits."""

    default_reason: SearchFailureReason = "quota_or_rate_limit"


class DiscoveryUnavailableError(DiscoveryError):
    """Raised when a discovery provider or its network is temporarily unavailable."""


class DiscoveryConfigurationError(DiscoveryUnavailableError):
    """Raised when discovery credentials or request authorization are rejected."""

    default_reason: SearchFailureReason = "authentication"


@dataclass(frozen=True)
class SearchQuery:
    kind: QueryKind
    text: str
    category: Category | None = None


@dataclass(frozen=True)
class CandidateSelection:
    """A bounded, deterministic set of usable search candidates and funnel counts."""

    candidates: tuple[Candidate, ...]
    raw_search_count: int
    valid_shape_count: int
    relevance_pass_count: int
    recent_7d_count: int
    fallback_8_30d_count: int
    unknown_date_count: int
    filter_rejected_count: int = 0
    event_duplicate_count: int = 0


class OfficialSeed(BaseModel):
    """One configured Grade-A official list page and its narrow link pattern."""

    model_config = ConfigDict(extra="forbid")

    name: str
    domain: str
    grade: SourceGrade
    list_urls: list[str]
    link_selector: str


class QueryPlanner:
    """Build a bounded search queue that preserves category coverage first."""

    _DISCOVERY_SCOPE = "中国 境内 -人工智能新闻 -AI新闻"
    _INCREMENTAL_QUERIES = (
        (
            Category.LASER_COMMUNICATION,
            "激光通信 空间激光通信 星间激光通信 激光通信终端 "
            "采购 招标 中标 结果 变更 延期 终止",
        ),
        (
            Category.LASER_WEAPON,
            "激光武器 高能激光 定向能激光 激光反无人机 激光反制 "
            "采购 招标 中标 结果 变更 延期 终止",
        ),
        (
            Category.EO_TURRET,
            "光电转塔 光电吊舱 机载光电 舰载光电 无人机光电载荷 "
            "采购 招标 中标 结果 变更 延期 终止",
        ),
        (
            Category.COMMERCIAL_SPACE_FINANCING,
            "商业航天 融资 运载火箭 卫星公司 卫星制造 卫星运营 "
            "股权投资 战略投资 增资 天使轮 种子轮 Pre-A轮 A轮 B轮",
        ),
    )
    _INACTIVE_STATUSES = frozenset({"completed", "closed", "cancelled", "terminated"})

    def __init__(
        self,
        max_queries: int = 40,
        financing_domains: Iterable[str] = (),
    ) -> None:
        if max_queries < 0:
            raise ValueError("max_queries must be non-negative")
        self.max_queries = max_queries
        self.financing_domains = tuple(sorted(set(financing_domains)))

    def plan(self, now: object, projects: Iterable[Project]) -> list[SearchQuery]:
        """Return category queries then three lifecycle follow-ups per active project."""
        if not isinstance(now, datetime):
            raise TypeError("planner now must be a datetime")
        queries = [
            SearchQuery(kind="incremental", text=text, category=category)
            for category, text in self._INCREMENTAL_QUERIES
        ]
        queries.extend(
            SearchQuery(
                kind="incremental",
                text=f"site:{domain} 商业航天 融资 投资 增资",
                category=Category.COMMERCIAL_SPACE_FINANCING,
            )
            for domain in self.financing_domains
        )
        for project in projects:
            if project.status.lower() in self._INACTIVE_STATUSES:
                continue
            project_identity = (
                f"{project.name} {project.organization} {project.project_id}"
            )
            queries.extend(
                (
                    SearchQuery(
                        kind="project_followup",
                        text=f"{project_identity} 采购 招标 中标",
                        category=project.category,
                    ),
                    SearchQuery(
                        kind="rolling_recheck",
                        text=f"{project_identity} 变更 延期 终止",
                        category=project.category,
                    ),
                )
            )
            if self._is_result_overdue(project, now):
                queries.append(
                    SearchQuery(
                        kind="overdue_result",
                        text=f"{project_identity} 中标 结果 流标 重新招标",
                        category=project.category,
                    )
                )
        return [
            SearchQuery(
                kind=query.kind,
                text=f"{query.text} {self._DISCOVERY_SCOPE}",
                category=query.category,
            )
            for query in queries[: self.max_queries]
        ]

    @staticmethod
    def _is_result_overdue(project: Project, now: datetime) -> bool:
        supported = [
            (project.deadlines[name], project.deadline_precision[name])
            for name in ("bid_submission", "opening")
            if name in project.deadlines
            and name in project.deadline_evidence
            and name in project.deadline_precision
        ]
        if not supported:
            return False
        return all(
            deadline_is_expired(deadline, precision, now)
            for deadline, precision in supported
        )

    @staticmethod
    def _datetime_key(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class BochaProvider:
    """Map Bocha Web Search responses to unverified Candidate records."""

    _SEARCH_URL = "https://api.bochaai.com/v1/web-search"

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self._client = client or httpx.Client()
        self._usage_count = 0

    @property
    def usage_count(self) -> int:
        """Return lifetime attempted searches, including controlled failures."""
        return self._usage_count

    def search(
        self,
        query: SearchQuery,
        *,
        freshness: str = "oneMonth",
        count: int = 10,
    ) -> list[Candidate]:
        if freshness not in {"oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"}:
            raise ValueError("unsupported Bocha freshness")
        if not 1 <= count <= 50:
            raise ValueError("Bocha result count must be between 1 and 50")
        self._usage_count += 1
        try:
            response = self._client.post(
                self._SEARCH_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "query": query.text,
                    "freshness": freshness,
                    "summary": True,
                    "count": count,
                },
            )
        except (httpx.TransportError, ConnectionError, TimeoutError) as error:
            raise DiscoveryUnavailableError(
                "bocha network unavailable",
                reason="network_or_timeout",
            ) from error
        if response.status_code == 429:
            raise DiscoveryQuotaError("bocha quota or rate limit exceeded")
        if response.status_code in {401, 403}:
            raise DiscoveryConfigurationError("bocha authentication rejected")
        if 500 <= response.status_code <= 599:
            raise DiscoveryUnavailableError(
                "bocha server unavailable",
                reason="server_error",
            )
        if not 200 <= response.status_code <= 299:
            raise DiscoveryUnavailableError(
                "bocha request rejected",
                reason="request_rejected",
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("response root must be an object")
            business_code = self._business_code(payload)
            if business_code not in {None, 0, 200}:
                self._raise_for_business_code(business_code)
            search_payload = self._search_payload(payload)
            web_pages = search_payload.get("webPages")
            if not isinstance(web_pages, dict):
                raise TypeError("webPages must be an object")
            results = web_pages.get("value")
            if not isinstance(results, list):
                raise TypeError("results must be a list")
            parsed_results: list[tuple[str, str, str, datetime | None]] = []
            for result in results:
                if not isinstance(result, dict):
                    continue
                title = result.get("name")
                url = result.get("url")
                summary = result.get("summary")
                snippet = result.get("snippet")
                if not isinstance(title, str) or not isinstance(url, str):
                    continue
                if summary is not None and not isinstance(summary, str):
                    summary = None
                if snippet is not None and not isinstance(snippet, str):
                    snippet = None
                if not title.strip() or not url.strip():
                    continue
                content = summary.strip() if isinstance(summary, str) else ""
                if not content and isinstance(snippet, str):
                    content = snippet.strip()
                parsed_results.append(
                    (
                        title.strip(),
                        url.strip(),
                        content,
                        _parse_source_published_at(result.get("datePublished")),
                    )
                )
            if results and not parsed_results:
                raise TypeError("response contains no valid result objects")
        except (TypeError, ValueError) as error:
            raise DiscoveryUnavailableError(
                "bocha response invalid",
                reason="invalid_response",
            ) from error
        discovered_at = beijing_now()
        return [
            Candidate(
                title=title,
                url=url,
                summary=content,
                discovered_at=discovered_at,
                discovery_source="bocha",
                category_hint=query.category,
                source_published_at=source_published_at,
            )
            for title, url, content, source_published_at in parsed_results
        ]

    @staticmethod
    def _business_code(payload: dict) -> int | None:
        value = payload.get("code")
        if value is None:
            return None
        if isinstance(value, bool):
            raise TypeError("business code must be numeric")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        raise TypeError("business code must be numeric")

    @staticmethod
    def _search_payload(payload: dict) -> dict:
        if "webPages" in payload:
            return payload
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TypeError("response data must be an object")
        return data

    @staticmethod
    def _raise_for_business_code(code: int) -> None:
        if code in {401, 403}:
            raise DiscoveryConfigurationError(
                f"bocha business authentication rejected (code {code})"
            )
        if code == 429:
            raise DiscoveryQuotaError(
                "bocha business quota or rate limit exceeded (code 429)"
            )
        if 500 <= code <= 599:
            raise DiscoveryUnavailableError(
                f"bocha business server unavailable (code {code})",
                reason="server_error",
            )
        raise DiscoveryUnavailableError(
            f"bocha business request rejected (code {code})",
            reason="request_rejected",
        )


class OfficialSeedCollector:
    """Collect linked notices without inferring any URL outside an official page."""

    def __init__(
        self, seeds: Iterable[OfficialSeed], client: httpx.Client | None = None
    ) -> None:
        self.seeds = tuple(seeds)
        self._client = client or httpx.Client()
        self._failed_domains: set[str] = set()

    @property
    def failed_domains(self) -> frozenset[str]:
        """Domains whose list page could not be fetched or selected this run."""
        return frozenset(self._failed_domains)

    def collect(self) -> list[Candidate]:
        rows: list[Candidate] = []
        self._failed_domains.clear()
        for seed in self.seeds:
            for list_url in seed.list_urls:
                try:
                    response = self._client.get(list_url)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "html.parser")
                    links = soup.select(seed.link_selector)
                except (httpx.HTTPError, FeatureNotFound, SelectorSyntaxError, UnicodeError):
                    self._failed_domains.add(seed.domain)
                    continue

                if not links:
                    self._failed_domains.add(seed.domain)
                    continue

                discovered_at = beijing_now()
                for link in links:
                    href = link.get("href")
                    if not isinstance(href, str) or not href.strip():
                        continue
                    rows.append(
                        Candidate(
                            title=link.get_text(" ", strip=True),
                            url=urljoin(list_url, href),
                            discovered_at=discovered_at,
                            discovery_source=f"official:{seed.domain}",
                        )
                    )
        return rows


_CATEGORY_TERMS: dict[Category, tuple[str, ...]] = {
    Category.LASER_COMMUNICATION: (
        "激光通信",
        "空间激光通信",
        "星间激光通信",
        "激光通信终端",
        "光通信终端",
        "laser communication",
        "laser terminal",
    ),
    Category.LASER_WEAPON: (
        "激光武器",
        "高能激光",
        "定向能激光",
        "激光反无人机",
        "激光反制",
        "laser weapon",
        "directed energy laser",
    ),
    Category.EO_TURRET: (
        "光电转塔",
        "光电吊舱",
        "机载光电",
        "舰载光电",
        "无人机光电载荷",
        "eo turret",
        "electro-optical turret",
    ),
}
_FINANCING_SUBJECT_TERMS = (
    "商业航天",
    "运载火箭",
    "火箭公司",
    "卫星公司",
    "卫星制造",
    "卫星运营",
    "commercial space",
)
_FINANCING_EVENT_TERMS = (
    "融资",
    "股权投资",
    "战略投资",
    "增资",
    "天使轮",
    "种子轮",
    "pre-a轮",
    "a轮",
    "b轮",
    "c轮",
    "d轮",
)
_FINANCING_SPECIFIC_SUBJECT_TERMS = (
    "航天",
    "火箭",
    "卫星",
    "太空",
    "空间科技",
)
_PROCUREMENT_EVENT_TERMS = (
    "采购意向",
    "采购公告",
    "采购项目",
    "采购",
    "招标",
    "询价",
    "比选",
    "竞争性谈判",
    "竞争性磋商",
    "候选人",
    "中标",
    "成交",
    "结果公告",
    "变更公告",
    "延期",
    "终止",
    "废标",
    "重新招标",
    "交付",
    "procurement",
    "tender",
    "request for proposal",
    "contract award",
)
_RESEARCH_REPORT_NOISE_TERMS = (
    "行业研究报告",
    "深度研究及发展前景",
    "发展深度洞察",
    "产业发展报告",
    "发展前景分析报告",
    "市场运行动态及发展前景",
    "市场调研报告",
    "报告目录",
    "报告摘要",
    "中国行业研究网",
    "iim信息",
)
_RESEARCH_REPORT_DOMAINS = (
    "chinairn.com",
    "iim.net.cn",
)
_ROUNDUP_TITLE_NOISE_TERMS = (
    "投融周报",
    "投融资周报",
    "融资周报",
    "投融资盘点",
    "融资盘点",
)
_MARKET_COMMENTARY_NOISE_TERMS = (
    "a股",
    "概念股",
    "板块拉涨",
    "板块上涨",
    "涨停",
    "高切低",
    "行情",
    "投资建议",
    "建议关注",
    "低位布局",
)
_NEGATED_EVENT_TERMS = (
    "没有具体采购事件",
    "无具体采购事件",
    "没有采购事件",
    "无采购事件",
    "没有具体融资事件",
    "无具体融资事件",
)
_EXCLUDED_NOISE_TERMS = (
    "激光打印机",
    "打印耗材",
    "硒鼓",
    "墨盒",
    "激光雕刻",
    "激光切割",
    "激光打标机",
    "激光美容",
    "激光脱毛",
    "激光祛斑",
    "医美",
    "医疗器械",
    "医用防护",
    "美容防护眼镜",
    "人工智能算法采购",
    "ai算法采购",
    "算力采购",
    "大模型采购",
    "软件采购",
)


def select_search_candidates(
    rows: Iterable[Candidate],
    now: datetime,
    *,
    minimum: int = 5,
    maximum: int = 10,
    fallback_max_days: int = 30,
) -> CandidateSelection:
    """Apply the approved shape, relevance and date gates to web-search rows."""
    if now.tzinfo is None:
        raise ValueError("selection time must include a timezone")
    if minimum < 0 or maximum < minimum:
        raise ValueError("candidate bounds must satisfy 0 <= minimum <= maximum")
    if fallback_max_days < 8 or fallback_max_days > 90:
        raise ValueError("fallback_max_days must be between 8 and 90")

    input_rows = list(rows)
    valid: list[Candidate] = []
    relevant: list[tuple[Candidate, int]] = []
    for row in input_rows:
        if not _has_usable_search_shape(row):
            continue
        valid.append(row)
        assessed = _assess_relevance(row)
        if assessed is not None:
            relevant.append(assessed)

    deduplicated: list[tuple[Candidate, int]] = []
    seen: set[str] = set()
    event_duplicate_count = 0
    for row, score in relevant:
        normalized_url = normalize_url(row.url)
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        normalized_row = row.model_copy(update={"url": normalized_url})
        if any(
            _same_search_event(normalized_row, existing)
            for existing, _ in deduplicated
        ):
            event_duplicate_count += 1
            continue
        deduplicated.append((normalized_row, score))

    recent: list[tuple[Candidate, int]] = []
    fallback: list[tuple[Candidate, int]] = []
    unknown: list[tuple[Candidate, int]] = []
    now_utc = now.astimezone(UTC)
    for row, score in deduplicated:
        published_at = row.source_published_at
        if published_at is None:
            unknown.append((row, score))
            continue
        published_utc = published_at.astimezone(UTC)
        if published_utc > now_utc + timedelta(hours=24):
            continue
        age = max(timedelta(0), now_utc - published_utc)
        if age <= timedelta(days=7):
            recent.append((row, score))
        elif age <= timedelta(days=fallback_max_days):
            fallback.append((row, score))

    recent.sort(key=_candidate_rank)
    fallback.sort(key=_candidate_rank)
    unknown.sort(key=_candidate_rank)

    selected = recent[:maximum]
    fallback_used: list[tuple[Candidate, int]] = []
    unknown_used: list[tuple[Candidate, int]] = []
    if len(selected) < minimum:
        fallback_used = fallback[: min(minimum - len(selected), maximum - len(selected))]
        selected.extend(fallback_used)
    if len(selected) < minimum:
        unknown_used = unknown[
            : min(2, minimum - len(selected), maximum - len(selected))
        ]
        selected.extend(unknown_used)

    return CandidateSelection(
        candidates=tuple(row for row, _score in selected),
        raw_search_count=len(input_rows),
        valid_shape_count=len(valid),
        relevance_pass_count=len(relevant),
        recent_7d_count=min(len(recent), maximum),
        fallback_8_30d_count=len(fallback_used),
        unknown_date_count=len(unknown_used),
        filter_rejected_count=len(valid) - len(relevant),
        event_duplicate_count=event_duplicate_count,
    )


def _parse_source_published_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _has_usable_search_shape(row: Candidate) -> bool:
    if not row.title.strip() or not row.summary.strip():
        return False
    parts = urlsplit(row.url)
    return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)


def _assess_relevance(row: Candidate) -> tuple[Candidate, int] | None:
    text = _normalized_candidate_text(row)
    if (
        any(term in text for term in _EXCLUDED_NOISE_TERMS)
        or _is_research_report_noise(row, text)
        or any(term in row.title.casefold() for term in _ROUNDUP_TITLE_NOISE_TERMS)
        or any(term in text for term in _MARKET_COMMENTARY_NOISE_TERMS)
        or any(term in text for term in _NEGATED_EVENT_TERMS)
    ):
        return None

    category = row.category_hint
    if category is Category.COMMERCIAL_SPACE_FINANCING:
        subject_hits = _term_hits(text, _FINANCING_SUBJECT_TERMS)
        event_hits = _term_hits(text, _FINANCING_EVENT_TERMS)
        specific_event = _has_specific_financing_event(row)
        if (
            not event_hits
            or not _title_has_financing_action(row)
            or (not subject_hits and not specific_event)
        ):
            return None
        return row, subject_hits + event_hits + (2 if specific_event else 0)

    if category in _CATEGORY_TERMS:
        subject_hits = _term_hits(text, _CATEGORY_TERMS[category])
        event_hits = _term_hits(text, _PROCUREMENT_EVENT_TERMS)
        return (
            (row, subject_hits + event_hits)
            if subject_hits and event_hits
            else None
        )

    financing_subject_hits = _term_hits(text, _FINANCING_SUBJECT_TERMS)
    financing_event_hits = _term_hits(text, _FINANCING_EVENT_TERMS)
    if financing_subject_hits and financing_event_hits:
        return (
            row.model_copy(
                update={"category_hint": Category.COMMERCIAL_SPACE_FINANCING}
            ),
            financing_subject_hits + financing_event_hits,
        )
    for inferred_category, terms in _CATEGORY_TERMS.items():
        subject_hits = _term_hits(text, terms)
        event_hits = _term_hits(text, _PROCUREMENT_EVENT_TERMS)
        if subject_hits and event_hits:
            return (
                row.model_copy(update={"category_hint": inferred_category}),
                subject_hits + event_hits,
            )
    return None


_ANNOUNCEMENT_CODE = re.compile(
    r"(?:项目|采购|招标|公告)?编号[：:\s]*([A-Za-z0-9][A-Za-z0-9._\-/]{4,})",
    re.IGNORECASE,
)


def _same_search_event(left: Candidate, right: Candidate) -> bool:
    if left.category_hint is not right.category_hint:
        return False
    if left.category_hint is Category.COMMERCIAL_SPACE_FINANCING:
        financing_match = _same_financing_event(left, right)
        if financing_match is not None:
            return financing_match
    left_code = _announcement_code(left)
    right_code = _announcement_code(right)
    if left_code and right_code:
        return left_code == right_code
    if _event_stage(left) != _event_stage(right):
        return False
    return _canonical_title(left.title) == _canonical_title(right.title)


def _announcement_code(row: Candidate) -> str | None:
    matched = _ANNOUNCEMENT_CODE.search(f"{row.title} {row.summary}")
    return matched.group(1).casefold() if matched else None


_QUOTED_COMPANY = re.compile(r"[「『“\"]([^」』”\"]{2,40})[」』”\"]")
_COMPANY_BEFORE_COMPLETED = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9·]{2,40}?)(?:宣布|正式|已)?完成"
)
_COMPANY_BEFORE_FINANCING = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9·]{2,40}?)(?:宣布|正式|已)?"
    r"(?:完成|获得|获)"
)
_COMPANY_PREFIXES = (
    "中国商业航天企业",
    "商业航天企业",
    "商业航天卫星公司",
    "商业航天公司",
    "卫星公司",
    "航天企业",
    "企业",
)
_FINANCING_ROUND = re.compile(
    r"(?i)(pre[\s-]?[a-d]\+{0,2}|[a-d]\+{0,2}|"
    r"天使\+{0,2}|种子|战略投资|战略|新一)\s*轮"
)


def _same_financing_event(left: Candidate, right: Candidate) -> bool | None:
    left_company = _financing_company(left)
    right_company = _financing_company(right)
    left_round = _financing_round(left)
    right_round = _financing_round(right)
    if not all((left_company, right_company, left_round, right_round)):
        return None
    if left_company != right_company or left_round != right_round:
        return False
    if left.source_published_at is None or right.source_published_at is None:
        return True
    return abs(
        (left.source_published_at.astimezone(UTC) -
         right.source_published_at.astimezone(UTC)).days
    ) <= 14


def _financing_company(row: Candidate) -> str | None:
    for text in (row.title, row.summary):
        quoted = _QUOTED_COMPANY.search(text)
        if quoted:
            return _normalize_company(quoted.group(1))
        completed = _COMPANY_BEFORE_COMPLETED.search(text)
        if completed:
            return _normalize_company(completed.group(1))
        financing = _COMPANY_BEFORE_FINANCING.search(text)
        if financing:
            return _normalize_company(financing.group(1))
    return None


def _normalize_company(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    for prefix in _COMPANY_PREFIXES:
        if normalized.startswith(prefix.casefold()):
            normalized = normalized[len(prefix) :]
            break
    return re.sub(r"[\s，,：:丨|]+", "", normalized)


def _financing_round(row: Candidate) -> str | None:
    matched = _FINANCING_ROUND.search(f"{row.title} {row.summary}")
    if matched is None:
        return None
    return re.sub(r"[\s-]+", "", matched.group(1).casefold())


def _title_has_financing_action(row: Candidate) -> bool:
    title = row.title.casefold()
    return any(term in title for term in ("融资", "投资", "增资"))


def _has_specific_financing_event(row: Candidate) -> bool:
    if _financing_company(row) is None:
        return False
    text = _normalized_candidate_text(row)
    return any(term in text for term in _FINANCING_SPECIFIC_SUBJECT_TERMS)


def _is_research_report_noise(row: Candidate, text: str) -> bool:
    if any(term in text for term in _RESEARCH_REPORT_NOISE_TERMS):
        return True
    hostname = (urlsplit(row.url).hostname or "").casefold()
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in _RESEARCH_REPORT_DOMAINS
    )


def _event_stage(row: Candidate) -> str:
    text = _normalized_candidate_text(row)
    stages = (
        ("termination", ("终止", "废标")),
        ("change", ("变更", "延期")),
        ("award", ("中标", "成交", "结果公告")),
        ("candidate", ("候选人",)),
        ("tender", ("招标", "采购公告", "询价", "比选")),
        ("intention", ("采购意向",)),
        ("delivery", ("交付",)),
        ("financing", _FINANCING_EVENT_TERMS),
    )
    return next(
        (stage for stage, terms in stages if any(term in text for term in terms)),
        "unknown",
    )


def _canonical_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    normalized = re.sub(r"[\s*｜|_\-—:：·•]+", "", normalized)
    normalized = normalized.replace("打印版", "")
    return re.sub(
        r"(?:中国行业研究网|腾讯新闻|新浪财经|搜狐网|网易新闻|"
        r"www\.[a-z0-9.-]+)$",
        "",
        normalized,
    )


def _normalized_candidate_text(row: Candidate) -> str:
    return " ".join(f"{row.title} {row.summary}".casefold().split())


def _term_hits(text: str, terms: Iterable[str]) -> int:
    return sum(1 for term in terms if term.casefold() in text)


def _candidate_rank(item: tuple[Candidate, int]) -> tuple[object, ...]:
    row, relevance_score = item
    published_at = row.source_published_at
    published_rank = (
        -published_at.astimezone(UTC).timestamp() if published_at is not None else 0
    )
    official_rank = 0 if row.discovery_source.startswith("official:") else 1
    return (published_rank, official_rank, -relevance_score, row.url)


_TRACKING_KEYS = frozenset({"spm", "from", "source"})


def normalize_url(url: str) -> str:
    """Remove URL presentation noise while retaining its location and query meaning."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))

    userinfo = ""
    if "@" in parts.netloc:
        userinfo = f"{parts.netloc.rsplit('@', 1)[0]}@"
    try:
        port = parts.port
    except ValueError:
        port = None
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    include_port = port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    )
    netloc = f"{userinfo}{display_host}{f':{port}' if include_port else ''}"
    components = [
        component
        for component in parts.query.split("&")
        if not _is_tracking_query_component(component)
    ]
    if all(_is_standard_query_pair(component) for component in components):
        components.sort()
    query = "&".join(components)
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def _is_tracking_query_component(component: str) -> bool:
    raw_key = component.partition("=")[0]
    decoded_key = unquote_plus(raw_key).lower()
    return decoded_key in _TRACKING_KEYS or decoded_key.startswith("utm_")


def _is_standard_query_pair(component: str) -> bool:
    key, separator, value = component.partition("=")
    return bool(key and separator and value)


def dedupe_candidates(rows: Iterable[Candidate]) -> list[Candidate]:
    """Keep the first discovery result for each normalized URL, in input order."""
    unique: list[Candidate] = []
    seen: set[str] = set()
    for row in rows:
        normalized = normalize_url(row.url)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(row.model_copy(update={"url": normalized}))
    return unique
