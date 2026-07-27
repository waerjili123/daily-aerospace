from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from laser_space_daily.discovery import select_search_candidates
from laser_space_daily.fetcher import FetchedPage
from laser_space_daily.matching import ProjectMatcher
from laser_space_daily.models import (
    AnalysisResult,
    Candidate,
    Category,
    StateBundle,
    TrendSummary,
    VerificationStatus,
)
from laser_space_daily.pipeline import Pipeline
from laser_space_daily.report import ReportRenderer
from laser_space_daily.repository import StateRepository
from laser_space_daily.verifier import RuleVerifier, SourceRegistry


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 22, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
STATE_FILES = (
    "state.json",
    "events.jsonl",
    "projects.json",
    "financings.json",
    "pending.json",
)
EXPECTED_RECORD_FIELDS = {
    "id",
    "category",
    "event_type",
    "buyer_or_company",
    "project_code",
    "project_chain",
    "relation",
    "verification_result",
    "verification_reason",
    "source_grade",
    "literal_evidence",
}


def _load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_records() -> list[dict]:
    records = [*_load_json("lifecycle_cases.json"), *_load_json("financing_cases.json")]
    for record in records:
        analysis = record["analysis"]
        if not analysis["in_scope"]:
            continue
        url = record["candidate"]["url"]
        if analysis["category"] == "commercial_space_financing":
            prefix = "中国商业航天企业。"
            record["page"]["text"] = prefix + record["page"]["text"]
            country_quote = "中国商业航天企业"
            scope_quote = record["page"]["text"]
            analysis["amount_disclosed"] = analysis["amount"] is not None
            amount_quote = analysis["amount"] or "金额未披露"
            analysis["evidence"].append(
                {"field": "amount", "quote": amount_quote, "source_url": url}
            )
            analysis["evidence"].extend(
                {"field": "investors", "quote": investor, "source_url": url}
                for investor in analysis["investors"]
            )
        else:
            country_quote = url
            scope_quote = record["page"]["title"]
        analysis["evidence"].extend(
            (
                {"field": "in_china", "quote": country_quote, "source_url": url},
                {"field": "in_scope", "quote": scope_quote, "source_url": url},
                {
                    "field": "category",
                    "quote": scope_quote,
                    "source_url": url,
                },
                {
                    "field": "event_type",
                    "quote": (
                        "融资"
                        if analysis["category"] == "commercial_space_financing"
                        else record["page"]["title"]
                    ),
                    "source_url": url,
                },
            )
        )
    return records


def test_information_availability_fixture_filters_noise_old_and_duplicate_rows() -> None:
    fixture = _load_json("information_availability_cases.json")
    now = datetime.fromisoformat(fixture["now"])
    rows = [
        Candidate.model_validate(
            {
                **record,
                "discovered_at": now,
                "discovery_source": "bocha",
            }
        )
        for record in fixture["candidates"]
    ]

    selection = select_search_candidates(rows, now)

    assert [item.url for item in selection.candidates] == fixture["expected_urls"]
    assert selection.raw_search_count == 21
    assert selection.valid_shape_count == 21
    assert selection.relevance_pass_count == 16
    assert selection.recent_7d_count == 6
    assert selection.fallback_8_30d_count == 0
    assert selection.unknown_date_count == 0


class FixturePlanner:
    def plan(self, _now, _projects):
        return []


class OfflineSearchProvider:
    usage_count = 0

    def search(self, _query):
        raise AssertionError("offline acceptance must not call external search")


class FixtureCollector:
    failed_domains: frozenset[str] = frozenset()

    def __init__(self, records: list[dict]) -> None:
        self._rows = [
            Candidate.model_validate({**record["candidate"], "discovered_at": NOW})
            for record in records
        ]

    def collect(self) -> list[Candidate]:
        return list(self._rows)


class FixtureFetcher:
    def __init__(self, records: list[dict]) -> None:
        self._pages = {
            record["candidate"]["url"]: FetchedPage(
                requested_url=record["candidate"]["url"],
                final_url=record["candidate"]["url"],
                fetched_at=NOW,
                content_hash=hashlib.sha256(
                    record["page"]["text"].encode("utf-8")
                ).hexdigest(),
                **record["page"],
            )
            for record in records
        }

    def fetch(self, item: Candidate) -> FetchedPage:
        return self._pages[item.url]


class FixtureAnalyzer:
    deepseek_tokens = 0

    def __init__(self, records: list[dict]) -> None:
        self._analyses = {
            record["candidate"]["url"]: AnalysisResult.model_validate(
                record["analysis"]
            )
            for record in records
        }

    def analyze(self, page: FetchedPage) -> AnalysisResult:
        return self._analyses[page.final_url]


class RecordingVerifier:
    def __init__(self) -> None:
        registry = SourceRegistry(
            {
                "www.procurement.gov.example": "A",
            },
            financing_company_domains={
                "funding.space-company.example": "天穹火箭科技有限公司"
            },
            financing_b_domains=(
                "finance.news-one.example",
                "industry.news-two.example",
                "only.news-three.example",
            ),
        )
        self._verifier = RuleVerifier(registry)
        self.decisions = {}

    def verify(self, analysis, page, corroborating=()):
        decision = self._verifier.verify(analysis, page, corroborating)
        self.decisions[page.final_url] = decision
        return decision


class RecordingMatcher:
    def __init__(self) -> None:
        self._matcher = ProjectMatcher()
        self.relations: dict[str, str] = {}

    def match(self, event, projects):
        decision = self._matcher.match(event, projects)
        self.relations[event.source_url] = decision.relation
        return decision


class FixtureTrendSummarizer:
    deepseek_tokens = 0

    def summarize_trends(self, state: StateBundle, window) -> TrendSummary:
        counts = Counter(event.category for event in state.events)
        counts[Category.COMMERCIAL_SPACE_FINANCING] += len(state.financings)
        return TrendSummary(
            window_start=window[0],
            window_end=window[1],
            summary="离线固定样本趋势",
            event_count=len(state.events) + len(state.financings),
            category_counts=dict(counts),
        )


class FixtureLogger:
    def warning(self, _message: str) -> None:
        pass


def _make_pipeline(root: Path, records: list[dict]):
    verifier = RecordingVerifier()
    matcher = RecordingMatcher()
    pipeline = Pipeline(
        repository=StateRepository(root),
        planner=FixturePlanner(),
        search_provider=OfflineSearchProvider(),
        official_collector=FixtureCollector(records),
        fetcher=FixtureFetcher(records),
        analyzer=FixtureAnalyzer(records),
        verifier=verifier,
        matcher=matcher,
        trend_summarizer=FixtureTrendSummarizer(),
        logger=FixtureLogger(),
    )
    return pipeline, verifier, matcher


def _project_with_code(state: StateBundle, code: str):
    return next(project for project in state.projects if code in project.project_codes)


def _section(markdown: str, heading: str, next_heading: str) -> str:
    return markdown.split(f"## {heading}\n", 1)[1].split(
        f"## {next_heading}\n", 1
    )[0]


def test_sanitized_fixture_corpus_runs_real_components_end_to_end(tmp_path) -> None:
    records = _fixture_records()
    expected = _load_json("expected.json")
    expected_by_id = {record["id"]: record for record in expected["records"]}
    record_by_id = {record["id"]: record for record in records}
    url_to_id = {
        record["candidate"]["url"]: record["id"] for record in records
    }

    assert len(records) == expected["summary"]["candidate_count"]
    assert set(record_by_id) == set(expected_by_id)
    for record_id, fixture in record_by_id.items():
        declaration = expected_by_id[record_id]
        analysis = fixture["analysis"]
        assert set(declaration) == EXPECTED_RECORD_FIELDS
        assert analysis["category"] == declaration["category"]
        assert analysis["event_type"] == declaration["event_type"]
        assert analysis["organization"] == declaration["buyer_or_company"]
        assert analysis["project_codes"] == (
            [declaration["project_code"]]
            if declaration["project_code"] is not None
            else []
        )
        assert declaration["literal_evidence"] in fixture["page"]["text"]
        assert declaration["literal_evidence"] in {
            evidence["quote"] for evidence in analysis["evidence"]
        }
        assert fixture["candidate"]["url"].startswith("https://")

    pipeline, verifier, matcher = _make_pipeline(tmp_path, records)
    first = pipeline.run(NOW)
    first_decisions = dict(verifier.decisions)

    status_counts = Counter(decision.status for decision in first_decisions.values())
    summary = expected["summary"]
    assert first.metrics.candidate_count == summary["candidate_count"]
    assert first.metrics.verified_count == summary["verified_decision_count"], {
        url_to_id[url]: decision.reason
        for url, decision in first_decisions.items()
        if decision.status is not VerificationStatus.VERIFIED
    }
    assert status_counts[VerificationStatus.PENDING] == summary["pending_count"]
    assert status_counts[VerificationStatus.REJECTED] == summary["rejected_count"]
    assert len(first.state.events) == summary["event_count"]
    assert len(first.state.projects) == summary["project_count"]
    assert len(first.state.financings) == summary["financing_count"]
    assert len(first.state.events) + len(first.state.financings) == summary[
        "formal_state_count"
    ]
    assert len(first.state.pending) == summary["pending_count"]

    for url, decision in first_decisions.items():
        declaration = expected_by_id[url_to_id[url]]
        assert decision.status.value == declaration["verification_result"]
        assert decision.reason == declaration["verification_reason"]
        assert decision.source_grade.value == declaration["source_grade"]
    event_by_id = {event.event_id: event for event in first.state.events}
    event_by_url = {event.source_url: event for event in first.state.events}
    project_id_by_event_id = {
        event_id: project.project_id
        for project in first.state.projects
        for event_id in project.event_ids
    }
    financing_by_url = {
        source_url: financing
        for financing in first.state.financings
        for source_url in {financing.source_url, *financing.source_urls}
    }
    pending_by_url = {item.source_url: item for item in first.state.pending}
    chain_state_ids: dict[str, set[str]] = {}
    consumed_chains: set[str] = set()
    for record_id, declaration in expected_by_id.items():
        url = record_by_id[record_id]["candidate"]["url"]
        chain = declaration["project_chain"]
        consumed_chains.add(chain)
        if declaration["category"] != "commercial_space_financing":
            event = event_by_url[url]
            project_id = project_id_by_event_id[event.event_id]
            assert matcher.relations[url] == declaration["relation"]
            chain_state_ids.setdefault(chain, set()).add(f"project:{project_id}")
        elif declaration["verification_result"] == "verified":
            financing = financing_by_url[url]
            if declaration["relation"] == "new_financing":
                assert financing.source_url == url
            else:
                assert declaration["relation"] == "same_financing"
                assert financing.source_url != url
            chain_state_ids.setdefault(chain, set()).add(
                f"financing:{financing.financing_id}"
            )
        elif declaration["verification_result"] == "pending":
            assert declaration["relation"] == "pending"
            chain_state_ids.setdefault(chain, set()).add(
                f"pending:{pending_by_url[url].item_id}"
            )
        else:
            assert declaration["verification_result"] == "rejected"
            assert declaration["relation"] == "rejected"
            assert url not in event_by_url | financing_by_url | pending_by_url
            chain_state_ids.setdefault(chain, set()).add(f"rejected:{url}")
    assert consumed_chains == {
        declaration["project_chain"] for declaration in expected_by_id.values()
    }
    assert all(len(state_ids) == 1 for state_ids in chain_state_ids.values())
    assert len({next(iter(state_ids)) for state_ids in chain_state_ids.values()}) == len(
        chain_state_ids
    )

    communication = _project_with_code(first.state, "XW-LC-2026-001")
    weapon = _project_with_code(first.state, "LW-2026-009")
    assert [event_by_id[item].event_type.value for item in communication.event_ids] == expected[
        "project_event_order"
    ]["laser-comm"]
    assert communication.status == expected["latest_status"]["laser-comm"]
    assert [event_by_id[item].event_type.value for item in weapon.event_ids] == expected[
        "project_event_order"
    ]["laser-weapon-rebid"]
    assert weapon.status == expected["latest_status"]["laser-weapon-rebid"]
    assert weapon.project_codes == ["LW-2026-009", "LW-2026-009-R2"]
    lot_one = _project_with_code(first.state, "EO-POD-2026-L1")
    lot_two = _project_with_code(first.state, "EO-POD-2026-L2")
    assert lot_one.project_id != lot_two.project_id

    two_b = next(item for item in first.state.financings if item.company == "星舟卫星有限公司")
    assert two_b.source_urls == sorted(
        [
            record_by_id["financing-two-b-primary"]["candidate"]["url"],
            record_by_id["financing-two-b-corroborating"]["candidate"]["url"],
        ]
    )
    assert {item.company for item in first.state.financings} == {
        "天穹火箭科技有限公司",
        "星舟卫星有限公司",
    }
    assert first.state.pending[0].source_url == record_by_id[
        "financing-one-b-pending"
    ]["candidate"]["url"]

    markdown = ReportRenderer(max_chars=18000).render(first).markdown
    headings = [
        "过去24小时新增/变化",
        "当前可报名及即将启动",
        "激光通信",
        "激光武器/反无人机",
        "光电转塔/吊舱",
        "商业航天融资",
        "今日重点跟进",
        "三个月趋势与数据完整性",
    ]
    assert [markdown.count(f"## {heading}") for heading in headings] == [1] * 8
    assert [markdown.index(f"## {heading}") for heading in headings] == sorted(
        markdown.index(f"## {heading}") for heading in headings
    )
    assert "北京时间 2026-07-21 09:30—2026-07-22 09:30" in markdown
    assert "滚动池 2026-04-22—2026-07-22" in markdown
    first_daily = _section(
        markdown, "过去24小时新增/变化", "当前可报名及即将启动"
    )
    formal_ids = {
        record["id"]
        for record in expected["records"]
        if record["verification_result"] == "verified"
    }
    expected_changed_urls = {
        record_by_id[record_id]["candidate"]["url"] for record_id in formal_ids
    }
    assert set(re.findall(r"\]\((https://[^)]+)\)", first_daily)) == expected_changed_urls
    for record_id in formal_ids:
        assert f"]({record_by_id[record_id]['candidate']['url']})" in markdown
    assert "2026-04-21" in first_daily
    assert "机载光电转塔伺服稳像核心组件招标公告" in first_daily
    followup = _section(markdown, "今日重点跟进", "三个月趋势与数据完整性")
    assert record_by_id["financing-one-b-pending"]["candidate"]["url"] in followup
    assert record_by_id["financing-one-b-pending"]["candidate"]["url"] not in first_daily
    assert record_by_id["bank-credit-rejected"]["candidate"]["url"] not in markdown
    eo_rolling = _section(markdown, "光电转塔/吊舱", "商业航天融资")
    assert record_by_id["eo-turret-core"]["candidate"]["url"] not in eo_rolling
    assert record_by_id["same-name-lot-1"]["candidate"]["url"] in eo_rolling
    assert not re.search(r"</?[a-z][^>]*>", markdown, flags=re.IGNORECASE)
    assert not re.search(r"^\s*\|", markdown, flags=re.MULTILINE)
    assert all(
        forbidden not in markdown
        for forbidden in ("AI日报", "人工智能日报", "AI新闻")
    )

    bytes_before = {
        name: (tmp_path / name).read_bytes() for name in STATE_FILES
    }
    financing_json_before = json.loads(bytes_before["financings.json"])
    first_ids = {
        "events": [item.event_id for item in first.state.events],
        "projects": [item.project_id for item in first.state.projects],
        "financings": [item.financing_id for item in first.state.financings],
        "pending": [item.item_id for item in first.state.pending],
    }
    second = pipeline.run(NOW)
    second_ids = {
        "events": [item.event_id for item in second.state.events],
        "projects": [item.project_id for item in second.state.projects],
        "financings": [item.financing_id for item in second.state.financings],
        "pending": [item.item_id for item in second.state.pending],
    }
    assert second_ids == first_ids
    assert json.loads((tmp_path / "financings.json").read_bytes()) == financing_json_before
    assert {name: (tmp_path / name).read_bytes() for name in STATE_FILES} == bytes_before
    assert second.changed_event_ids == first.changed_event_ids
    assert second.changed_project_ids == first.changed_project_ids
    assert second.changed_financing_ids == first.changed_financing_ids
    second_daily = _section(
        ReportRenderer(max_chars=18000).render(second).markdown,
        "过去24小时新增/变化",
        "当前可报名及即将启动",
    )
    assert set(re.findall(r"\]\((https://[^)]+)\)", second_daily)) == expected_changed_urls
