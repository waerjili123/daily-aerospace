from __future__ import annotations

from datetime import datetime
from json import dumps, loads
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from laser_space_daily.analyzer import (
    DeepSeekAnalyzer,
    ResilientAnalyzer,
    RuleFallbackAnalyzer,
    UngroundedOutput,
    guard_grounded_output,
)
from laser_space_daily.fetcher import FetchedPage
from laser_space_daily.models import (
    AnalysisResult,
    Category,
    Event,
    EventType,
    Project,
    SourceGrade,
    StateBundle,
    VerificationStatus,
)


NOW = datetime(2026, 7, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
OFFICIAL_URL = "https://official.example.cn/notices/1"


class FakeCompletions:
    def __init__(self) -> None:
        self.replies: list[str | BaseException] = []
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if not isinstance(reply, str):
            return reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
        )


class FakeClient:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def call_count(self) -> int:
        return len(self.completions.calls)

    @property
    def last_call(self) -> dict:
        return self.completions.calls[-1]

    @property
    def last_user_message(self) -> str:
        return self.last_call["messages"][-1]["content"]

    def reply_json(self, value: dict, *, times: int = 1) -> None:
        self.completions.replies.extend([dumps(value, ensure_ascii=False)] * times)

    def reply_text(self, value: str, *, times: int = 1) -> None:
        self.completions.replies.extend([value] * times)

    def reply_error(self, value: BaseException, *, times: int = 1) -> None:
        self.completions.replies.extend([value] * times)

    def reply_response(self, value, *, times: int = 1) -> None:
        self.completions.replies.extend([value] * times)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def official_page() -> FetchedPage:
    return make_page(
        "Laser terminal tender\n"
        "Organization: National Optics Institute\n"
        "Published: 2026-07-21"
    )


@pytest.fixture
def valid_analysis() -> dict:
    path = Path(__file__).parent / "fixtures" / "deepseek_analysis.json"
    return loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def event(official_page: FetchedPage) -> Event:
    return Event(
        event_id="event-1",
        category=Category.LASER_COMMUNICATION,
        title="Laser terminal tender",
        organization="National Optics Institute",
        published_at="2026-07-21T00:00:00+08:00",
        source_url=official_page.final_url,
        source_grade=SourceGrade.A,
        verification_status=VerificationStatus.VERIFIED,
    )


@pytest.fixture
def projects() -> list[Project]:
    return [
        Project(
            project_id="project-1",
            name="Laser terminal acquisition",
            organization="National Optics Institute",
            category=Category.LASER_COMMUNICATION,
            status="tender",
        )
    ]


def make_page(text: str, *, title: str | None = None, url: str = OFFICIAL_URL) -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=200,
        title=title or text.splitlines()[0],
        text=text,
        fetched_at=NOW,
        content_hash="0" * 64,
    )


def test_rule_fallback_extracts_evidence_backed_procurement_deadlines():
    source = make_page(
        "中国境内激光通信终端招标公告\n"
        "采购单位：National Optics Institute\n发布日期：2026-07-21\n"
        "报名截止时间：2026-07-24 17:00\n"
        "投标截止时间：2026-07-25 09:30:00\n"
        "开标时间：2026-07-25",
        title="中国境内激光通信终端招标公告",
    )

    result = RuleFallbackAnalyzer().analyze(source)

    assert result.registration_deadline.isoformat() == "2026-07-24T17:00:00+08:00"
    assert result.bid_submission_deadline.isoformat() == "2026-07-25T09:30:00+08:00"
    assert result.opening_deadline.isoformat() == "2026-07-25T00:00:00+08:00"
    assert result.deadline_precision == {
        "registration": "minute",
        "bid_submission": "second",
        "opening": "date",
    }
    assert {
        item.field for item in result.evidence if item.field.endswith("_deadline")
    } == {
        "registration_deadline",
        "bid_submission_deadline",
        "opening_deadline",
    }


def test_deepseek_returns_schema_valid_analysis(fake_client, official_page, valid_analysis):
    fake_client.reply_json(valid_analysis)

    result = DeepSeekAnalyzer(fake_client, flash_model="deepseek-v4-flash").analyze(
        official_page
    )

    assert result.category == Category.LASER_COMMUNICATION
    assert result.source_url == official_page.final_url


def test_deepseek_accumulates_response_token_usage(
    fake_client, official_page, valid_analysis
):
    fake_client.reply_response(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=dumps(valid_analysis, ensure_ascii=False)
                    )
                )
            ],
            usage=SimpleNamespace(total_tokens=42),
        )
    )
    analyzer = DeepSeekAnalyzer(fake_client)

    analyzer.analyze(official_page)

    assert analyzer.deepseek_tokens == 42


def test_analysis_uses_deterministic_json_request_and_flash_model(
    fake_client, official_page, valid_analysis
):
    fake_client.reply_json(valid_analysis)

    DeepSeekAnalyzer(fake_client, flash_model="deepseek-v4-flash").analyze(official_page)

    assert fake_client.last_call["model"] == "deepseek-v4-flash"
    assert fake_client.last_call["temperature"] == 0
    assert fake_client.last_call["response_format"] == {"type": "json_object"}
    assert list(loads(fake_client.last_user_message)) == [
        "source_url",
        "page_title",
        "body_text",
    ]


@pytest.mark.parametrize(
    ("mutated_field", "value"),
    [
        ("source_url", "https://invented.example/a"),
        ("organization", "Invented Institute"),
        ("published_at", "2025-01-02T00:00:00+08:00"),
        ("amount", "人民币9亿元"),
        ("financing_round", "C轮"),
        ("business_area", "模型臆测的卫星制造领域"),
        ("investors", ["Imaginary Capital"]),
    ],
)
def test_grounding_rejects_hallucinated_fields(
    official_page, valid_analysis, mutated_field, value
):
    bad = AnalysisResult.model_validate({**valid_analysis, mutated_field: value})

    with pytest.raises(UngroundedOutput, match=mutated_field):
        guard_grounded_output(bad, official_page)


def test_grounding_rejects_nonliteral_evidence(official_page, valid_analysis):
    valid_analysis["evidence"][0]["quote"] = "invented quote"

    with pytest.raises(UngroundedOutput, match="evidence"):
        guard_grounded_output(AnalysisResult.model_validate(valid_analysis), official_page)


def test_business_area_requires_field_specific_evidence(official_page, valid_analysis):
    valid_analysis["business_area"] = "Laser terminal"

    with pytest.raises(UngroundedOutput, match="business_area"):
        guard_grounded_output(AnalysisResult.model_validate(valid_analysis), official_page)


def test_grounding_rejects_ungrounded_title(official_page, valid_analysis):
    valid_analysis["title"] = "Invented procurement title"

    with pytest.raises(UngroundedOutput, match="title"):
        guard_grounded_output(AnalysisResult.model_validate(valid_analysis), official_page)


@pytest.mark.parametrize(
    ("field", "value"),
    [("title", "   "), ("organization", "   "), ("investors", ["   "])],
)
def test_grounding_rejects_blank_claim_values(
    official_page, valid_analysis, field, value
):
    valid_analysis[field] = value

    with pytest.raises(UngroundedOutput, match=field):
        guard_grounded_output(AnalysisResult.model_validate(valid_analysis), official_page)


def test_unknown_data_remains_null_or_empty(fake_client, official_page):
    unknown = {
        "in_china": True,
        "in_scope": False,
        "title": "Laser terminal tender",
        "source_url": official_page.final_url,
    }
    fake_client.reply_json(unknown)

    result = DeepSeekAnalyzer(fake_client).analyze(official_page)

    assert result.organization is None
    assert result.published_at is None
    assert result.amount is None
    assert result.investors == []
    assert result.evidence == []


@pytest.mark.parametrize(
    "invalid",
    [
        "not json",
        dumps({
            "in_china": True,
            "in_scope": False,
            "title": "Laser terminal tender",
            "source_url": OFFICIAL_URL,
            "unexpected": "forbidden",
        }),
        dumps({"in_china": "not-a-bool"}),
        dumps({
            "in_china": "true",
            "in_scope": 0,
            "title": "Laser terminal tender",
            "source_url": OFFICIAL_URL,
        }),
    ],
)
def test_analyzer_falls_back_after_two_invalid_responses(
    fake_client, official_page, invalid
):
    fake_client.reply_text(invalid, times=2)

    result = ResilientAnalyzer(
        DeepSeekAnalyzer(fake_client), RuleFallbackAnalyzer()
    ).analyze(official_page)

    assert result.degraded is True
    assert fake_client.call_count == 2


def test_analyzer_falls_back_after_two_timeouts(fake_client, official_page):
    fake_client.reply_error(TimeoutError("model timed out"), times=2)

    result = ResilientAnalyzer(
        DeepSeekAnalyzer(fake_client), RuleFallbackAnalyzer()
    ).analyze(official_page)

    assert result.degraded is True
    assert fake_client.call_count == 2


def test_analyzer_falls_back_after_two_malformed_response_envelopes(
    fake_client, official_page
):
    fake_client.reply_response(SimpleNamespace(choices=None), times=2)

    result = ResilientAnalyzer(
        DeepSeekAnalyzer(fake_client), RuleFallbackAnalyzer()
    ).analyze(official_page)

    assert result.degraded is True
    assert fake_client.call_count == 2


def test_analyzer_rejects_more_than_two_configured_attempts(fake_client):
    with pytest.raises(ValueError, match="at most two"):
        DeepSeekAnalyzer(fake_client, max_attempts=3)


def test_resilient_analyzer_does_not_hide_programmer_type_errors(official_page):
    class BrokenAnalyzer:
        def analyze(self, _page):
            raise TypeError("programmer bug")

    with pytest.raises(TypeError, match="programmer bug"):
        ResilientAnalyzer(BrokenAnalyzer(), RuleFallbackAnalyzer()).analyze(official_page)


def test_pro_model_suggestion_cannot_auto_merge(fake_client, event, projects):
    fake_client.reply_json(
        {"relation": "same_project", "confidence": 0.84, "reason": "标题相似"}
    )

    suggestion = DeepSeekAnalyzer(
        fake_client, pro_model="deepseek-v4-pro"
    ).suggest_match(event, projects)

    assert fake_client.last_call["model"] == "deepseek-v4-pro"
    assert suggestion.requires_human_review is True
    assert projects[0].event_ids == []


def test_invalid_match_suggestion_is_conservative(fake_client, event, projects):
    fake_client.reply_json(
        {
            "relation": "same_project",
            "confidence": 0.99,
            "reason": "猜测",
            "requires_human_review": False,
        }
    )

    suggestion = DeepSeekAnalyzer(fake_client).suggest_match(event, projects)

    assert suggestion.relation == "suspected"
    assert suggestion.confidence == 0
    assert suggestion.requires_human_review is True


def test_financing_scope_excludes_bank_credit(fake_client):
    page = make_page("星河动力获得银行授信10亿元，用于火箭生产。", title="银行授信公告")
    model_claim = {
        "in_china": True,
        "in_scope": True,
        "category": "commercial_space_financing",
        "event_type": "financing",
        "title": "银行授信公告",
        "organization": "星河动力",
        "amount": "10亿元",
        "source_url": page.final_url,
    }
    fake_client.reply_json(model_claim)

    result = DeepSeekAnalyzer(fake_client).analyze(page)

    assert result.in_scope is False


def test_procurement_is_not_excluded_by_incidental_bank_credit_text(fake_client):
    page = make_page("中国境内激光通信终端招标公告，投标人需提供银行授信证明")
    model_claim = {
        "in_china": True,
        "in_scope": True,
        "category": "laser_communication",
        "event_type": "tender",
        "title": page.title,
        "source_url": page.final_url,
    }
    fake_client.reply_json(model_claim)

    model_result = DeepSeekAnalyzer(fake_client).analyze(page)
    fallback_result = RuleFallbackAnalyzer().analyze(page)

    assert model_result.in_scope is True
    assert fallback_result.in_scope is True
    assert fallback_result.category is Category.LASER_COMMUNICATION


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("中国境内激光通信终端采购招标公告", Category.LASER_COMMUNICATION),
        ("中国境内激光武器系统采购招标公告", Category.LASER_WEAPON),
        ("国内项目机载光电吊舱采购招标公告", Category.EO_TURRET),
        ("中国商业航天企业完成A轮融资", Category.COMMERCIAL_SPACE_FINANCING),
        ("国内项目跟瞄转台用于激光通信终端采购招标公告", Category.LASER_COMMUNICATION),
    ],
)
def test_rule_fallback_deterministically_includes_four_categories(text, expected):
    result = RuleFallbackAnalyzer().analyze(make_page(text))

    assert result.in_china is True
    assert result.in_scope is True
    assert result.category is expected
    assert result.degraded is True


@pytest.mark.parametrize(
    "text",
    [
        "通用激光器零部件采购招标公告",
        "采购跟瞄转台；本单位同时开展激光通信技术研究。",
        "商业航天企业获得银行贷款授信",
        "商业航天公司完成融资，同时获得某银行提供10亿元综合授信",
        "商业航天上市公司启动股权再融资",
        "上市公司发布常规再融资公告",
        "跟瞄转台采购公告",
    ],
)
def test_rule_fallback_excludes_generic_parts_and_financing_exclusions(text):
    result = RuleFallbackAnalyzer().analyze(make_page(text))

    assert result.in_scope is False
    assert result.category is None
    assert result.degraded is True


def test_rule_fallback_does_not_treat_chinese_language_as_china_location():
    page = make_page(
        "中国媒体报道：美国国防部发布激光武器系统采购招标公告",
        url="https://media.example.cn/foreign-tender",
    )

    result = RuleFallbackAnalyzer().analyze(page)

    assert result.in_china is False
    assert result.in_scope is False


@pytest.mark.parametrize(
    "foreign_cue",
    [
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
    ],
)
def test_rule_fallback_foreign_event_overrides_approved_domestic_source(foreign_cue):
    page = make_page(
        f"{foreign_cue}发布激光武器系统采购招标公告",
        url="https://notices.example.gov.cn/foreign-tender",
    )

    result = RuleFallbackAnalyzer().analyze(page)

    assert result.in_china is False
    assert result.in_scope is False


def test_trends_are_deterministic_over_verified_rolling_state(fake_client, official_page):
    verified_recent = Event(
        event_id="verified-recent",
        category=Category.LASER_COMMUNICATION,
        title="Verified public event",
        organization="National Optics Institute",
        published_at="2026-07-21T00:00:00+08:00",
        source_url=official_page.final_url,
        source_grade=SourceGrade.A,
        verification_status=VerificationStatus.VERIFIED,
    )
    verified_older = Event(
        event_id="verified-older",
        category=Category.LASER_WEAPON,
        title="Older verified event",
        organization="National Optics Institute",
        published_at="2026-04-23T00:00:00+08:00",
        source_url="https://example.cn/older",
        source_grade=SourceGrade.A,
        verification_status=VerificationStatus.VERIFIED,
    )
    outside_window = Event(
        event_id="outside-window",
        category=Category.EO_TURRET,
        title="Outside verified event",
        organization="National Optics Institute",
        published_at="2026-04-21T00:00:00+08:00",
        source_url="https://example.cn/outside",
        source_grade=SourceGrade.A,
        verification_status=VerificationStatus.VERIFIED,
    )
    unverified = Event(
        event_id="pending",
        category=Category.LASER_WEAPON,
        title="unverified secret body",
        organization="Secret org",
        published_at="2026-07-21T00:00:00+08:00",
        source_url="https://example.cn/pending",
        source_grade=SourceGrade.C,
        verification_status=VerificationStatus.PENDING,
        formal_record=False,
    )
    state = StateBundle(
        events=[verified_recent, verified_older, outside_window, unverified]
    )
    window = (
        datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2026, 7, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = DeepSeekAnalyzer(fake_client).summarize_trends(state, window)

    assert fake_client.call_count == 0
    assert result.event_count == 2
    assert result.category_counts[Category.LASER_COMMUNICATION] == 1
    assert result.category_counts[Category.LASER_WEAPON] == 1
    assert Category.EO_TURRET not in result.category_counts
    assert result.summary == (
        "统计期内共有2条已核验事件：laser_communication 1条、laser_weapon 1条。"
    )
    assert result.degraded is False


def test_empty_trend_is_deterministic_without_model_call(fake_client):
    start = datetime(2026, 7, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    end = datetime(2026, 7, 31, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = DeepSeekAnalyzer(fake_client).summarize_trends(
        StateBundle(), (start, end)
    )

    assert result.event_count == 0
    assert result.category_counts == {}
    assert result.degraded is False
    assert result.summary == "统计期内无已核验事件。"
    assert fake_client.call_count == 0


def test_match_suggestion_model_rejects_auto_merge_at_schema_level():
    from laser_space_daily.analyzer import MatchSuggestion

    with pytest.raises(ValidationError, match="human review"):
        MatchSuggestion(
            relation="same_project",
            confidence=1,
            reason="same",
            requires_human_review=False,
        )
