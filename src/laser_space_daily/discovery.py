"""Discovery-only search and official-source collection for China intelligence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable, Literal
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

    def search(self, query: SearchQuery) -> list[Candidate]:
        self._usage_count += 1
        try:
            response = self._client.post(
                self._SEARCH_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "query": query.text,
                    "freshness": "oneMonth",
                    "summary": True,
                    "count": 10,
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
) -> CandidateSelection:
    """Apply the approved shape, relevance and date gates to web-search rows."""
    if now.tzinfo is None:
        raise ValueError("selection time must include a timezone")
    if minimum < 0 or maximum < minimum:
        raise ValueError("candidate bounds must satisfy 0 <= minimum <= maximum")

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
    for row, score in relevant:
        normalized_url = normalize_url(row.url)
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        deduplicated.append((row.model_copy(update={"url": normalized_url}), score))

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
        elif age <= timedelta(days=30):
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
    if any(term in text for term in _EXCLUDED_NOISE_TERMS):
        return None

    category = row.category_hint
    if category is Category.COMMERCIAL_SPACE_FINANCING:
        subject_hits = _term_hits(text, _FINANCING_SUBJECT_TERMS)
        event_hits = _term_hits(text, _FINANCING_EVENT_TERMS)
        if not subject_hits or not event_hits:
            return None
        return row, subject_hits + event_hits

    if category in _CATEGORY_TERMS:
        hits = _term_hits(text, _CATEGORY_TERMS[category])
        return (row, hits) if hits else None

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
        hits = _term_hits(text, terms)
        if hits:
            return row.model_copy(update={"category_hint": inferred_category}), hits
    return None


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
