"""Bounded DeepSeek tool-calling orchestration for multi-round web discovery."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any, Literal

from .discovery import QueryPlanner, SearchQuery, normalize_url
from .models import Candidate, Category, Project


DiscoveryMode = Literal["daily", "backfill"]
StopReason = Literal[
    "budget_exhausted",
    "model_completed",
    "model_error",
    "no_new_candidates",
    "round_limit",
]

_DISCOVERY_SCOPE = "中国 境内 -人工智能新闻 -AI新闻"
_ALLOWED_INTENTS = frozenset(
    {"project_followup", "status_followup", "corroboration"}
)
_INTENT_TO_KIND = {
    "project_followup": "project_followup",
    "status_followup": "rolling_recheck",
    "corroboration": "project_followup",
}
_QUERY_NOISE_TERMS = (
    "激光粒度分析仪",
    "激光打印机",
    "激光切割",
    "激光美容",
    "医疗器械",
    "解放军总医院",
)
_SYSTEM_PROMPT = """你是中国激光与商业航天情报检索规划器。
你只能使用 search_web 工具提出后续搜索，不得声称自己已经访问网页。
四个范围是激光通信采购、激光武器/反无人机采购、光电转塔/吊舱采购和商业航天股权融资。
优先追查具体项目名、采购方、公告编号、招标/中标/变更状态、企业融资轮次和第二独立来源。
排除行业研究报告销售页、股市行情、荐股、泛化券商观点和没有具体主体的趋势评论。
已有信息足够或没有高价值追查方向时停止调用工具。"""


@dataclass(frozen=True)
class ResearchTraceItem:
    round_index: int
    query: str
    category: Category
    intent: str
    result_count: int
    new_candidate_count: int
    budget_remaining: int
    outcome: str


@dataclass(frozen=True)
class AgenticDiscoveryResult:
    candidates: tuple[Candidate, ...]
    trace: tuple[ResearchTraceItem, ...]
    budget: int
    budget_used: int
    search_count: int
    agent_round_count: int
    duplicate_query_count: int
    degraded: bool
    error_reasons: list[str]
    stop_reason: StopReason
    mode: DiscoveryMode = "daily"


class AgenticSearchOrchestrator:
    """Execute seed searches and bounded model-directed follow-up searches."""

    def __init__(
        self,
        *,
        client: Any,
        search_provider: Any,
        fallback_planner: QueryPlanner,
        model: str,
        mode: DiscoveryMode,
        search_budget: int,
        max_agent_rounds: int,
        max_results_per_call: int,
        stop_after_no_new_rounds: int,
    ) -> None:
        hard_limit = 12 if mode == "daily" else 40
        if search_budget < 0 or search_budget > hard_limit:
            raise ValueError(f"{mode} search budget must be between 0 and {hard_limit}")
        if max_agent_rounds < 0:
            raise ValueError("max_agent_rounds must be non-negative")
        if not 1 <= max_results_per_call <= 50:
            raise ValueError("max_results_per_call must be between 1 and 50")
        if stop_after_no_new_rounds < 1:
            raise ValueError("stop_after_no_new_rounds must be positive")
        self._client = client
        self._search_provider = search_provider
        self._fallback_planner = fallback_planner
        self._model = model
        self._mode = mode
        self._search_budget = search_budget
        self._max_agent_rounds = max_agent_rounds
        self._max_results_per_call = max_results_per_call
        self._stop_after_no_new_rounds = stop_after_no_new_rounds
        self._deepseek_tokens = 0

    @property
    def deepseek_tokens(self) -> int:
        return self._deepseek_tokens

    def discover(
        self, now: datetime, projects: Iterable[Project]
    ) -> AgenticDiscoveryResult:
        if now.tzinfo is None:
            raise ValueError("discovery time must include timezone")

        candidates: list[Candidate] = []
        seen_urls: set[str] = set()
        seen_queries: set[str] = set()
        trace: list[ResearchTraceItem] = []
        errors: list[str] = []
        budget_used = 0
        duplicate_query_count = 0

        seed_queries = self._fallback_planner.plan(now, projects)[
            : min(4, self._search_budget)
        ]
        for query in seed_queries:
            scoped = _validated_query(
                query.text,
                query.category,
                "seed",
                kind=query.kind,
            )
            normalized_query = _normalize_query(scoped.text)
            seen_queries.add(normalized_query)
            rows, new_count, outcome = self._search(
                scoped, candidates, seen_urls, errors
            )
            budget_used += 1
            trace.append(
                ResearchTraceItem(
                    round_index=0,
                    query=scoped.text,
                    category=scoped.category,
                    intent="seed",
                    result_count=len(rows),
                    new_candidate_count=new_count,
                    budget_remaining=self._search_budget - budget_used,
                    outcome=outcome,
                )
            )

        if budget_used >= self._search_budget:
            return _result(
                candidates,
                trace,
                self._search_budget,
                budget_used,
                0,
                duplicate_query_count,
                bool(errors),
                errors,
                "budget_exhausted",
                self._mode,
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _research_context(now, self._mode, candidates, projects),
            },
        ]
        no_new_rounds = 0
        rounds_completed = 0

        for round_index in range(1, self._max_agent_rounds + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=[_search_tool_schema()],
                    tool_choice="auto",
                    stream=False,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                self._deepseek_tokens += _response_tokens(response)
                message = response.choices[0].message
            except Exception as error:
                errors.append(type(error).__name__)
                return _result(
                    candidates,
                    trace,
                    self._search_budget,
                    budget_used,
                    rounds_completed,
                    duplicate_query_count,
                    True,
                    errors,
                    "model_error",
                    self._mode,
                )

            rounds_completed += 1
            tool_calls = list(getattr(message, "tool_calls", None) or ())
            messages.append(_assistant_message(message, tool_calls))
            if not tool_calls:
                return _result(
                    candidates,
                    trace,
                    self._search_budget,
                    budget_used,
                    rounds_completed,
                    duplicate_query_count,
                    bool(errors),
                    errors,
                    "model_completed",
                    self._mode,
                )

            round_new_count = 0
            for call in tool_calls:
                if budget_used >= self._search_budget:
                    return _result(
                        candidates,
                        trace,
                        self._search_budget,
                        budget_used,
                        rounds_completed,
                        duplicate_query_count,
                        bool(errors),
                        errors,
                        "budget_exhausted",
                        self._mode,
                    )
                parsed, parse_error = _parse_tool_call(call)
                if parse_error is not None:
                    messages.append(_tool_message(call, {"error": parse_error}))
                    continue
                query, category, intent = parsed
                try:
                    scoped = _validated_query(query, category, intent)
                except ValueError:
                    messages.append(
                        _tool_message(call, {"error": "query_validation_failed"})
                    )
                    continue
                normalized_query = _normalize_query(scoped.text)
                if normalized_query in seen_queries:
                    duplicate_query_count += 1
                    messages.append(_tool_message(call, {"error": "duplicate_query"}))
                    continue
                seen_queries.add(normalized_query)
                rows, new_count, outcome = self._search(
                    scoped, candidates, seen_urls, errors
                )
                budget_used += 1
                round_new_count += new_count
                trace.append(
                    ResearchTraceItem(
                        round_index=round_index,
                        query=scoped.text,
                        category=scoped.category,
                        intent=intent,
                        result_count=len(rows),
                        new_candidate_count=new_count,
                        budget_remaining=self._search_budget - budget_used,
                        outcome=outcome,
                    )
                )
                messages.append(
                    _tool_message(call, _compact_search_results(rows, outcome))
                )

            if budget_used >= self._search_budget:
                return _result(
                    candidates,
                    trace,
                    self._search_budget,
                    budget_used,
                    rounds_completed,
                    duplicate_query_count,
                    bool(errors),
                    errors,
                    "budget_exhausted",
                    self._mode,
                )
            no_new_rounds = no_new_rounds + 1 if round_new_count == 0 else 0
            if no_new_rounds >= self._stop_after_no_new_rounds:
                return _result(
                    candidates,
                    trace,
                    self._search_budget,
                    budget_used,
                    rounds_completed,
                    duplicate_query_count,
                    bool(errors),
                    errors,
                    "no_new_candidates",
                    self._mode,
                )

        return _result(
            candidates,
            trace,
            self._search_budget,
            budget_used,
            rounds_completed,
            duplicate_query_count,
            bool(errors),
            errors,
            "round_limit",
            self._mode,
        )

    def _search(
        self,
        query: SearchQuery,
        candidates: list[Candidate],
        seen_urls: set[str],
        errors: list[str],
    ) -> tuple[list[Candidate], int, str]:
        freshness = "oneMonth" if self._mode == "daily" else "oneYear"
        try:
            rows = list(
                self._search_provider.search(
                    query,
                    freshness=freshness,
                    count=self._max_results_per_call,
                )
            )
        except Exception as error:
            errors.append(type(error).__name__)
            return [], 0, f"error:{type(error).__name__}"
        new_count = 0
        for row in rows:
            normalized_url = normalize_url(row.url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            candidates.append(row.model_copy(update={"url": normalized_url}))
            new_count += 1
        return rows, new_count, "ok"


def _validated_query(
    query: str,
    category: Category | None,
    intent: str,
    *,
    kind: str | None = None,
) -> SearchQuery:
    cleaned = " ".join(str(query).split())
    if not cleaned or len(cleaned) > 300:
        raise ValueError("agent search query must contain 1-300 characters")
    if category is None:
        raise ValueError("agent search query must include a supported category")
    if any(term.casefold() in cleaned.casefold() for term in _QUERY_NOISE_TERMS):
        raise ValueError("agent search query targets a known out-of-scope topic")
    if intent != "seed" and intent not in _ALLOWED_INTENTS:
        raise ValueError("agent search intent is unsupported")
    if _DISCOVERY_SCOPE not in cleaned:
        cleaned = f"{cleaned} {_DISCOVERY_SCOPE}"
    selected_kind = kind or _INTENT_TO_KIND[intent]
    return SearchQuery(kind=selected_kind, text=cleaned, category=category)


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", "", query).casefold()


def _parse_tool_call(
    call: Any,
) -> tuple[tuple[str, Category, str] | None, str | None]:
    function = getattr(call, "function", None)
    if getattr(function, "name", None) != "search_web":
        return None, "unsupported_tool"
    try:
        arguments = json.loads(function.arguments)
        query = arguments["query"]
        category = Category(arguments["category"])
        intent = arguments["intent"]
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, "invalid_arguments"
    if not isinstance(query, str) or not isinstance(intent, str):
        return None, "invalid_arguments"
    if intent not in _ALLOWED_INTENTS:
        return None, "invalid_arguments"
    return (query, category, intent), None


def _search_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search for one concrete Chinese laser or commercial-space event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": [category.value for category in Category],
                    },
                    "intent": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_INTENTS),
                    },
                },
                "required": ["query", "category", "intent"],
                "additionalProperties": False,
            },
        },
    }


def _research_context(
    now: datetime,
    mode: DiscoveryMode,
    candidates: list[Candidate],
    projects: Iterable[Project],
) -> str:
    payload = {
        "task": "根据种子结果提出高价值后续搜索；不要重复已有查询。",
        "now": now.isoformat(),
        "mode": mode,
        "known_projects": [
            {
                "name": item.name,
                "organization": item.organization,
                "category": item.category.value,
            }
            for item in list(projects)[:20]
        ],
        "seed_results": _compact_candidates(candidates),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _compact_candidates(candidates: Iterable[Candidate]) -> list[dict[str, Any]]:
    return [
        {
            "title": item.title[:240],
            "url": item.url,
            "summary": item.summary[:500],
            "category": item.category_hint.value if item.category_hint else None,
            "published_at": (
                item.source_published_at.isoformat()
                if item.source_published_at is not None
                else None
            ),
        }
        for item in candidates
    ]


def _compact_search_results(
    rows: list[Candidate], outcome: str
) -> dict[str, Any]:
    return {"outcome": outcome, "results": _compact_candidates(rows)}


def _assistant_message(message: Any, tool_calls: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": "assistant",
        "content": getattr(message, "content", None),
    }
    if tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in tool_calls
        ]
    return result


def _tool_message(call: Any, content: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": getattr(call, "id", "unknown"),
        "content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
    }


def _result(
    candidates: list[Candidate],
    trace: list[ResearchTraceItem],
    budget: int,
    budget_used: int,
    rounds: int,
    duplicate_query_count: int,
    degraded: bool,
    errors: list[str],
    stop_reason: StopReason,
    mode: DiscoveryMode = "daily",
) -> AgenticDiscoveryResult:
    return AgenticDiscoveryResult(
        candidates=tuple(candidates),
        trace=tuple(trace),
        budget=budget,
        budget_used=budget_used,
        search_count=budget_used,
        agent_round_count=rounds,
        duplicate_query_count=duplicate_query_count,
        degraded=degraded,
        error_reasons=list(dict.fromkeys(errors)),
        stop_reason=stop_reason,
        mode=mode,
    )


def _response_tokens(response: Any) -> int:
    usage = getattr(response, "usage", None)
    value = (
        usage.get("total_tokens")
        if isinstance(usage, dict)
        else getattr(usage, "total_tokens", 0)
    )
    return max(0, int(value)) if isinstance(value, (int, float)) else 0
