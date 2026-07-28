from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from laser_space_daily.agentic_discovery import AgenticSearchOrchestrator
from laser_space_daily.discovery import QueryPlanner
from laser_space_daily.models import Candidate, Category


NOW = datetime(2026, 7, 28, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def candidate(url: str, category: Category) -> Candidate:
    return Candidate(
        title=f"{category.value} 具体项目招标公告",
        url=url,
        summary="某单位发布具体项目招标公告，包含项目名称和采购安排。",
        discovered_at=NOW,
        discovery_source="bocha",
        category_hint=category,
        source_published_at=NOW,
    )


def tool_call(
    call_id: str,
    query: str,
    category: Category,
    intent: str = "project_followup",
):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name="search_web",
            arguments=json.dumps(
                {
                    "query": query,
                    "category": category.value,
                    "intent": intent,
                },
                ensure_ascii=False,
            ),
        ),
    )


def response(*calls, total_tokens=0):
    return SimpleNamespace(
        usage=SimpleNamespace(total_tokens=total_tokens),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=list(calls),
                )
            )
        ]
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        next_response = self.responses.pop(0)
        if isinstance(next_response, BaseException):
            raise next_response
        return next_response


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class FakeSearchProvider:
    def __init__(self):
        self.calls = []

    def search(self, query, *, freshness="oneMonth", count=10):
        self.calls.append((query, freshness, count))
        return [candidate(f"https://search.example/{len(self.calls)}", query.category)]


def orchestrator(responses, *, budget=12, stop_after_no_new_rounds=2):
    provider = FakeSearchProvider()
    subject = AgenticSearchOrchestrator(
        client=FakeClient(responses),
        search_provider=provider,
        fallback_planner=QueryPlanner(max_queries=4),
        model="deepseek-v4-pro",
        mode="daily",
        search_budget=budget,
        max_agent_rounds=4,
        max_results_per_call=10,
        stop_after_no_new_rounds=stop_after_no_new_rounds,
    )
    return subject, provider


def test_executes_four_seed_queries_before_agent_followups():
    subject, provider = orchestrator(
        [
            response(
                tool_call(
                    "call-1",
                    "某激光通信项目 采购方 公告编号",
                    Category.LASER_COMMUNICATION,
                )
            ),
            response(),
        ],
        budget=5,
    )

    result = subject.discover(NOW, [])

    assert result.search_count == 5
    assert len(provider.calls) == 5
    assert [call[0].category for call in provider.calls[:4]] == [
        Category.LASER_COMMUNICATION,
        Category.LASER_WEAPON,
        Category.EO_TURRET,
        Category.COMMERCIAL_SPACE_FINANCING,
    ]
    assert result.budget == result.budget_used == 5
    assert result.stop_reason == "budget_exhausted"
    assert all(call[2] == 10 for call in provider.calls)


def test_duplicate_agent_query_is_rejected_without_spending_budget():
    duplicate = "某激光通信项目 采购方 公告编号"
    subject, provider = orchestrator(
        [
            response(
                tool_call("call-1", duplicate, Category.LASER_COMMUNICATION),
                tool_call("call-2", duplicate, Category.LASER_COMMUNICATION),
            ),
            response(),
        ],
        budget=8,
    )

    result = subject.discover(NOW, [])

    assert len(provider.calls) == 5
    assert result.budget_used == 5
    assert result.duplicate_query_count == 1
    assert result.stop_reason == "model_completed"


def test_model_failure_keeps_seed_results_and_marks_agent_degraded():
    subject, provider = orchestrator([RuntimeError("model unavailable")])

    result = subject.discover(NOW, [])

    assert len(provider.calls) == 4
    assert len(result.candidates) == 4
    assert result.degraded is True
    assert result.stop_reason == "model_error"
    assert result.error_reasons == ["RuntimeError"]


def test_query_scope_and_backfill_freshness_are_enforced_locally():
    provider = FakeSearchProvider()
    subject = AgenticSearchOrchestrator(
        client=FakeClient(
            [
                response(
                    tool_call(
                        "call-1",
                        "某商业航天企业 B轮",
                        Category.COMMERCIAL_SPACE_FINANCING,
                        "corroboration",
                    )
                ),
                response(),
            ]
        ),
        search_provider=provider,
        fallback_planner=QueryPlanner(max_queries=4),
        model="deepseek-v4-pro",
        mode="backfill",
        search_budget=5,
        max_agent_rounds=4,
        max_results_per_call=7,
        stop_after_no_new_rounds=2,
    )

    result = subject.discover(NOW, [])

    assert all("中国 境内" in call[0].text for call in provider.calls)
    assert all(call[1] == "oneYear" for call in provider.calls)
    assert all(call[2] == 7 for call in provider.calls)
    assert result.mode == "backfill"


def test_multiple_tool_calls_cannot_exceed_remaining_budget():
    subject, provider = orchestrator(
        [
            response(
                tool_call("call-1", "项目一 中标", Category.LASER_COMMUNICATION),
                tool_call("call-2", "项目二 中标", Category.LASER_COMMUNICATION),
                tool_call("call-3", "项目三 中标", Category.LASER_COMMUNICATION),
            )
        ],
        budget=6,
    )

    result = subject.discover(NOW, [])

    assert len(provider.calls) == 6
    assert result.budget_used == 6
    assert result.stop_reason == "budget_exhausted"


def test_invalid_long_query_does_not_crash_or_spend_budget():
    subject, provider = orchestrator(
        [
            response(
                tool_call(
                    "call-1",
                    "超长" * 200,
                    Category.LASER_COMMUNICATION,
                )
            ),
            response(total_tokens=17),
        ],
        budget=8,
    )

    result = subject.discover(NOW, [])

    assert len(provider.calls) == 4
    assert result.budget_used == 4
    assert result.stop_reason == "model_completed"
    assert subject.deepseek_tokens == 17
