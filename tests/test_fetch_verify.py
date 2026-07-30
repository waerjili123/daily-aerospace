from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from laser_space_daily.analyzer import ResilientAnalyzer, RuleFallbackAnalyzer
from laser_space_daily.fetcher import (
    FetchRedirectLimit,
    FetchedPage,
    PageFetcher,
    PageTooLarge,
    UnsafeUrl,
)
from laser_space_daily.models import (
    AnalysisResult,
    Candidate,
    Category,
    EventType,
    Evidence,
    SourceGrade,
    VerificationStatus,
)
from laser_space_daily.verifier import (
    RuleVerifier,
    SourceRegistry,
    financing_evidence_gaps,
)


NOW = datetime(2026, 7, 22, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
PUBLIC_URL = "https://official.example.cn/notices/1"
PUBLIC_CANDIDATE = Candidate(
    title="Laser terminal tender",
    url=PUBLIC_URL,
    discovered_at=NOW,
    discovery_source="test",
)
REGISTRY = SourceRegistry(
    {
        "official.example.cn": SourceGrade.A,
        "media.example.cn": SourceGrade.B,
        "other.example.cn": SourceGrade.B,
    },
    financing_company_domains={"official.example.cn": "Orbit Corp"},
    financing_b_domains=("media.example.cn", "other.example.cn"),
)


def public_resolver(_hostname: str):
    return ["93.184.216.34"]


def page(url: str, text: str, *, status_code: int = 200) -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=status_code,
        title=text.splitlines()[0],
        text=text,
        fetched_at=NOW,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def classification_evidence(
    url: str,
    scope_quote: str,
    *,
    country_quote: str | None = None,
    category_quote: str,
    event_quote: str,
) -> list[Evidence]:
    return [
        Evidence(field="in_china", quote=country_quote or url, source_url=url),
        Evidence(field="in_scope", quote=scope_quote, source_url=url),
        Evidence(field="category", quote=category_quote, source_url=url),
        Evidence(field="event_type", quote=event_quote, source_url=url),
    ]


@pytest.fixture
def official_page() -> FetchedPage:
    text = (Path(__file__).parent / "fixtures" / "tender_notice.html").read_text(
        encoding="utf-8"
    )
    return page(PUBLIC_URL, text)


@pytest.fixture
def media_page() -> FetchedPage:
    return page(
        "https://media.example.cn/report/1",
        "中国商业航天企业 Orbit Corp Series B financing 2026-07-21，金额1亿元，Capital One投资。",
    )


@pytest.fixture
def second_media_page() -> FetchedPage:
    return page(
        "https://other.example.cn/story/9",
        "中国商业航天企业 Orbit Corp 于 2026-07-21 confirmed its Series B financing，"
        "金额1亿元，Capital One投资。",
    )


@pytest.fixture
def analysis(official_page: FetchedPage) -> AnalysisResult:
    return AnalysisResult(
        in_china=True,
        in_scope=True,
        category=Category.LASER_COMMUNICATION,
        event_type=EventType.TENDER,
        title="Laser terminal tender",
        organization="National Optics Institute",
        published_at="2026-07-21T00:00:00+08:00",
        evidence=[
            Evidence(field="title", quote="Laser terminal tender", source_url=official_page.final_url),
            Evidence(
                field="organization",
                quote="National Optics Institute",
                source_url=official_page.final_url,
            ),
            Evidence(field="published_at", quote="2026-07-21", source_url=official_page.final_url),
            *classification_evidence(
                official_page.final_url,
                "Laser terminal tender",
                category_quote="Laser terminal",
                event_quote="tender",
            ),
        ],
        source_url=official_page.final_url,
    )


@pytest.fixture
def financing_analysis(media_page: FetchedPage) -> AnalysisResult:
    return AnalysisResult(
        in_china=True,
        in_scope=True,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        title="Orbit Corp closes Series B",
        organization="Orbit Corp",
        published_at="2026-07-21T00:00:00+08:00",
        financing_round="Series B",
        amount="1亿元",
        investors=["Capital One"],
        evidence=[
            Evidence(field="organization", quote="Orbit Corp", source_url=media_page.final_url),
            Evidence(field="financing_round", quote="Series B", source_url=media_page.final_url),
            Evidence(field="published_at", quote="2026-07-21", source_url=media_page.final_url),
            Evidence(field="amount", quote="1亿元", source_url=media_page.final_url),
            Evidence(field="investors", quote="Capital One", source_url=media_page.final_url),
            *classification_evidence(
                media_page.final_url,
                "中国商业航天企业 Orbit Corp Series B financing 2026-07-21",
                country_quote="中国商业航天企业",
                category_quote="商业航天",
                event_quote="financing",
            ),
        ],
        source_url=media_page.final_url,
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/a",
        "http://169.254.169.254/latest",
        "http://10.0.0.1/a",
        "http://[::1]/a",
        "http://0.0.0.0/a",
        "http://224.0.0.1/a",
        "http://192.0.2.1/a",
    ],
)
def test_fetcher_blocks_non_public_urls(url):
    with pytest.raises(UnsafeUrl):
        PageFetcher().fetch(
            Candidate(title="x", url=url, discovered_at=NOW, discovery_source="test")
        )


def test_fetcher_revalidates_redirect_target():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            302, headers={"Location": "http://127.0.0.1/private"}
        )
    )

    with pytest.raises(UnsafeUrl):
        PageFetcher(transport=transport, resolver=public_resolver).fetch(PUBLIC_CANDIDATE)


def test_fetcher_connects_to_validated_ip_while_preserving_host_and_sni():
    captured: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="<title>Safe</title><main>Safe body</main>")

    PageFetcher(
        transport=httpx.MockTransport(record), resolver=public_resolver
    ).fetch(PUBLIC_CANDIDATE)

    assert captured[0].url.host == "93.184.216.34"
    assert captured[0].headers["host"] == "official.example.cn"
    assert captured[0].headers["connection"] == "close"
    assert captured[0].extensions["sni_hostname"] == "official.example.cn"


def test_fetcher_rejects_hostname_if_any_resolved_address_is_not_public():
    def mixed_resolver(_hostname: str):
        return ["93.184.216.34", "10.0.0.2"]

    with pytest.raises(UnsafeUrl):
        PageFetcher(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
            resolver=mixed_resolver,
        ).fetch(PUBLIC_CANDIDATE)


def test_fetcher_enforces_ten_mibibyte_body_limit():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"x" * 33)
    )

    with pytest.raises(PageTooLarge):
        PageFetcher(
            transport=transport, resolver=public_resolver, max_bytes=32
        ).fetch(PUBLIC_CANDIDATE)


def test_fetcher_enforces_body_limit_without_content_length():
    class ChunkedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 16
            yield b"y" * 17

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=ChunkedStream())
    )

    with pytest.raises(PageTooLarge):
        PageFetcher(
            transport=transport, resolver=public_resolver, max_bytes=32
        ).fetch(PUBLIC_CANDIDATE)


def test_fetcher_stops_after_five_redirects():
    def redirect(request: httpx.Request) -> httpx.Response:
        index = int(request.url.path.rsplit("/", 1)[-1]) if request.url.path != "/notices/1" else 0
        return httpx.Response(302, headers={"Location": f"/redirect/{index + 1}"})

    with pytest.raises(FetchRedirectLimit):
        PageFetcher(
            transport=httpx.MockTransport(redirect),
            resolver=public_resolver,
            max_redirects=5,
        ).fetch(PUBLIC_CANDIDATE)


def test_fetcher_falls_back_to_beautifulsoup_when_trafilatura_is_empty(monkeypatch):
    html = (Path(__file__).parent / "fixtures" / "tender_notice.html").read_text(
        encoding="utf-8"
    )
    monkeypatch.setattr("laser_space_daily.fetcher.trafilatura.extract", lambda _html: None)
    fetched = PageFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=html)),
        resolver=public_resolver,
    ).fetch(PUBLIC_CANDIDATE)

    assert fetched.title == "Laser terminal tender"
    assert "National Optics Institute" in fetched.text
    assert "Navigation that should not be extracted" not in fetched.text
    assert "ignore me" not in fetched.text
    assert fetched.fetched_at.tzinfo == ZoneInfo("Asia/Shanghai")
    assert len(fetched.content_hash) == 64


def test_fetcher_falls_back_to_beautifulsoup_when_trafilatura_raises(monkeypatch):
    html = "<html><head><title>Fallback</title></head><body><main>Body text</main></body></html>"

    def fail_extract(_html: str):
        raise RuntimeError("extractor failed")

    monkeypatch.setattr("laser_space_daily.fetcher.trafilatura.extract", fail_extract)
    fetched = PageFetcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=html)),
        resolver=public_resolver,
    ).fetch(PUBLIC_CANDIDATE)

    assert "Body text" in fetched.text


@pytest.mark.parametrize(
    "metadata",
    [
        '<meta property="article:published_time" content="2026-07-21T14:02:00+08:00">',
        '<meta itemprop="datePublished" content="2026-07-21">',
        (
            '<script type="application/ld+json">'
            '{"@type":"NewsArticle","datePublished":"2026-07-21T14:02:00+08:00"}'
            "</script>"
        ),
    ],
)
def test_fetcher_preserves_page_published_metadata_for_grounding(metadata):
    html = (
        f"<html><head><title>融资新闻</title>{metadata}</head>"
        "<body><main>光邮星空完成Pre-A轮融资。</main></body></html>"
    )

    fetched = PageFetcher(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=html)
        ),
        resolver=public_resolver,
    ).fetch(PUBLIC_CANDIDATE)

    assert "光邮星空完成Pre-A轮融资" in fetched.text
    assert "页面发布时间：2026-07-21" in fetched.text


def test_fetcher_does_not_promote_modified_or_invalid_metadata_as_publication_date():
    html = (
        "<html><head><title>融资新闻</title>"
        '<meta property="article:modified_time" content="2026-07-22">'
        '<meta property="article:published_time" content="not-a-date">'
        "</head><body><main>正文无发布日期。</main></body></html>"
    )

    fetched = PageFetcher(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=html)
        ),
        resolver=public_resolver,
    ).fetch(PUBLIC_CANDIDATE)

    assert "页面发布时间" not in fetched.text
    assert "2026-07-22" not in fetched.text


def test_source_registry_uses_longest_registered_domain():
    registry = SourceRegistry(
        {"example.cn": SourceGrade.B, "official.example.cn": SourceGrade.A}
    )

    assert registry.grade("https://news.official.example.cn/a") is SourceGrade.A
    assert registry.grade("https://unregistered.gov.cn/a") is SourceGrade.C


def test_tender_requires_grade_a_and_field_evidence(official_page, analysis):
    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)
    assert decision.status == VerificationStatus.VERIFIED, decision.reason
    assert {e.field for e in decision.evidence} >= {
        "title",
        "organization",
        "published_at",
    }


def test_tender_rejects_whitespace_evidence_quote(official_page, analysis):
    analysis.evidence[0].quote = "   "

    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "evidence_not_grounded"


def test_tender_rejects_unrelated_literal_under_required_field_label(
    official_page, analysis
):
    unrelated = "This notice is available for public review"
    official_page.text += unrelated
    analysis.evidence[0].quote = unrelated

    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "evidence_not_grounded"


@pytest.mark.parametrize(
    "rendered_date",
    ["2026-07-21", "2026/07/21", "2026年7月21日", "2026年07月21日"],
)
def test_tender_accepts_supported_date_renderings(official_page, analysis, rendered_date):
    quote = f"Published on {rendered_date}"
    official_page.text += quote
    published = next(item for item in analysis.evidence if item.field == "published_at")
    published.quote = quote

    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.status == VerificationStatus.VERIFIED


def test_tender_matches_title_and_organization_ignoring_punctuation_case_and_spacing(
    official_page, analysis
):
    title_quote = "Notice: LASER-terminal   TENDER"
    organization_quote = "Issued by NATIONAL, OPTICS-INSTITUTE"
    official_page.text += title_quote + organization_quote
    next(item for item in analysis.evidence if item.field == "title").quote = title_quote
    next(
        item for item in analysis.evidence if item.field == "organization"
    ).quote = organization_quote

    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.status == VerificationStatus.VERIFIED


def test_grade_b_tender_stays_pending(media_page, analysis):
    tender_page = page(
        media_page.final_url,
        "中国境内激光通信终端 tender\nLaser terminal tender\n"
        "National Optics Institute\nPublished: 2026-07-21",
    )
    analysis.source_url = tender_page.final_url
    analysis.evidence = [
        Evidence(field="title", quote="Laser terminal tender", source_url=tender_page.final_url),
        Evidence(field="organization", quote="National Optics Institute", source_url=tender_page.final_url),
        Evidence(field="published_at", quote="2026-07-21", source_url=tender_page.final_url),
        *classification_evidence(
            tender_page.final_url,
            "中国境内激光通信终端 tender",
            country_quote="中国境内",
            category_quote="激光通信终端",
            event_quote="tender",
        ),
    ]
    decision = RuleVerifier(REGISTRY).verify(analysis, tender_page)
    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "tender_requires_grade_a"


def test_single_b_source_financing_stays_pending(media_page, financing_analysis):
    decision = RuleVerifier(REGISTRY).verify(financing_analysis, media_page)
    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "financing_requires_official_or_two_independent_b_sources"


def test_same_page_split_category_and_event_evidence_passes_classification(
    media_page, financing_analysis
):
    non_classification = [
        item
        for item in financing_analysis.evidence
        if item.field not in {"in_china", "in_scope", "category", "event_type"}
    ]
    financing_analysis.evidence = [
        *non_classification,
        *classification_evidence(
            media_page.final_url,
            "商业航天",
            country_quote="中国商业航天企业",
            category_quote="商业航天",
            event_quote="financing",
        ),
    ]

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, media_page)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "financing_requires_official_or_two_independent_b_sources"


def test_space_laser_business_and_financing_quotes_combine_at_page_level(
    media_page, financing_analysis
):
    media_page.text += " Orbit Corp provides 星地激光通信 products."
    non_classification = [
        item
        for item in financing_analysis.evidence
        if item.field not in {"in_china", "in_scope", "category", "event_type"}
    ]
    financing_analysis.evidence = [
        *non_classification,
        *classification_evidence(
            media_page.final_url,
            "星地激光通信",
            country_quote="中国商业航天企业",
            category_quote="星地激光通信",
            event_quote="financing",
        ),
    ]

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, media_page)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "financing_requires_official_or_two_independent_b_sources"


@pytest.mark.parametrize(
    ("scope_quote", "country_quote", "category_quote", "event_quote", "expected"),
    [
        (
            "商业航天",
            "Orbit Corp",
            "商业航天",
            "financing",
            "classification_country_evidence_invalid",
        ),
        (
            "Orbit Corp",
            "中国商业航天企业",
            "Orbit Corp",
            "financing",
            "classification_category_evidence_invalid",
        ),
        (
            "商业航天",
            "中国商业航天企业",
            "商业航天",
            "Orbit Corp",
            "classification_event_evidence_invalid",
        ),
        (
            "Orbit Corp",
            "中国商业航天企业",
            "商业航天",
            "financing",
            "classification_scope_evidence_invalid",
        ),
    ],
)
def test_classification_evidence_reports_precise_invalid_reason(
    media_page,
    financing_analysis,
    scope_quote,
    country_quote,
    category_quote,
    event_quote,
    expected,
):
    non_classification = [
        item
        for item in financing_analysis.evidence
        if item.field not in {"in_china", "in_scope", "category", "event_type"}
    ]
    financing_analysis.evidence = [
        *non_classification,
        *classification_evidence(
            media_page.final_url,
            scope_quote,
            country_quote=country_quote,
            category_quote=category_quote,
            event_quote=event_quote,
        ),
    ]

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, media_page)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == expected


def test_two_independent_b_articles_verify_with_deterministic_fallback() -> None:
    primary = page(
        "https://media.example.cn/longqing",
        "2026年7月23日，北京龙擎空天科技有限公司宣布完成近亿元Pre-A+轮融资。\n"
        "该公司面向商业航天领域研发低轨卫星终端及星载智算产品。",
    ).model_copy(update={"title": "龙擎空天再获Pre-A+轮融资"})
    secondary = page(
        "https://other.example.cn/longqing",
        "2026年7月23日，北京龙擎空天科技有限公司完成近亿元Pre-A+轮融资。\n"
        "龙擎空天是一家研发低轨卫星终端的中国商业航天企业。",
    ).model_copy(update={"title": "龙擎空天完成近亿元Pre-A+轮融资"})
    analyzer = RuleFallbackAnalyzer()
    primary_analysis = analyzer.analyze(primary)
    secondary_analysis = analyzer.analyze(secondary)

    decision = RuleVerifier(REGISTRY).verify(
        primary_analysis,
        primary,
        [(secondary_analysis, secondary)],
    )

    assert decision.status is VerificationStatus.VERIFIED
    assert decision.reason == "verified_financing_two_independent_sources"
    assert {record.source_url for record in decision.source_records} == {
        primary.final_url,
        secondary.final_url,
    }


def test_two_independent_space_laser_financing_articles_verify_after_evidence_enrichment():
    primary = page(
        "https://media.example.cn/guangyou",
        "2026年7月21日，北京光邮星空科技有限公司聚焦高速星地激光通信领域。\n"
        "公司近日完成数千万元Pre-A+轮融资。",
    ).model_copy(update={"title": "光邮星空完成Pre-A+轮融资"})
    secondary = page(
        "https://other.example.cn/guangyou",
        "2026年7月21日，北京光邮星空科技有限公司提供空间激光通信产品。\n"
        "北京光邮星空科技有限公司近日完成数千万元Pre-A+轮融资。",
    ).model_copy(update={"title": "光邮星空完成Pre-A+轮融资"})

    class NarrowPrimary:
        def analyze(self, source):
            category_quote = (
                "星地激光通信"
                if "星地激光通信" in source.text
                else "空间激光通信"
            )
            return AnalysisResult(
                in_china=True,
                in_scope=True,
                category=Category.COMMERCIAL_SPACE_FINANCING,
                event_type=EventType.FINANCING,
                title=source.title,
                source_url=source.final_url,
                evidence=[
                    item
                    for item in classification_evidence(
                        source.final_url,
                        category_quote,
                        country_quote="北京光邮星空科技有限公司",
                        category_quote=category_quote,
                        event_quote="Pre-A+轮融资",
                    )
                    if item.field != "in_china"
                ],
            )

    analyzer = ResilientAnalyzer(NarrowPrimary(), RuleFallbackAnalyzer())
    primary_analysis = analyzer.analyze(primary)
    secondary_analysis = analyzer.analyze(secondary)

    assert primary_analysis.organization == secondary_analysis.organization
    assert primary_analysis.published_at == secondary_analysis.published_at
    assert primary_analysis.financing_round == secondary_analysis.financing_round
    assert primary_analysis.amount == secondary_analysis.amount
    assert any(
        item.field == "in_china"
        and item.quote == "北京光邮星空科技有限公司"
        for item in primary_analysis.evidence
    )
    assert any(
        item.field == "in_china"
        and item.quote == "北京光邮星空科技有限公司"
        for item in secondary_analysis.evidence
    )

    decision = RuleVerifier(REGISTRY).verify(
        primary_analysis,
        primary,
        [(secondary_analysis, secondary)],
    )

    assert decision.status is VerificationStatus.VERIFIED
    assert decision.reason == "verified_financing_two_independent_sources"


def test_grade_a_financing_rejects_empty_evidence(financing_analysis):
    official = page(
        PUBLIC_URL,
        "中国商业航天企业 Orbit Corp closes Series B financing on 2026-07-21，"
        "金额1亿元，Capital One投资。",
    )
    financing_analysis.source_url = official.final_url
    financing_analysis.evidence = classification_evidence(
        official.final_url,
        "中国商业航天企业 Orbit Corp closes Series B financing on 2026-07-21，金额1亿元，Capital One投资。",
        country_quote="中国商业航天企业",
        category_quote="商业航天",
        event_quote="financing",
    )

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, official)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "financing_missing_required_evidence"


def test_financing_evidence_gaps_distinguish_omitted_and_undisclosed_amount(
    financing_analysis,
):
    original = financing_analysis.model_copy(deep=True)
    financing_analysis.amount = None
    financing_analysis.amount_disclosed = None
    financing_analysis.evidence = [
        item for item in financing_analysis.evidence if item.field != "amount"
    ]

    assert financing_evidence_gaps(financing_analysis) == ("amount",)
    assert financing_analysis.amount is None
    assert financing_analysis.amount_disclosed is None

    financing_analysis.amount_disclosed = False
    financing_analysis.evidence.append(
        Evidence(
            field="amount",
            quote="具体融资金额未披露",
            source_url=financing_analysis.source_url,
        )
    )
    assert financing_evidence_gaps(financing_analysis) == ()
    assert original.amount == "1亿元"


def test_financing_evidence_gaps_report_only_required_present_claims(
    financing_analysis,
):
    financing_analysis.evidence = [
        item
        for item in financing_analysis.evidence
        if item.field not in {"published_at", "financing_round", "investors"}
    ]

    assert financing_evidence_gaps(financing_analysis) == (
        "published_at",
        "financing_round",
        "investors",
    )


def test_grade_a_financing_rejects_unrelated_literal_evidence(financing_analysis):
    unrelated = "Details were disclosed in this official announcement"
    official = page(
        PUBLIC_URL,
        f"中国商业航天企业 Orbit Corp closes Series B financing on 2026-07-21，"
        f"金额1亿元，Capital One投资。 {unrelated}",
    )
    financing_analysis.source_url = official.final_url
    financing_analysis.evidence = [
        Evidence(field=field, quote=unrelated, source_url=official.final_url)
        for field in ("organization", "published_at", "financing_round", "amount", "investors")
    ] + classification_evidence(
        official.final_url,
        "中国商业航天企业 Orbit Corp closes Series B financing on 2026-07-21，金额1亿元，Capital One投资。",
        country_quote="中国商业航天企业",
        category_quote="商业航天",
        event_quote="financing",
    )

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, official)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "evidence_not_grounded"


def test_grade_a_financing_with_substantive_evidence_verifies(financing_analysis):
    official = page(
        PUBLIC_URL,
        "中国商业航天企业 Orbit Corp closes its Series B financing on 2026/07/21，"
        "金额1亿元，Capital One投资。",
    )
    financing_analysis.source_url = official.final_url
    financing_analysis.evidence = [
        Evidence(field="organization", quote="Orbit Corp", source_url=official.final_url),
        Evidence(field="published_at", quote="2026/07/21", source_url=official.final_url),
        Evidence(field="financing_round", quote="Series B", source_url=official.final_url),
        Evidence(field="amount", quote="1亿元", source_url=official.final_url),
        Evidence(field="investors", quote="Capital One", source_url=official.final_url),
        *classification_evidence(
            official.final_url,
            "中国商业航天企业 Orbit Corp closes its Series B financing on 2026/07/21，金额1亿元，Capital One投资。",
            country_quote="中国商业航天企业",
            category_quote="商业航天",
            event_quote="financing",
        ),
    ]

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, official)

    assert decision.status == VerificationStatus.VERIFIED


def test_financing_category_with_tender_event_is_pending_before_two_b_rule(
    media_page, second_media_page, financing_analysis
):
    financing_analysis.event_type = EventType.TENDER

    decision = RuleVerifier(REGISTRY).verify(
        financing_analysis, media_page, [second_media_page]
    )

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "category_event_type_mismatch"


def test_source_url_mismatch_precedes_category_event_type_mismatch(
    media_page, financing_analysis
):
    financing_analysis.event_type = EventType.TENDER
    financing_analysis.source_url = "https://official.example.cn/different"

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, media_page)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "source_url_mismatch"


def test_financing_event_with_tender_category_is_pending(media_page, financing_analysis):
    financing_analysis.category = Category.LASER_COMMUNICATION

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, media_page)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "category_event_type_mismatch"


def test_two_independent_b_sources_verify_financing(
    media_page, second_media_page, financing_analysis
):
    decision = RuleVerifier(REGISTRY).verify(
        financing_analysis,
        media_page,
        [(financing_claim(second_media_page), second_media_page)],
    )
    assert decision.status == VerificationStatus.VERIFIED, decision.reason


def test_two_b_sources_on_same_registered_domain_are_not_independent(
    media_page, financing_analysis
):
    same_domain = page(
        "https://wire.media.example.cn/other",
        "Orbit Corp Series B financing was announced 2026-07-21.",
    )
    decision = RuleVerifier(REGISTRY).verify(financing_analysis, media_page, [same_domain])

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "financing_requires_official_or_two_independent_b_sources"


def test_overlapping_registered_subdomains_are_not_independent(financing_analysis):
    registry = SourceRegistry(
        {
            "media.example.cn": SourceGrade.B,
            "wire.media.example.cn": SourceGrade.B,
        }
    )
    primary = page(
        "https://media.example.cn/report/1",
        "Orbit Corp Series B financing 2026-07-21",
    )
    other = page(
        "https://wire.media.example.cn/report/2",
        "Orbit Corp Series B financing 2026-07-21",
    )
    financing_analysis.source_url = primary.final_url
    financing_analysis.evidence = []

    decision = RuleVerifier(registry).verify(financing_analysis, primary, [other])

    assert decision.status == VerificationStatus.PENDING


def test_single_letter_financing_round_requires_a_distinct_round_token(
    media_page, second_media_page, financing_analysis
):
    financing_analysis.financing_round = "B"
    financing_analysis.evidence = []
    media_page.text = "Orbit Corp is about rockets on 2026-07-21"
    second_media_page.text = "Orbit Corp builds spacecraft as of 2026-07-21"

    decision = RuleVerifier(REGISTRY).verify(
        financing_analysis, media_page, [second_media_page]
    )

    assert decision.status == VerificationStatus.PENDING


def test_two_b_sources_require_primary_page_to_contain_same_financing_facts(
    media_page, second_media_page, financing_analysis
):
    media_page.text = "A generic article with the evidence phrases removed."
    financing_analysis.evidence = []

    decision = RuleVerifier(REGISTRY).verify(
        financing_analysis, media_page, [second_media_page]
    )

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "classification_evidence_missing"


def test_evidence_must_be_literal_body_substring(official_page, analysis):
    analysis.evidence[0].quote = "model claim absent from body"
    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)
    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "evidence_not_grounded"


def test_missing_fields_take_precedence_over_unavailable_source(official_page, analysis):
    analysis.organization = None
    official_page.status_code = 503

    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.reason == "missing_required_fields:organization"


@pytest.mark.parametrize("field", ["title", "organization", "published_at"])
def test_tender_requires_each_required_evidence_field(official_page, analysis, field):
    analysis.evidence = [item for item in analysis.evidence if item.field != field]
    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "tender_missing_required_evidence"


@pytest.mark.parametrize("flag", ["in_china", "in_scope"])
def test_out_of_scope_analysis_is_rejected(official_page, analysis, flag):
    setattr(analysis, flag, False)
    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.status == VerificationStatus.REJECTED
    assert decision.reason == "out_of_scope"


def test_analysis_result_uses_complete_strict_contract():
    result = AnalysisResult(
        in_china=True,
        in_scope=True,
        title="Orbit Corp financing",
        source_url="https://media.example.cn/report/1",
    )

    assert result.project_codes == []
    assert result.investors == []
    assert result.keywords == []
    assert result.evidence == []


def test_grade_a_office_computer_notice_cannot_cross_classification_boundary():
    office = page(
        PUBLIC_URL,
        "中国境内采购公告\nGeneric office computer tender\n"
        "采购人：National Optics Institute\n发布日期：2026-07-21\n办公计算机采购",
    )
    model_claim = AnalysisResult(
        in_china=True,
        in_scope=True,
        category=Category.LASER_COMMUNICATION,
        event_type=EventType.TENDER,
        title="Generic office computer tender",
        organization="National Optics Institute",
        published_at="2026-07-21T00:00:00+08:00",
        evidence=[
            Evidence(field="in_china", quote="中国境内采购公告", source_url=PUBLIC_URL),
            Evidence(field="in_scope", quote="采购公告", source_url=PUBLIC_URL),
            Evidence(field="category", quote="办公计算机采购", source_url=PUBLIC_URL),
            Evidence(field="event_type", quote="采购公告", source_url=PUBLIC_URL),
            Evidence(field="title", quote="Generic office computer tender", source_url=PUBLIC_URL),
            Evidence(field="organization", quote="National Optics Institute", source_url=PUBLIC_URL),
            Evidence(field="published_at", quote="2026-07-21", source_url=PUBLIC_URL),
        ],
        source_url=PUBLIC_URL,
    )

    decision = RuleVerifier(REGISTRY).verify(model_claim, office)

    assert decision.status is VerificationStatus.PENDING
    assert decision.reason == "classification_rule_disagreement"


def test_official_company_registry_rejects_inserted_competitor_name():
    registry = SourceRegistry(
        {}, financing_company_domains={"landspace.example": ["蓝箭航天", "蓝箭航天科技有限公司"]}
    )

    assert registry.grade_financing("https://landspace.example/news", "蓝箭航天") is SourceGrade.A
    assert registry.grade_financing("https://landspace.example/news", "蓝箭航天科技有限公司") is SourceGrade.A
    assert registry.grade_financing("https://landspace.example/news", "蓝箭航天竞争对手有限公司") is SourceGrade.C


def test_official_investor_domain_requires_named_grounded_participation():
    registry = SourceRegistry(
        {}, financing_investor_domains={"capital.example": ["远航基金", "远航产业基金"]}
    )
    evidence = [
        Evidence(
            field="investors",
            quote="远航产业基金参与本轮融资",
            source_url="https://capital.example/news",
        )
    ]

    assert registry.grade_financing(
        "https://capital.example/news", "星舟航天", ["远航产业基金"], evidence
    ) is SourceGrade.A
    assert registry.grade_financing(
        "https://capital.example/news", "星舟航天", ["无关基金"], evidence
    ) is SourceGrade.C
    assert registry.grade_financing(
        "https://capital.example/news", "星舟航天", ["远航产业基金"], []
    ) is SourceGrade.C


@pytest.mark.parametrize(
    ("subtype", "phrase", "expected"),
    [
        ("capital_increase", "产业基金增资", VerificationStatus.VERIFIED),
        ("strategic", "战略融资", VerificationStatus.VERIFIED),
        ("merger_acquisition", "并购融资", VerificationStatus.VERIFIED),
        ("capital_increase", "增资", VerificationStatus.PENDING),
    ],
)
def test_explicit_non_round_financing_subtypes_do_not_require_round(
    official_page, financing_analysis, subtype, phrase, expected
):
    financing_page = page(
        PUBLIC_URL,
        "Orbit Corp closes financing\n中国商业航天企业 Orbit Corp 2026-07-21 金额1亿元 Capital One投资 financing\n" + phrase,
    )
    financing_analysis.financing_round = None
    financing_analysis.financing_subtype = subtype
    financing_analysis.source_url = financing_page.final_url
    financing_analysis.evidence = [
        item.model_copy(update={"source_url": financing_page.final_url})
        for item in financing_analysis.evidence
        if item.field != "financing_round"
    ]
    financing_analysis.evidence.append(
        Evidence(field="financing_subtype", quote=phrase, source_url=financing_page.final_url)
    )
    financing_page.text = "\n".join(item.quote for item in financing_analysis.evidence)

    decision = RuleVerifier(REGISTRY).verify(financing_analysis, financing_page)

    assert decision.status is expected, decision.reason


def test_model_category_disagreement_with_grounded_source_evidence_is_pending():
    laser = page(
        PUBLIC_URL,
        "中国境内激光通信终端招标公告\n采购人：National Optics Institute\n"
        "发布日期：2026-07-21",
    )
    model_claim = AnalysisResult(
        in_china=True,
        in_scope=True,
        category=Category.EO_TURRET,
        event_type=EventType.TENDER,
        title="中国境内激光通信终端招标公告",
        organization="National Optics Institute",
        published_at="2026-07-21T00:00:00+08:00",
        evidence=[
            Evidence(field="in_china", quote="中国境内", source_url=PUBLIC_URL),
            Evidence(field="in_scope", quote="激光通信终端招标", source_url=PUBLIC_URL),
            Evidence(field="category", quote="激光通信终端", source_url=PUBLIC_URL),
            Evidence(field="event_type", quote="招标公告", source_url=PUBLIC_URL),
            Evidence(field="title", quote="中国境内激光通信终端招标公告", source_url=PUBLIC_URL),
            Evidence(field="organization", quote="National Optics Institute", source_url=PUBLIC_URL),
            Evidence(field="published_at", quote="2026-07-21", source_url=PUBLIC_URL),
        ],
        source_url=PUBLIC_URL,
    )

    decision = RuleVerifier(REGISTRY).verify(model_claim, laser)

    assert decision.status is VerificationStatus.PENDING
    assert decision.reason == "classification_rule_disagreement"


def test_procurement_deadline_without_field_evidence_stays_pending(
    official_page, analysis
):
    analysis.bid_submission_deadline = datetime(
        2026, 7, 25, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    analysis.deadline_precision = {"bid_submission": "minute"}

    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.status is VerificationStatus.PENDING
    assert decision.reason == "deadline_evidence_missing"


def test_procurement_deadline_evidence_must_support_exact_time(
    official_page, analysis
):
    official_page.text += "\n投标截止时间：2026-07-24 09:30"
    analysis.bid_submission_deadline = datetime(
        2026, 7, 25, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    analysis.deadline_precision = {"bid_submission": "minute"}
    analysis.evidence.append(
        Evidence(
            field="bid_submission_deadline",
            quote="投标截止时间：2026-07-24 09:30",
            source_url=official_page.final_url,
        )
    )

    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)

    assert decision.status is VerificationStatus.PENDING
    assert decision.reason == "deadline_evidence_invalid"


def financing_claim(
    source: FetchedPage,
    *,
    announced_at: str = "2026-07-21T00:00:00+08:00",
    round_name: str = "Series B",
    amount: str = "1亿元",
) -> AnalysisResult:
    scope_quote = next(
        line for line in source.text.splitlines() if "商业航天" in line
    )
    evidence = [
        Evidence(field="organization", quote="Orbit Corp", source_url=source.final_url),
        Evidence(field="published_at", quote=announced_at[:10], source_url=source.final_url),
        Evidence(field="financing_round", quote=round_name, source_url=source.final_url),
        Evidence(field="amount", quote=amount, source_url=source.final_url),
        Evidence(field="investors", quote="Capital One", source_url=source.final_url),
        *classification_evidence(
            source.final_url,
            scope_quote,
            country_quote="中国商业航天企业",
            category_quote="商业航天",
        event_quote="融资" if "融资" in source.text else "financing",
        ),
    ]
    return AnalysisResult(
        in_china=True,
        in_scope=True,
        category=Category.COMMERCIAL_SPACE_FINANCING,
        event_type=EventType.FINANCING,
        title=source.title,
        organization="Orbit Corp",
        published_at=announced_at,
        financing_round=round_name,
        amount=amount,
        investors=["Capital One"],
        evidence=evidence,
        source_url=source.final_url,
    )


def test_two_b_financing_requires_independent_analysis_and_persists_both_records():
    primary = page(
        "https://media.example.cn/report/independent",
        "中国商业航天企业 Orbit Corp 于 2026-07-21 完成 Series B 融资，"
        "金额1亿元，Capital One投资。",
    )
    secondary = page(
        "https://other.example.cn/story/independent",
        "中国商业航天企业 Orbit Corp Series B 融资于 2026-07-21 完成，"
        "金额1亿元，投资方Capital One。",
    )

    try:
        decision = RuleVerifier(REGISTRY).verify(
            financing_claim(primary),
            primary,
            [(financing_claim(secondary), secondary)],
        )
    except (AttributeError, TypeError) as error:
        pytest.fail(f"verifier did not consume independently analyzed sources: {error}")

    assert decision.status is VerificationStatus.VERIFIED
    assert {record.source_url for record in decision.source_records} == {
        primary.final_url,
        secondary.final_url,
    }
    assert {item.source_url for item in decision.evidence} == {
        primary.final_url,
        secondary.final_url,
    }


@pytest.mark.parametrize(
    ("secondary_date", "secondary_round", "secondary_amount"),
    [
        ("2026-07-21T00:00:00+08:00", "Series B", "2亿元"),
        ("2026-07-21T00:00:00+08:00", "Series C", "1亿元"),
        ("2026-07-20T00:00:00+08:00", "Series B", "1亿元"),
    ],
)
def test_two_b_financing_conflicting_critical_fields_stay_pending(
    secondary_date, secondary_round, secondary_amount
):
    primary = page(
        "https://media.example.cn/report/conflict",
        "中国商业航天企业 Orbit Corp 于 2026-07-21 完成 Series B 融资，"
        "金额1亿元，Capital One投资。",
    )
    rendered_date = secondary_date[:10]
    secondary = page(
        "https://other.example.cn/story/conflict",
        f"中国商业航天企业 Orbit Corp 于 {rendered_date} 完成 {secondary_round} 融资，"
        f"金额{secondary_amount}，Capital One投资。",
    )

    try:
        decision = RuleVerifier(REGISTRY).verify(
            financing_claim(primary),
            primary,
            [
                (
                    financing_claim(
                        secondary,
                        announced_at=secondary_date,
                        round_name=secondary_round,
                        amount=secondary_amount,
                    ),
                    secondary,
                )
            ],
        )
    except (AttributeError, TypeError) as error:
        pytest.fail(f"verifier did not compare independently analyzed sources: {error}")

    assert decision.status is VerificationStatus.PENDING
    assert decision.reason == "financing_corroboration_conflict"


def test_two_b_passive_company_round_mention_is_not_corroboration():
    primary = page(
        "https://media.example.cn/report/assertion",
        "中国商业航天企业 Orbit Corp 于 2026-07-21 完成 Series B 融资，"
        "金额1亿元，Capital One投资。",
    )
    passive = page(
        "https://other.example.cn/story/passive",
        "中国商业航天企业 Orbit Corp 的 Series B 案例资料日期为 2026-07-21，"
        "金额1亿元，提及 Capital One。",
    )
    passive_analysis = financing_claim(passive).model_copy(
        update={"in_scope": False, "category": None, "event_type": None}
    )

    try:
        decision = RuleVerifier(REGISTRY).verify(
            financing_claim(primary), primary, [(passive_analysis, passive)]
        )
    except (AttributeError, TypeError) as error:
        pytest.fail(f"verifier did not reject passive analyzed source: {error}")

    assert decision.status is VerificationStatus.PENDING
    assert decision.reason == "financing_corroboration_insufficient"


def test_two_b_identical_syndicated_content_is_one_source():
    text = (
        "中国商业航天企业 Orbit Corp 于 2026-07-21 完成 Series B 融资，"
        "金额1亿元，Capital One投资。"
    )
    primary = page("https://media.example.cn/report/wire", text)
    syndicated = page("https://other.example.cn/story/wire", text)

    try:
        decision = RuleVerifier(REGISTRY).verify(
            financing_claim(primary),
            primary,
            [(financing_claim(syndicated), syndicated)],
        )
    except (AttributeError, TypeError) as error:
        pytest.fail(f"verifier did not inspect syndicated content identity: {error}")

    assert decision.status is VerificationStatus.PENDING
    assert decision.reason == "financing_requires_independent_sources"
