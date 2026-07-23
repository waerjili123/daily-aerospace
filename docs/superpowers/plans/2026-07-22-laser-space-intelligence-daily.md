# 激光与商业航天情报日报 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建一个与“AI日报”完全隔离的 Python 项目，每天北京时间 07:30 自动采集、核验、串联并推送中国激光通信、激光武器/反无人机、光电转塔/吊舱招采动态及中国商业航天一级市场融资日报。

**Architecture:** 流水线以可替换接口连接 Tavily 搜索、官方种子页采集、网页正文读取、DeepSeek 结构化分析、确定性核验与实体归并、JSON/JSONL 状态仓库、Markdown 渲染和钉钉 Webhook。模型只处理工具返回的真实正文与 URL；正式入库必须通过确定性来源和证据规则，同一招采项目以事件链维护并生成最近 24 小时变化与滚动三个月项目池。

**Tech Stack:** Python 3.13、Pydantic 2、httpx、BeautifulSoup4、trafilatura、PyYAML、python-dateutil、OpenAI-compatible DeepSeek API、Tavily Search API、pytest、respx、GitHub Actions。

## Global Constraints

- 新项目根目录固定为 `激光与商业航天情报日报/`，不得修改或向 `AI日报/` 写入文件。
- 只收集中国境内公开信息；不得出现 AI 新闻采集器、AI 新闻提示词、AI 新闻历史数据或 AI 日报推送内容。
- 招采范围覆盖采购意向、招标/询价/比选、变更/延期/终止、候选人公示、中标/成交、废标和二次招标，并串联同一项目。
- 正式招采事件原则上只接受 A 级官方来源；融资无正式公告时必须有两个相互独立的 B 级可靠来源。
- DeepSeek 不得生成输入中不存在的 URL、日期、金额、企业、采购方或投资方。
- 顶部窗口为执行时刻前 24 小时；滚动项目池窗口为最新动态发生日向前 3 个自然月。
- 每天北京时间 07:30 运行；GitHub Actions cron 使用 UTC `30 23 * * *`。
- 单次只发送一条钉钉 Markdown；达到长度上限时不得裁剪 24 小时变化、可报名项目、即将启动项目和融资事件。
- 状态使用 Git 友好的 UTF-8 JSON/JSONL；同日重跑必须幂等。
- 密钥只从 `DEEPSEEK_API_KEY`、`TAVILY_API_KEY`、`DINGTALK_WEBHOOK` 环境变量读取，绝不写入日志、报告或仓库。
- 每项功能先写失败测试，再实现最小代码，任务结束时运行对应测试并提交。

## File Structure

```text
激光与商业航天情报日报/
├── .github/workflows/daily-intelligence.yml     # 定时、手动 dry-run、状态提交
├── .gitignore                                   # 密钥、本地缓存和临时产物排除
├── README.md                                    # 配置、Secret、运行和迁移说明
├── pyproject.toml                               # 依赖、pytest 与命令入口
├── config.example.yaml                          # 非敏感搜索、来源和长度配置模板
├── config.yaml                                  # GitHub Actions 使用的非敏感正式配置
├── config/official_sources.yaml                 # 官方来源域名、等级和种子页
├── src/laser_space_daily/
│   ├── __init__.py
│   ├── cli.py                                   # 参数解析、配置加载和退出码
│   ├── config.py                                # Pydantic 配置及环境变量校验
│   ├── models.py                                # 候选、事件、项目、融资、待核实、指标
│   ├── timebox.py                               # 北京时区 24h/3 月窗口
│   ├── discovery.py                             # 查询规划、Tavily 与官方种子采集
│   ├── fetcher.py                               # URL 安全校验、下载和正文提取
│   ├── verifier.py                              # 来源分级、证据与中国/范围核验
│   ├── analyzer.py                              # DeepSeek JSON 分析与规则降级
│   ├── matching.py                              # 项目串联、事件/融资去重
│   ├── repository.py                            # 原子读写 JSON/JSONL
│   ├── pipeline.py                              # 端到端编排、降级和运行指标
│   ├── report.py                                # 单条钉钉 Markdown 生成与压缩
│   └── notifier.py                              # 钉钉 Webhook 推送
├── data/events.jsonl
├── data/projects.json
├── data/financings.json
├── data/pending.json
├── reports/.gitkeep
└── tests/
    ├── fixtures/                                # 脱敏公告、融资网页及 API 响应
    ├── snapshots/                               # Markdown 预期输出
    ├── test_config_models.py
    ├── test_discovery.py
    ├── test_fetch_verify.py
    ├── test_analyzer.py
    ├── test_matching_repository.py
    ├── test_report_notifier.py
    └── test_pipeline.py
```

---

### Task 1: 项目骨架、配置、时间窗口与领域模型

**Files:**
- Create: `激光与商业航天情报日报/pyproject.toml`
- Create: `激光与商业航天情报日报/.gitignore`
- Create: `激光与商业航天情报日报/config.example.yaml`
- Create: `激光与商业航天情报日报/src/laser_space_daily/{__init__,config,models,timebox}.py`
- Create: `激光与商业航天情报日报/tests/test_config_models.py`

**Interfaces:**
- Produces: `load_settings(path: Path) -> Settings`
- Produces: `beijing_now() -> datetime`、`daily_window(now) -> tuple[datetime, datetime]`、`rolling_start(now) -> datetime`
- Produces: `Candidate`、`Evidence`、`AnalysisResult`、`TrendSummary`、`Event`、`Project`、`Financing`、`PendingItem`、`RunMetrics`、`StateBundle`

- [ ] **Step 1: 写配置、枚举、模型和时间窗口的失败测试**

```python
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from laser_space_daily.config import load_settings
from laser_space_daily.models import Category, Event, SourceGrade, VerificationStatus
from laser_space_daily.timebox import daily_window, rolling_start

def test_settings_require_three_secrets(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("report:\n  max_chars: 18000\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        load_settings(path)

def test_windows_use_beijing_time_and_calendar_months():
    now = datetime(2026, 7, 22, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    start, end = daily_window(now)
    assert start.isoformat() == "2026-07-21T07:30:00+08:00"
    assert end == now
    assert rolling_start(now).isoformat() == "2026-04-22T07:30:00+08:00"

def test_event_rejects_unverified_formal_record():
    with pytest.raises(ValueError, match="verified"):
        Event(event_id="e1", category=Category.LASER_COMMUNICATION,
              title="空间激光通信终端", organization="某研究院",
              published_at="2026-07-22T00:00:00+08:00",
              source_url="https://example.gov.cn/a", source_grade=SourceGrade.A,
              verification_status=VerificationStatus.PENDING)
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_config_models.py -v`

Expected: FAIL，包含 `ModuleNotFoundError: No module named 'laser_space_daily'`。

- [ ] **Step 3: 创建依赖、配置和模型最小实现**

```toml
[project]
name = "laser-space-intelligence-daily"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "beautifulsoup4>=4.12,<5", "httpx>=0.28,<1", "openai>=1.100,<2",
  "pydantic>=2.11,<3", "PyYAML>=6.0,<7", "python-dateutil>=2.9,<3",
  "trafilatura>=2.0,<3"
]
[project.optional-dependencies]
dev = ["pytest>=8.4,<9", "pytest-cov>=6.2,<7", "respx>=0.22,<1"]
[project.scripts]
laser-space-daily = "laser_space_daily.cli:main"
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"
[tool.pytest.ini_options]
pythonpath = ["src"]
```

`models.py` 必须定义字符串枚举 `Category`（`laser_communication`、`laser_weapon`、`eo_turret`、`commercial_space_financing`）、`EventType`（`procurement_intention`、`tender`、`inquiry`、`comparison`、`change`、`extension`、`termination`、`candidate`、`award`、`failed`、`rebid`、`financing`）、`SourceGrade`（A/B/C）和 `VerificationStatus`（verified/pending/rejected），所有模型继承 `pydantic.BaseModel`。`AnalysisResult`、`TrendSummary` 和聚合 `events/projects/financings/pending` 四个列表的 `StateBundle` 也在此处定义，供 analyzer、verifier、repository、pipeline 和 report 共用，避免循环依赖。`Event` 使用 `@model_validator(mode="after")` 拒绝非 `verified` 状态；`Evidence` 固定字段为 `field`、`quote`、`source_url`；ID、URL、时间、金额、项目编号和证据字段均显式声明，禁止额外字段。

`config.py` 使用 `yaml.safe_load` 读取配置，只从环境变量读取三个 Secret；`Settings` 包含 Tavily/DeepSeek 超时与模型名、报告字符上限、数据目录和来源配置路径。`timebox.py` 使用 `ZoneInfo("Asia/Shanghai")` 和 `dateutil.relativedelta(months=3)`，不得用 90 天代替三个自然月。

- [ ] **Step 4: 运行模型测试并确认通过**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_config_models.py -v`

Expected: 3 passed。

- [ ] **Step 5: 提交骨架**

```bash
cd 激光与商业航天情报日报
git init -b main
git add .
git commit -m "feat: scaffold laser and space intelligence daily"
```

### Task 2: 查询规划、Tavily 搜索与官方种子采集

**Files:**
- Create: `激光与商业航天情报日报/config/official_sources.yaml`
- Create: `激光与商业航天情报日报/src/laser_space_daily/discovery.py`
- Create: `激光与商业航天情报日报/tests/test_discovery.py`
- Create: `激光与商业航天情报日报/tests/fixtures/tavily_search.json`
- Create: `激光与商业航天情报日报/tests/fixtures/official_list.html`

**Interfaces:**
- Consumes: `Settings`、`Candidate`、`Project`
- Produces: `QueryPlanner.plan(now, projects) -> list[SearchQuery]`
- Produces: `TavilyProvider.search(query) -> list[Candidate]`
- Produces: `OfficialSeedCollector.collect() -> list[Candidate]`

- [ ] **Step 1: 写失败测试，覆盖四类检索和 URL 原样保留**

```python
def test_planner_covers_incremental_backfill_and_overdue(project_factory, fixed_now):
    queries = QueryPlanner(max_queries=40).plan(fixed_now, [project_factory(needs_recheck=True)])
    kinds = {q.kind for q in queries}
    assert kinds == {"incremental", "project_followup", "rolling_recheck", "overdue_result"}
    assert any("激光通信" in q.text for q in queries)
    assert any("激光反无人机" in q.text for q in queries)
    assert any("光电转塔" in q.text for q in queries)
    assert any("商业航天 融资" in q.text for q in queries)

def test_tavily_keeps_tool_url(respx_mock, tavily_payload):
    respx_mock.post("https://api.tavily.com/search").respond(200, json=tavily_payload)
    rows = TavilyProvider("secret").search(SearchQuery(kind="incremental", text="测试"))
    assert rows[0].url == tavily_payload["results"][0]["url"]

def test_official_collector_continues_after_one_seed_fails(respx_mock):
    respx_mock.get("https://bad.gov.cn/list").respond(503)
    respx_mock.get("https://good.gov.cn/list").respond(200, text=OFFICIAL_HTML)
    rows = OfficialSeedCollector(SEEDS).collect()
    assert [row.url for row in rows] == ["https://good.gov.cn/notice/1"]
```

- [ ] **Step 2: 运行并确认 discovery 模块缺失**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_discovery.py -v`

Expected: FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现检索接口和确定性去重**

```python
@dataclass(frozen=True)
class SearchQuery:
    kind: Literal["incremental", "project_followup", "rolling_recheck", "overdue_result"]
    text: str

class OfficialSeed(BaseModel):
    name: str
    domain: str
    grade: SourceGrade
    list_urls: list[HttpUrl]
    link_selector: str

class TavilyProvider:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=20.0, follow_redirects=True)

    def search(self, query: SearchQuery) -> list[Candidate]:
        response = self.client.post("https://api.tavily.com/search", json={
            "api_key": self.api_key, "query": query.text, "topic": "general",
            "search_depth": "advanced", "max_results": 10, "include_answer": False,
        })
        response.raise_for_status()
        now = beijing_now()
        return [Candidate(title=row["title"], url=row["url"], summary=row.get("content", ""),
                          discovered_at=now, discovery_source="tavily")
                for row in response.json().get("results", [])]

class OfficialSeedCollector:
    def __init__(self, seeds: list[OfficialSeed], client: httpx.Client | None = None):
        self.seeds = seeds
        self.client = client or httpx.Client(timeout=20.0, follow_redirects=True)

    def collect(self) -> list[Candidate]:
        found: list[Candidate] = []
        for seed in self.seeds:
            for list_url in seed.list_urls:
                try:
                    response = self.client.get(list_url)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "html.parser")
                    found.extend(Candidate(title=node.get_text(" ", strip=True),
                                           url=urljoin(list_url, node["href"]), summary="",
                                           discovered_at=beijing_now(), discovery_source=seed.name)
                                 for node in soup.select(seed.link_selector) if node.get("href"))
                except (httpx.HTTPError, ValueError, KeyError):
                    continue
        return found

def dedupe_candidates(rows: Iterable[Candidate]) -> list[Candidate]:
    by_url: dict[str, Candidate] = {}
    for row in rows:
        normalized = normalize_url(row.url)
        by_url.setdefault(normalized, row.model_copy(update={"url": normalized}))
    return list(by_url.values())
```

Tavily 请求体固定包含 `query`、`topic="general"`、`search_depth="advanced"`、`max_results`、`include_answer=false`。官方种子 YAML 为每个站点声明 `name`、`domain`、`grade: A`、`list_urls`、`link_selector`；首版固定配置 `https://www.ccgp.gov.cn/cggg/`、`https://www.plap.mil.cn/freecms/site/juncai/cggg/index.html`、`https://bulletin.cebpubservice.com/`、`https://www.ggzy.gov.cn/`。为每个入口保存脱敏列表页 fixture，并用该 fixture 锁定各自的公告链接 selector；站点改版导致 selector 失效时记录覆盖降级，不猜测新链接。每个站点捕获独立异常并记录失败域名，不中断其他站点。

- [ ] **Step 4: 运行 discovery 测试**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_discovery.py -v`

Expected: 3 passed。

- [ ] **Step 5: 提交发现层**

```bash
cd 激光与商业航天情报日报
git add config src/laser_space_daily/discovery.py tests
git commit -m "feat: add search and official source discovery"
```

### Task 3: 安全网页读取、正文提取和来源核验

**Files:**
- Create: `激光与商业航天情报日报/src/laser_space_daily/fetcher.py`
- Create: `激光与商业航天情报日报/src/laser_space_daily/verifier.py`
- Create: `激光与商业航天情报日报/tests/test_fetch_verify.py`
- Create: `激光与商业航天情报日报/tests/fixtures/tender_notice.html`
- Create: `激光与商业航天情报日报/tests/fixtures/financing_article.html`

**Interfaces:**
- Produces: `PageFetcher.fetch(candidate) -> FetchedPage`
- Produces: `SourceRegistry.grade(url) -> SourceGrade`
- Produces: `RuleVerifier.verify(analysis, page) -> VerificationDecision`

- [ ] **Step 1: 写 URL 安全、正文和验证规则的失败测试**

```python
@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/a", "http://169.254.169.254/latest"])
def test_fetcher_blocks_non_public_urls(url):
    with pytest.raises(UnsafeUrl):
        PageFetcher().fetch(Candidate(url=url, title="x", discovered_at=NOW))

def test_tender_requires_grade_a_and_field_evidence(official_page, analysis):
    decision = RuleVerifier(REGISTRY).verify(analysis, official_page)
    assert decision.status == VerificationStatus.VERIFIED
    assert {e.field for e in decision.evidence} >= {"title", "organization", "published_at"}

def test_single_b_source_financing_stays_pending(media_page, financing_analysis):
    decision = RuleVerifier(REGISTRY).verify(financing_analysis, media_page)
    assert decision.status == VerificationStatus.PENDING
    assert decision.reason == "financing_requires_official_or_two_independent_b_sources"
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_fetch_verify.py -v`

Expected: FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现 SSRF 防护、正文提取与验证决策**

`PageFetcher` 只允许 `http/https`，解析 DNS 后拒绝 loopback、private、link-local、multicast 和 reserved IP；重定向每一跳重复校验，限制 5 次重定向、10 MB 响应体和配置超时。正文优先使用 trafilatura，失败时用 BeautifulSoup 删除 `script/style/nav` 后提取文本，并返回以下固定模型：

```python
class FetchedPage(BaseModel):
    requested_url: HttpUrl
    final_url: HttpUrl
    status_code: int
    title: str
    text: str
    fetched_at: datetime
    content_hash: str

class SourceRegistry:
    def __init__(self, exact_domains: dict[str, SourceGrade]):
        self.exact_domains = {domain.lower().strip("."): grade for domain, grade in exact_domains.items()}

    def grade(self, url: str) -> SourceGrade:
        host = (urlparse(str(url)).hostname or "").lower().strip(".")
        matches = [(domain, grade) for domain, grade in self.exact_domains.items()
                   if host == domain or host.endswith("." + domain)]
        return max(matches, key=lambda item: len(item[0]))[1] if matches else SourceGrade.C
```

```python
class VerificationDecision(BaseModel):
    status: VerificationStatus
    reason: str
    source_grade: SourceGrade
    evidence: list[Evidence]

class RuleVerifier:
    def verify(self, analysis: AnalysisResult, page: FetchedPage,
               corroborating: list[FetchedPage] | None = None) -> VerificationDecision:
        if not analysis.in_china or not analysis.in_scope:
            return VerificationDecision(status="rejected", reason="out_of_scope",
                                        source_grade=self.registry.grade(page.final_url), evidence=[])
        if analysis.category != Category.COMMERCIAL_SPACE_FINANCING:
            return self._verify_tender_grade_a_and_evidence(analysis, page)
        return self._verify_financing_official_or_two_b(analysis, page, corroborating or [])
```

关键字段的证据片段必须是页面正文的逐字子串；找不到时不得以模型摘要替代。域名按最长后缀精确匹配，禁止用简单的 `endswith("gov.cn")` 判断官方来源。

- [ ] **Step 4: 运行网页与核验测试**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_fetch_verify.py -v`

Expected: 3 passed。

- [ ] **Step 5: 提交读取与验证层**

```bash
cd 激光与商业航天情报日报
git add src/laser_space_daily/fetcher.py src/laser_space_daily/verifier.py tests
git commit -m "feat: verify fetched sources and evidence"
```

### Task 4: DeepSeek 结构化分析、契约校验与规则降级

**Files:**
- Create: `激光与商业航天情报日报/src/laser_space_daily/analyzer.py`
- Create: `激光与商业航天情报日报/tests/test_analyzer.py`
- Create: `激光与商业航天情报日报/tests/fixtures/deepseek_analysis.json`

**Interfaces:**
- Consumes: `FetchedPage`、`Category`、`Settings`
- Produces: `DeepSeekAnalyzer.analyze(page) -> AnalysisResult`
- Produces: `DeepSeekAnalyzer.suggest_match(event, projects) -> MatchSuggestion`
- Produces: `DeepSeekAnalyzer.summarize_trends(state, window) -> TrendSummary`
- Produces: `RuleFallbackAnalyzer.analyze(page) -> AnalysisResult`
- Produces: `guard_grounded_output(result, page) -> AnalysisResult`

- [ ] **Step 1: 写 JSON 契约、幻觉 URL 和降级测试**

```python
def test_deepseek_returns_schema_valid_analysis(fake_openai, official_page):
    fake_openai.reply_json(VALID_ANALYSIS)
    result = DeepSeekAnalyzer(fake_openai, model="deepseek-v4-flash").analyze(official_page)
    assert result.category == Category.LASER_COMMUNICATION
    assert result.source_url == official_page.final_url

def test_grounding_rejects_model_generated_url(official_page):
    bad = AnalysisResult.model_validate({**VALID_ANALYSIS, "source_url": "https://invented.example/a"})
    with pytest.raises(UngroundedOutput, match="source_url"):
        guard_grounded_output(bad, official_page)

def test_analyzer_falls_back_after_two_invalid_responses(fake_openai, official_page):
    fake_openai.reply_text("not json", times=2)
    result = ResilientAnalyzer(DeepSeekAnalyzer(fake_openai), RuleFallbackAnalyzer()).analyze(official_page)
    assert result.degraded is True
    assert result.category == Category.LASER_COMMUNICATION

def test_pro_model_suggestion_cannot_auto_merge(fake_openai, event, projects):
    fake_openai.reply_json({"relation": "same_project", "confidence": 0.84, "reason": "标题相似"})
    suggestion = DeepSeekAnalyzer(fake_openai, pro_model="deepseek-v4-pro").suggest_match(event, projects)
    assert suggestion.requires_human_review is True

def test_financing_scope_excludes_bank_credit(fake_openai, bank_credit_page):
    fake_openai.reply_json(BANK_CREDIT_ANALYSIS)
    result = DeepSeekAnalyzer(fake_openai).analyze(bank_credit_page)
    assert result.in_scope is False
```

- [ ] **Step 2: 运行并确认 analyzer 模块缺失**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_analyzer.py -v`

Expected: FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现严格 Schema、两次重试和关键词降级**

```python
class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_china: bool
    in_scope: bool
    category: Category | None
    event_type: EventType | None
    title: str
    organization: str | None
    published_at: datetime | None
    project_codes: list[str] = []
    amount: str | None = None
    financing_round: str | None = None
    investors: list[str] = []
    keywords: list[str] = []
    evidence: list[Evidence] = []
    source_url: HttpUrl
    degraded: bool = False
```

上述 `AnalysisResult` 的实际定义放在 `models.py`，此处代码用于锁定完整字段契约。`MatchSuggestion` 包含 `relation`、`confidence`、`reason`、`requires_human_review=True`；任何模型建议只能进入疑似关联队列，不能直接修改 `project_id`。`summarize_trends` 只接收三个月窗口内已核验的聚合计数与项目字段，使用 `deepseek-v4-pro` 生成 `TrendSummary`；失败时返回按类别计数和状态分布组成的规则摘要。

系统提示必须明确：只根据给定 URL 与正文输出 JSON；不确定字段为 `null` 或空列表；不得补全事实；每个非空关键字段提供正文逐字证据。调用 `client.chat.completions.create` 时使用配置模型、`temperature=0`、`response_format={"type":"json_object"}`；解析或契约失败最多重试 2 次。`guard_grounded_output` 检查 URL 完全等于抓取最终 URL、证据逐字存在、日期/金额/主体可在正文定位。

`RuleFallbackAnalyzer` 仅用受控关键词和正则产生保守结果：命中板块必要词且能提取标题、主体、日期时才 `in_scope=True`；不能确定的候选交给待核实队列，并标记 `degraded=True`。

- [ ] **Step 4: 运行 analyzer 测试**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_analyzer.py -v`

Expected: 5 passed。

- [ ] **Step 5: 提交模型分析层**

```bash
cd 激光与商业航天情报日报
git add src/laser_space_daily/analyzer.py tests
git commit -m "feat: add grounded DeepSeek analysis"
```

### Task 5: 确定性项目串联、融资去重与状态仓库

**Files:**
- Create: `激光与商业航天情报日报/src/laser_space_daily/matching.py`
- Create: `激光与商业航天情报日报/src/laser_space_daily/repository.py`
- Create: `激光与商业航天情报日报/tests/test_matching_repository.py`
- Create: `激光与商业航天情报日报/data/events.jsonl`
- Create: `激光与商业航天情报日报/data/projects.json`
- Create: `激光与商业航天情报日报/data/financings.json`
- Create: `激光与商业航天情报日报/data/pending.json`

**Interfaces:**
- Produces: `event_fingerprint(event) -> str`、`financing_fingerprint(financing) -> str`
- Produces: `ProjectMatcher.match(event, projects) -> MatchDecision`
- Produces: `StateRepository.load() -> StateBundle`、`StateRepository.commit(bundle) -> None`

- [ ] **Step 1: 写项目编号优先、二次招标串联、跨标段隔离和幂等测试**

```python
def test_exact_project_code_wins(existing_project, result_event):
    result_event.project_codes = [existing_project.project_codes[0]]
    assert ProjectMatcher().match(result_event, [existing_project]).project_id == existing_project.project_id

def test_rebid_links_to_original_but_different_lot_does_not(existing_project, rebid_event, lot_two_event):
    matcher = ProjectMatcher()
    assert matcher.match(rebid_event, [existing_project]).relation == "same_project"
    assert matcher.match(lot_two_event, [existing_project]).relation == "new_project"

def test_repository_same_event_twice_is_idempotent(tmp_path, verified_event):
    repo = StateRepository(tmp_path)
    repo.append_event(verified_event)
    repo.append_event(verified_event)
    assert len(repo.load().events) == 1
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_matching_repository.py -v`

Expected: FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现规范化、指纹、匹配优先级和原子写入**

```python
class MatchDecision(BaseModel):
    relation: Literal["same_project", "suspected", "new_project"]
    project_id: str | None
    reason: str
    score: float

def event_fingerprint(event: Event) -> str:
    payload = "|".join([normalize_url(str(event.source_url)), event.event_type,
                        event.published_at.isoformat(), normalize_text(event.title)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def financing_fingerprint(row: Financing) -> str:
    payload = "|".join([normalize_company(row.company), row.round or "",
                        row.announced_at.date().isoformat(), normalize_amount(row.amount),
                        ",".join(sorted(map(normalize_company, row.investors)))])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

`ProjectMatcher` 固定顺序执行：相同项目/采购编号；相同采购方与规范化标题；相同采购方、标题相似度至少 0.90 且阶段时间可衔接。不同年度、批次、标段默认为新项目；标题含“二次/重新招标”且采购方、核心标题和原编号吻合时作为原项目新事件。分数 0.75–0.90 只返回 `suspected`，写入 pending，不自动合并。

`StateRepository` 写临时文件后用 `Path.replace` 原子替换；JSON 使用 `ensure_ascii=False, indent=2, sort_keys=True`，JSONL 按 `event_id` 去重并稳定排序。`event_id`、`project_id` 使用规范化输入的 UUIDv5，禁止随机 ID 导致同日重跑重复。

- [ ] **Step 4: 运行归并与仓库测试**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_matching_repository.py -v`

Expected: 3 passed。

- [ ] **Step 5: 提交状态层**

```bash
cd 激光与商业航天情报日报
git add src/laser_space_daily/matching.py src/laser_space_daily/repository.py data tests
git commit -m "feat: link lifecycle events and persist state"
```

### Task 6: 端到端流水线、降级路径与运行指标

**Files:**
- Create: `激光与商业航天情报日报/src/laser_space_daily/pipeline.py`
- Create: `激光与商业航天情报日报/tests/test_pipeline.py`

**Interfaces:**
- Consumes: discovery/fetcher/analyzer/verifier/matcher/repository 接口
- Produces: `Pipeline.run(now: datetime) -> RunResult`

- [ ] **Step 1: 写完整编排、服务降级和同日重跑测试**

```python
def test_pipeline_routes_verified_and_pending(deps, fixed_now):
    deps.discovery.rows = [OFFICIAL_CANDIDATE, UNREACHABLE_CANDIDATE]
    result = Pipeline(**deps.as_kwargs()).run(fixed_now)
    assert result.metrics.verified_count == 1
    assert result.metrics.pending_count == 1
    assert len(result.state.events) == 1

def test_tavily_failure_still_runs_official_collector(deps, fixed_now):
    deps.tavily.raise_error = RuntimeError("quota")
    result = Pipeline(**deps.as_kwargs()).run(fixed_now)
    assert result.metrics.coverage_degraded is True
    assert result.metrics.official_candidates > 0

def test_rerun_does_not_duplicate_state(deps, fixed_now):
    first = Pipeline(**deps.as_kwargs()).run(fixed_now)
    second = Pipeline(**deps.as_kwargs()).run(fixed_now)
    assert [e.event_id for e in first.state.events] == [e.event_id for e in second.state.events]

def test_ambiguous_match_is_pending_not_merged(deps, fixed_now):
    deps.matcher.decision = MatchDecision(relation="suspected", project_id="p1", reason="similar", score=0.84)
    result = Pipeline(**deps.as_kwargs()).run(fixed_now)
    assert result.metrics.pending_count == 1
    assert result.state.projects[0].event_ids == []
```

- [ ] **Step 2: 运行并确认失败**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_pipeline.py -v`

Expected: FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现逐候选隔离的编排逻辑**

```python
class Pipeline:
    def run(self, now: datetime) -> RunResult:
        state = self.repository.load()
        queries = self.planner.plan(now, state.projects)
        candidates = self._discover_with_tavily_fallback(queries)
        for candidate in dedupe_candidates(candidates):
            try:
                page = self.fetcher.fetch(candidate)
                analysis = self.analyzer.analyze(page)
                decision = self.verifier.verify(analysis, page, self._corroboration(page, candidates))
                self._apply_decision(state, candidate, page, analysis, decision, now)
            except Exception as exc:
                self._record_pending(state, candidate, stable_error_code(exc), now)
        self.repository.commit(state)
        return self._build_result(state, now)

class RunResult(BaseModel):
    state: StateBundle
    metrics: RunMetrics
    trend_summary: TrendSummary
    window_start: datetime
    window_end: datetime
    rolling_start: datetime
```

`_apply_decision` 必须按事件时间和合法阶段转换更新项目当前状态、金额/预算、截止时间、`needs_recheck` 与公告链；旧事件仍保留，不用新公告覆盖。疑似匹配调用 pro 模型只生成复核建议并写入 pending。流水线结束前调用 `summarize_trends`，将 `TrendSummary` 放入 `RunResult`；模型失败时使用确定性类别计数摘要。

指标必须记录搜索次数、候选数、官方候选数、验证通过数、待核实数、新建项目数、状态更新数、去重数、失败域名、DeepSeek token、Tavily 使用量、模型/搜索覆盖降级标记。日志只记录域名、稳定错误码和计数；对 URL 查询串脱敏，不打印请求头、密钥或完整正文。

- [ ] **Step 4: 运行流水线测试**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_pipeline.py -v`

Expected: 4 passed。

- [ ] **Step 5: 提交流水线**

```bash
cd 激光与商业航天情报日报
git add src/laser_space_daily/pipeline.py tests/test_pipeline.py
git commit -m "feat: orchestrate resilient intelligence pipeline"
```

### Task 7: 钉钉 Markdown 日报渲染与长度压缩

**Files:**
- Create: `激光与商业航天情报日报/src/laser_space_daily/report.py`
- Create: `激光与商业航天情报日报/tests/test_report_notifier.py`
- Create: `激光与商业航天情报日报/tests/snapshots/daily_report.md`

**Interfaces:**
- Consumes: `RunResult`、24 小时窗口、三个月窗口
- Produces: `ReportRenderer.render(result) -> RenderedReport`

- [ ] **Step 1: 写模块顺序、直接链接、公告链和压缩优先级测试**

```python
def test_report_matches_snapshot(run_result, snapshot_text):
    report = ReportRenderer(max_chars=18000).render(run_result)
    assert report.markdown == snapshot_text
    headings = ["过去24小时新增/变化", "当前可报名及即将启动", "激光通信",
                "激光武器/反无人机", "光电转塔/吊舱", "商业航天融资",
                "今日重点跟进", "三个月趋势与数据完整性"]
    assert [report.markdown.index(h) for h in headings] == sorted(report.markdown.index(h) for h in headings)

def test_stage_links_point_to_original_urls(run_result):
    text = ReportRenderer(18000).render(run_result).markdown
    assert "[采购意向](https://official.example/intention)" in text
    assert "[中标结果](https://official.example/award)" in text
    assert "[查看原始公告](https://official.example/award)" in text

def test_compression_never_drops_protected_sections(oversized_result):
    text = ReportRenderer(max_chars=3000).render(oversized_result).markdown
    assert len(text) <= 3000
    assert "过去24小时新增/变化" in text
    assert "当前可报名及即将启动" in text
    assert "商业航天融资" in text

def test_rolling_pool_uses_three_calendar_months_and_keeps_older_history_out(run_result):
    text = ReportRenderer(max_chars=18000).render(run_result).markdown
    assert "2026-04-22边界项目" in text
    assert "2026-04-21历史项目" not in text
```

- [ ] **Step 2: 运行并确认 report 模块缺失**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_report_notifier.py -k report -v`

Expected: FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现稳定 Markdown 和分级压缩**

```python
class RenderedReport(BaseModel):
    title: str
    markdown: str
    omitted_completed_projects: int = 0

class ReportRenderer:
    PROTECTED = ("changes_24h", "open_projects", "upcoming_projects", "financings")
    def render(self, result: RunResult) -> RenderedReport:
        sections = self._build_sections(result)
        omitted_count = 0
        text = self._join(sections)
        if len(text) > self.max_chars:
            sections = self._compact_completed_history(sections)
        if len(self._join(sections)) > self.max_chars:
            sections, omitted_count = self._remove_oldest_completed(sections)
        text = self._join(sections)
        if len(text) > self.max_chars:
            raise ReportTooLong("protected sections exceed configured DingTalk limit")
        return RenderedReport(title=self._title(result), markdown=text,
                              omitted_completed_projects=omitted_count)
```

报告标题包含日期、统计窗口和运行覆盖状态。项目条目依次显示最新动态日期、类别、项目/采购方、规模或金额、当前状态、关键截止期、指向最新事件的 `[查看原始公告]` 和完整公告链；融资条目显示企业、轮次、金额披露情况、投资方、业务方向及来源链。所有链接使用仓库中已核验的原始 URL，不经过模型改写或短链服务。顶部“过去24小时”收录最近 24 小时首次发现的正式事件或项目状态变化，正文同时显示其实际公告日期，避免迟发现公告被误写成当天发布。

- [ ] **Step 4: 生成并审核快照后运行报告测试**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_report_notifier.py -k report -v`

Expected: 4 passed；快照是一条不含 HTML/CSS 的钉钉兼容 Markdown。

- [ ] **Step 5: 提交渲染层**

```bash
cd 激光与商业航天情报日报
git add src/laser_space_daily/report.py tests
git commit -m "feat: render DingTalk intelligence report"
```

### Task 8: 钉钉通知、dry-run、CLI 与可恢复失败

**Files:**
- Create: `激光与商业航天情报日报/src/laser_space_daily/notifier.py`
- Create: `激光与商业航天情报日报/src/laser_space_daily/cli.py`
- Modify: `激光与商业航天情报日报/tests/test_report_notifier.py`
- Modify: `激光与商业航天情报日报/tests/test_pipeline.py`

**Interfaces:**
- Produces: `DingTalkNotifier.send(report) -> None`
- Produces: `run_cli(argv: Sequence[str]) -> int`、`main() -> NoReturn`

- [ ] **Step 1: 写钉钉成功码、失败退出、报告留存和 dry-run 测试**

```python
def test_dingtalk_requires_errcode_zero(respx_mock, rendered_report):
    respx_mock.post("https://oapi.dingtalk.com/robot/send").respond(200, json={"errcode": 310000, "errmsg": "keywords not in content"})
    with pytest.raises(NotificationError, match="310000"):
        DingTalkNotifier(WEBHOOK).send(rendered_report)

def test_dry_run_writes_report_without_posting(cli_deps, tmp_path):
    code = run_cli(["--config", cli_deps.config, "--dry-run", "--now", "2026-07-22T07:30:00+08:00"])
    assert code == 0
    assert (tmp_path / "reports/2026-07-22.md").exists()
    assert cli_deps.notifier.calls == 0

def test_push_failure_keeps_report_and_returns_nonzero(cli_deps, tmp_path):
    cli_deps.notifier.error = NotificationError("failed")
    code = run_cli(["--config", cli_deps.config])
    assert code == 3
    assert next((tmp_path / "reports").glob("*.md")).exists()
```

- [ ] **Step 2: 运行并确认 notifier/cli 缺失**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_report_notifier.py tests/test_pipeline.py -k 'dingtalk or dry_run or push_failure' -v`

Expected: FAIL，包含 `ModuleNotFoundError`。

- [ ] **Step 3: 实现单条 Markdown 请求与稳定退出码**

```python
class DingTalkNotifier:
    def send(self, report: RenderedReport) -> None:
        payload = {"msgtype": "markdown", "markdown": {"title": report.title, "text": report.markdown}}
        response = self.client.post(self.webhook, json=payload)
        response.raise_for_status()
        body = response.json()
        if body.get("errcode") != 0:
            raise NotificationError(f"DingTalk errcode={body.get('errcode')}: {body.get('errmsg', '')}")
```

CLI 参数固定为 `--config`、`--dry-run`、`--now`、`--log-level`。执行顺序必须是：加载配置→运行流水线并持久化状态→渲染→原子写入 `reports/YYYY-MM-DD.md`→若非 dry-run 则推送。退出码：0 成功，2 配置错误，3 推送失败，4 流水线不可恢复失败；任何退出路径都不得输出 Secret。

- [ ] **Step 4: 运行通知与 CLI 测试**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_report_notifier.py tests/test_pipeline.py -v`

Expected: 全部通过。

- [ ] **Step 5: 提交通知与命令入口**

```bash
cd 激光与商业航天情报日报
git add src/laser_space_daily/notifier.py src/laser_space_daily/cli.py tests
git commit -m "feat: add DingTalk delivery and dry run CLI"
```

### Task 9: GitHub Actions 定时运行、状态提交与安全配置

**Files:**
- Create: `激光与商业航天情报日报/.github/workflows/daily-intelligence.yml`
- Create: `激光与商业航天情报日报/README.md`
- Create: `激光与商业航天情报日报/config.yaml`
- Modify: `激光与商业航天情报日报/.gitignore`
- Modify: `激光与商业航天情报日报/config.example.yaml`

**Interfaces:**
- Consumes: `laser-space-daily` CLI 和三个 GitHub Actions Secrets
- Produces: 北京时间 07:30 定时任务、手动 dry-run、日报 artifact、状态自动提交

- [ ] **Step 1: 写工作流静态测试**

在 `tests/test_config_models.py` 增加：

```python
def test_workflow_schedule_secrets_and_dry_run():
    workflow = Path(".github/workflows/daily-intelligence.yml").read_text(encoding="utf-8")
    assert "30 23 * * *" in workflow
    assert "workflow_dispatch:" in workflow
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in workflow
    assert "TAVILY_API_KEY: ${{ secrets.TAVILY_API_KEY }}" in workflow
    assert "DINGTALK_WEBHOOK: ${{ secrets.DINGTALK_WEBHOOK }}" in workflow
    assert "concurrency:" in workflow
```

- [ ] **Step 2: 运行并确认工作流文件缺失**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_config_models.py::test_workflow_schedule_secrets_and_dry_run -v`

Expected: FAIL，包含 `FileNotFoundError`。

- [ ] **Step 3: 创建工作流和操作文档**

```yaml
name: Daily Laser and Space Intelligence
on:
  schedule:
    - cron: "30 23 * * *"
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Generate report without DingTalk delivery"
        type: boolean
        default: true
concurrency:
  group: laser-space-daily
  cancel-in-progress: false
permissions:
  contents: write
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    env:
      DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
      TAVILY_API_KEY: ${{ secrets.TAVILY_API_KEY }}
      DINGTALK_WEBHOOK: ${{ secrets.DINGTALK_WEBHOOK }}
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: actions/setup-python@v5
        with: {python-version: "3.13", cache: pip}
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest -q
      - name: Run daily pipeline
        run: laser-space-daily --config config.yaml ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run && '--dry-run' || '' }}
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: daily-intelligence-output, path: "reports/"}
      - name: Commit state and report
        if: success()
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data reports
          git diff --cached --quiet || git commit -m "data: update daily intelligence state"
          git push
```

将真实运行配置从 `config.example.yaml` 复制为 `config.yaml` 后提交，但其中不得含密钥。README 给出私有 GitHub 仓库创建、三个 Secret 配置、首次 `workflow_dispatch` dry-run、快照审核、正式推送启用以及未来用 cron + SQLite/PostgreSQL 迁移服务器的命令与数据兼容说明。

- [ ] **Step 4: 运行静态测试和 YAML 解析**

Run: `cd 激光与商业航天情报日报 && python -m pytest tests/test_config_models.py -v`

Expected: 全部通过，工作流 cron 与 Secret 名称准确。

- [ ] **Step 5: 提交自动化配置**

```bash
cd 激光与商业航天情报日报
git add .github README.md .gitignore config.example.yaml config.yaml tests
git commit -m "ci: schedule daily intelligence workflow"
```

### Task 10: 全量验证、真实 dry-run 验收与上线前检查

**Files:**
- Modify: `激光与商业航天情报日报/tests/fixtures/*`
- Modify: `激光与商业航天情报日报/tests/snapshots/daily_report.md`
- Modify: `激光与商业航天情报日报/README.md`

**Interfaces:**
- 验证所有前述接口在一条流水线上协同工作

- [ ] **Step 1: 扩充固定样本覆盖完整生命周期和四个板块**

样本集合必须包含同一项目的采购意向→招标→更正→候选人→中标、废标→二次招标分支、不同标段同名项目、激光通信关键分系统、激光反无人机系统、光电吊舱结构件、企业官方融资公告、双 B 级融资报道和单 B 级待核实案例。每个样本旁保存 `expected.json`，明确期望类别、事件阶段、项目 ID 关系、来源等级和证据原文。

- [ ] **Step 2: 运行全量单元、固定样本、模型契约和快照测试**

Run: `cd 激光与商业航天情报日报 && python -m pytest --cov=laser_space_daily --cov-report=term-missing -v`

Expected: 全部通过；核心模块 `verifier.py`、`matching.py`、`repository.py`、`report.py` 行覆盖率均不低于 90%。

- [ ] **Step 3: 执行本地真实 dry-run 并检查产物**

Run: `cd 激光与商业航天情报日报 && laser-space-daily --config config.yaml --dry-run`

Expected: 退出码 0；生成当日 `reports/YYYY-MM-DD.md`；`data/` 更新；钉钉无消息；报告无 AI 新闻，正式条目均有可点击原始来源。

- [ ] **Step 4: 重复 dry-run 验证幂等和 Secret 泄露**

Run: `cd 激光与商业航天情报日报 && laser-space-daily --config config.yaml --dry-run && git diff --check`

Expected: 第二次运行不新增重复事件/项目；`git diff --check` 退出码 0。随后运行 `git grep -n -E '(sk-[A-Za-z0-9_-]{20,}|oapi\.dingtalk\.com/robot/send\?access_token=)' -- . ':!README.md'`，Expected: 无输出、退出码 1。

- [ ] **Step 5: 在 GitHub Actions 手动触发 dry-run**

在私有仓库 Actions 页面选择 `Daily Laser and Space Intelligence`，保持 `dry_run=true`。Expected: 测试、流水线、artifact 和状态提交步骤成功；钉钉无消息；artifact 中日报与本地格式一致。

- [ ] **Step 6: 启用一次正式推送并验收钉钉显示**

手动触发时设置 `dry_run=false`。Expected: 群内仅出现一条完整 Markdown，标题和八个模块顺序正确，`[查看原始公告]`及阶段链接直接打开来源页面，24 小时变化与滚动三个月池均存在。

- [ ] **Step 7: 记录验收结果并提交最终文档**

```bash
cd 激光与商业航天情报日报
git add tests README.md
git commit -m "test: validate daily intelligence workflow end to end"
```
