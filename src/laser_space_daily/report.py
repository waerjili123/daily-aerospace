"""Deterministic DingTalk-compatible Markdown daily report rendering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from urllib.parse import quote

from .deadlines import deadline_is_current
from .models import (
    Category,
    DomainModel,
    Event,
    EventType,
    Financing,
    Project,
    VerificationStatus,
)
from .pipeline import RunResult
from .timebox import BEIJING_TIMEZONE


class RenderedReport(DomainModel):
    title: str
    markdown: str
    omitted_completed_projects: int = 0


class ReportTooLong(RuntimeError):
    """Raised when protected report content cannot fit the configured limit."""


@dataclass(frozen=True)
class _Section:
    heading: str
    lines: tuple[str, ...]
    protected: bool
    project_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ProjectEntry:
    project: Project
    latest_at: datetime
    completed: bool


_CATEGORY_LABELS = {
    Category.LASER_COMMUNICATION: "激光通信",
    Category.LASER_WEAPON: "激光武器/反无人机",
    Category.EO_TURRET: "光电转塔/吊舱",
    Category.COMMERCIAL_SPACE_FINANCING: "商业航天融资",
}

_EVENT_LABELS = {
    EventType.PROCUREMENT_INTENTION: "采购意向",
    EventType.TENDER: "招标公告",
    EventType.INQUIRY: "询价公告",
    EventType.COMPARISON: "比选公告",
    EventType.CHANGE: "变更公告",
    EventType.EXTENSION: "延期公告",
    EventType.TERMINATION: "终止公告",
    EventType.CANDIDATE: "中标候选人",
    EventType.AWARD: "中标结果",
    EventType.FAILED: "废标公告",
    EventType.REBID: "重新招标",
    EventType.FINANCING: "融资",
}

_STATUS_LABELS = {
    "upcoming": "即将启动",
    "open": "开放报名",
    "evaluating": "评审中",
    "awarded": "已中标",
    "terminated": "已终止",
    "failed": "已废标",
    "completed": "已完结",
    "closed": "已关闭",
}
_PENDING_REASON_LABELS = {
    "fetch_failed": "正文抓取失败",
    "network_failed": "网络或证书校验失败",
    "analysis_failed": "AI 分析失败",
    "validation_failed": "结构校验失败",
    "source_unavailable": "来源暂不可用",
    "verified_payload_invalid": "核验字段不完整",
    "suspected_project_match": "疑似项目匹配",
    "missing_matched_project": "匹配项目缺失",
}

_COMPLETED_STATUSES = frozenset(
    {"awarded", "terminated", "failed", "completed", "closed"}
)
_OPEN_STATUSES = frozenset({"open", "upcoming"})
_DEADLINE_LABELS = {
    "registration": "报名截止",
    "bid_submission": "投标截止",
    "opening": "开标时间",
}
_ROLLING_CATEGORIES = (
    Category.LASER_COMMUNICATION,
    Category.LASER_WEAPON,
    Category.EO_TURRET,
)


class ReportRenderer:
    """Render one stable Markdown report without lossy protected truncation."""

    def __init__(self, max_chars: int = 18000) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def render(self, result: RunResult) -> RenderedReport:
        entries = self._rolling_entries(result)
        markdown = self._render_document(result, entries, frozenset(), frozenset())
        if len(markdown) <= self.max_chars:
            return RenderedReport(
                title=self._title(result),
                markdown=markdown,
            )

        completed_ids = frozenset(
            entry.project.project_id for entry in entries if entry.completed
        )
        markdown = self._render_document(
            result, entries, completed_ids, frozenset()
        )
        if len(markdown) <= self.max_chars:
            return RenderedReport(
                title=self._title(result),
                markdown=markdown,
            )

        removed: set[str] = set()
        completed_oldest_first = sorted(
            (entry for entry in entries if entry.completed),
            key=lambda entry: (
                _datetime_key(entry.latest_at),
                entry.project.project_id,
            ),
        )
        for entry in completed_oldest_first:
            removed.add(entry.project.project_id)
            markdown = self._render_document(
                result, entries, completed_ids, frozenset(removed)
            )
            if len(markdown) <= self.max_chars:
                return RenderedReport(
                    title=self._title(result),
                    markdown=markdown,
                    omitted_completed_projects=len(removed),
                )

        raise ReportTooLong(
            f"protected report content exceeds max_chars={self.max_chars}"
        )

    def _render_document(
        self,
        result: RunResult,
        rolling_entries: tuple[_ProjectEntry, ...],
        compact_project_ids: frozenset[str],
        removed_project_ids: frozenset[str],
    ) -> str:
        event_by_id = {event.event_id: event for event in result.state.events}
        project_by_id = {
            project.project_id: project for project in result.state.projects
        }
        financing_by_id = {
            item.financing_id: item for item in result.state.financings
        }

        changed_projects = sorted(
            (
                project_by_id[project_id]
                for project_id in set(result.changed_project_ids)
                if project_id in project_by_id
            ),
            key=lambda project: (
                _datetime_key(
                    _project_latest_at(project, event_by_id)
                    or result.window_start
                ),
                project.project_id,
            ),
            reverse=True,
        )
        project_event_ids = {
            event_id
            for project in changed_projects
            for event_id in project.event_ids
        }
        changed_event_ids = set(result.changed_event_ids) - project_event_ids
        changed_events = sorted(
            (
                event_by_id[event_id]
                for event_id in changed_event_ids
                if event_id in event_by_id
            ),
            key=lambda event: (_datetime_key(event.published_at), event.event_id),
            reverse=True,
        )
        changed_financings = sorted(
            (
                financing_by_id[financing_id]
                for financing_id in set(result.changed_financing_ids)
                if financing_id in financing_by_id
            ),
            key=lambda item: (_datetime_key(item.announced_at), item.financing_id),
            reverse=True,
        )
        daily_lines = (
            tuple(
                line
                for project in changed_projects
                for line in _format_project(project, event_by_id, compact=False)
            )
            + tuple(_format_event(event) for event in changed_events)
            + tuple(_format_financing(item) for item in changed_financings)
        )

        open_projects = sorted(
            (
                project
                for project in result.state.projects
                if project.status in _OPEN_STATUSES
                and _actionable_deadline(project, result.window_end) is not None
            ),
            key=_open_project_sort_key,
        )
        open_lines = tuple(
            line
            for project in open_projects
            for line in _format_project(project, event_by_id, compact=False)
        )

        sections: list[_Section] = [
            _Section(
                heading="过去24小时新增/变化",
                lines=daily_lines,
                protected=True,
                project_ids=tuple(
                    project.project_id for project in changed_projects
                ),
            ),
            _Section(
                heading="当前可报名及即将启动",
                lines=open_lines,
                protected=True,
                project_ids=tuple(project.project_id for project in open_projects),
            ),
        ]

        for category in _ROLLING_CATEGORIES:
            all_category_entries = sorted(
                (
                    entry
                    for entry in rolling_entries
                    if entry.project.category is category
                ),
                key=lambda entry: entry.project.project_id,
            )
            category_entries = tuple(
                entry
                for entry in all_category_entries
                if entry.project.project_id not in removed_project_ids
            )
            lines = tuple(
                line
                for entry in category_entries
                for line in _format_project(
                    entry.project,
                    event_by_id,
                    compact=entry.project.project_id in compact_project_ids,
                )
            )
            sections.append(
                _Section(
                    heading=_CATEGORY_LABELS[category],
                    lines=lines,
                    protected=False,
                    project_ids=tuple(
                        entry.project.project_id for entry in all_category_entries
                    ),
                )
            )

        rolling_financings = sorted(
            (
                item
                for item in result.state.financings
                if item.verification_status is VerificationStatus.VERIFIED
                and _in_window(
                    _financing_latest_at(item),
                    result.rolling_start,
                    result.window_end,
                )
            ),
            key=lambda item: (
                _datetime_key(_financing_latest_at(item)),
                item.financing_id,
            ),
            reverse=True,
        )
        sections.append(
            _Section(
                heading="商业航天融资",
                lines=tuple(_format_financing(item) for item in rolling_financings),
                protected=True,
            )
        )
        sections.append(
            _Section(
                heading="今日重点跟进",
                lines=_followup_lines(result, event_by_id),
                protected=True,
            )
        )
        sections.append(
            _Section(
                heading="三个月趋势与数据完整性",
                lines=_trend_lines(result),
                protected=True,
            )
        )

        compressible_project_ids = {
            project_id
            for section in sections
            if not section.protected
            for project_id in section.project_ids
        }
        transformed_project_ids = set(compact_project_ids) | set(
            removed_project_ids
        )
        if not transformed_project_ids <= compressible_project_ids:
            raise AssertionError(
                "compression candidate must belong to an unprotected rolling section"
            )

        opening = [self._title(result), self._window_line(result)]
        if removed_project_ids:
            opening.extend(
                [
                    "",
                    f"压缩说明：已压缩 {len(removed_project_ids)} 个已完结历史项目",
                ]
            )
        blocks = ["\n".join(opening)]
        for section in sections:
            lines = section.lines or ("- 暂无已核实信息",)
            blocks.append(f"## {section.heading}\n" + "\n".join(lines))
        return "\n\n".join(blocks) + "\n"

    @staticmethod
    def _title(result: RunResult) -> str:
        report_date = _as_beijing(result.window_end).date().isoformat()
        return f"# 中国激光与商业航天情报日报｜{report_date}"

    @staticmethod
    def _window_line(result: RunResult) -> str:
        start = _format_datetime(result.window_start)
        end = _format_datetime(result.window_end)
        rolling = _as_beijing(result.rolling_start).date().isoformat()
        report_date = _as_beijing(result.window_end).date().isoformat()
        return (
            f"时窗：北京时间 {start}—{end}；滚动池 {rolling}—{report_date}；"
            f"覆盖：{_coverage_status_text(result)}"
        )

    @staticmethod
    def _rolling_entries(result: RunResult) -> tuple[_ProjectEntry, ...]:
        event_by_id = {event.event_id: event for event in result.state.events}
        entries: list[_ProjectEntry] = []
        for project in result.state.projects:
            latest = _project_latest_at(project, event_by_id)
            if latest is None or not _in_window(
                latest, result.rolling_start, result.window_end
            ):
                continue
            completed = (
                project.status in _COMPLETED_STATUSES
                or project.current_stage
                in {EventType.AWARD, EventType.TERMINATION, EventType.FAILED}
            )
            entries.append(
                _ProjectEntry(
                    project=project,
                    latest_at=latest,
                    completed=completed,
                )
            )
        return tuple(entries)


def _format_event(event: Event) -> str:
    parts = [
        f"- {_format_date(event.published_at)}",
        _CATEGORY_LABELS[event.category],
        _EVENT_LABELS[event.event_type],
        _safe_text(event.title),
        f"采购单位：{_safe_text(event.organization)}",
    ]
    if event.analysis and event.analysis.amount:
        parts.append(f"金额：{_safe_text(event.analysis.amount)}")
    parts.append(_link("查看原始公告", event.source_url))
    return "｜".join(parts)


def _format_project(
    project: Project,
    event_by_id: dict[str, Event],
    *,
    compact: bool,
) -> tuple[str, ...]:
    latest = _latest_project_event(project, event_by_id)
    latest_at = _project_latest_at(project, event_by_id)
    latest_url = project.latest_source_url or (latest.source_url if latest else None)
    date_text = _format_date(latest_at) if latest_at else "日期未记录"
    status = _STATUS_LABELS.get(project.status, _safe_text(project.status))
    if compact:
        parts = [f"- {date_text}", _safe_text(project.name), f"状态：{status}"]
    else:
        parts = [
            f"- {date_text}",
            _CATEGORY_LABELS[project.category],
            _safe_text(project.name),
            f"采购单位：{_safe_text(project.organization)}",
            f"状态：{status}",
        ]
        if project.amount:
            parts.append(f"金额：{_safe_text(project.amount)}")
        deadline_text = _deadline_text(project)
        if deadline_text:
            parts.append(f"截止：{deadline_text}")
    if latest_url:
        parts.append(_link("查看原始公告", latest_url))
    first_line = "｜".join(parts)
    if compact:
        return (first_line,)

    chain = sorted(
        (
            event_by_id[event_id]
            for event_id in set(project.event_ids)
            if event_id in event_by_id and event_by_id[event_id].source_url
        ),
        key=lambda event: (_datetime_key(event.published_at), event.event_id),
    )
    if not chain:
        return (first_line,)
    chain_text = " → ".join(
        f"{_link(_EVENT_LABELS[event.event_type], event.source_url)}"
        f"（{_format_date(event.published_at)}）"
        for event in chain
    )
    return first_line, f"  - 公告链：{chain_text}"


def _format_financing(item: Financing) -> str:
    subtype_labels = {
        "strategic": "战略融资",
        "capital_increase": "产业基金增资",
        "merger_acquisition": "并购融资",
    }
    parts = [
        f"- {_format_date(item.announced_at)}",
        _CATEGORY_LABELS[Category.COMMERCIAL_SPACE_FINANCING],
        _safe_text(item.company),
        (
            _safe_text(item.round_name)
            if item.round_name
            else subtype_labels.get(item.financing_subtype, "轮次未披露")
        ),
        f"金额：{_financing_amount(item)}",
        (
            f"投资方：{'、'.join(_safe_text(value) for value in sorted(item.investors))}"
            if item.investors
            else "投资方：未披露"
        ),
        (
            f"领域：{_safe_text(item.business_area)}"
            if item.business_area
            else "领域：未披露"
        ),
    ]
    urls = sorted({item.source_url, *item.source_urls})
    source_links = [_link(_source_label(item, url), url) for url in urls]
    if source_links:
        parts.append(" / ".join(source_links))
    return "｜".join(parts)


def _source_label(item: Financing, url: str) -> str:
    matching = [
        evidence
        for evidence in item.evidence
        if evidence.source_url == url
    ]
    metadata = " ".join(evidence.field for evidence in matching).lower()
    if "企业公告" in metadata or "company announcement" in metadata:
        return "企业公告"
    if "投资方公告" in metadata or "investor announcement" in metadata:
        return "投资方公告"
    if (
        "权威媒体" in metadata
        or "authoritative media" in metadata
        or "official media" in metadata
    ):
        return "权威媒体报道"
    return "来源"


def _financing_amount(item: Financing) -> str:
    if not item.amount_disclosed or item.amount_cny is None:
        return "未披露"
    amount = item.amount_cny
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.2f}亿元"
    if amount >= 10_000:
        return f"{amount / 10_000:.2f}万元"
    return f"{amount:.2f}元"


def _financing_latest_at(item: Financing) -> datetime:
    return max(
        [item.announced_at, *item.source_published_at.values()],
        key=_datetime_key,
    )


def _followup_lines(
    result: RunResult, event_by_id: dict[str, Event]
) -> tuple[str, ...]:
    deadline_rows: list[tuple[datetime, str, Project]] = []
    for project in result.state.projects:
        if project.status not in _OPEN_STATUSES:
            continue
        for label, deadline in _supported_deadlines(project).items():
            if not deadline_is_current(
                deadline, project.deadline_precision[label], result.window_end
            ):
                continue
            deadline_rows.append((deadline, label, project))
    deadline_rows.sort(
        key=lambda row: (_datetime_key(row[0]), row[1], row[2].project_id)
    )
    lines: list[str] = []
    for deadline, _, project in deadline_rows:
        latest = _latest_project_event(project, event_by_id)
        latest_url = project.latest_source_url or (latest.source_url if latest else None)
        parts = [
            f"- 截止 {_format_datetime(deadline)}",
            _safe_text(project.name),
            f"状态：{_STATUS_LABELS.get(project.status, _safe_text(project.status))}",
        ]
        if latest_url:
            parts.append(_link("查看原始公告", latest_url))
        lines.append("｜".join(parts))
    evaluating = sum(
        project.status == "evaluating" for project in result.state.projects
    )
    suspected = sum(
        item.reason == "suspected_project_match" for item in result.state.pending
    )
    lines.append(f"- 评估中项目：{evaluating}；疑似匹配待核实：{suspected}")
    current_pending = sorted(
        (
            item
            for item in result.state.pending
            if result.window_start
            <= _as_beijing(item.discovered_at)
            <= result.window_end
        ),
        key=lambda item: _pending_sort_key(item, result.window_end),
    )
    for item in current_pending[:10]:
        reason = _PENDING_REASON_LABELS.get(item.reason, _safe_text(item.reason))
        category = (
            _CATEGORY_LABELS.get(item.category_hint, "板块未确定")
            if item.category_hint is not None
            else "板块未确定"
        )
        source_date = (
            _format_date(item.source_published_at)
            if item.source_published_at is not None
            else "发布日期未知"
        )
        time_label = _pending_time_label(item.source_published_at, result.window_end)
        lines.append(
            "｜".join(
                (
                    "- 待核实候选",
                    category,
                    source_date,
                    time_label,
                    _safe_text(item.title),
                    f"原因：{reason}",
                    _link("查看原始来源", item.source_url),
                )
            )
        )
        if item.summary.strip():
            lines.append(
                f"  - 搜索摘要（未核实）：{_safe_text(item.summary[:240])}"
            )
    if len(current_pending) > 10:
        lines.append(f"- 另有 {len(current_pending) - 10} 条待核实候选未展开")
    surfaced_urls = {
        item.source_url for item in result.state.pending
    } | {
        item.source_url for item in result.state.events
    } | {
        url
        for item in result.state.financings
        for url in (item.source_urls or [item.source_url])
    }
    discovery_candidates = [
        item
        for item in result.discovery_candidates
        if item.url not in surfaced_urls
    ]
    for item in discovery_candidates[:10]:
        category = (
            _CATEGORY_LABELS.get(item.category_hint, "板块未确定")
            if item.category_hint is not None
            else "板块未确定"
        )
        source_date = (
            _format_date(item.source_published_at)
            if item.source_published_at is not None
            else "发布日期未知"
        )
        time_label = _pending_time_label(item.source_published_at, result.window_end)
        lines.append(
            "｜".join(
                (
                    "- 搜索候选（未核实）",
                    category,
                    source_date,
                    time_label,
                    _safe_text(item.title),
                    _link("查看原始来源", item.url),
                )
            )
        )
        if item.summary.strip():
            lines.append(
                f"  - 搜索摘要（未核实）：{_safe_text(item.summary[:240])}"
            )
    if len(discovery_candidates) > 10:
        lines.append(f"- 另有 {len(discovery_candidates) - 10} 条搜索候选未展开")
    return tuple(lines)


def _trend_lines(result: RunResult) -> tuple[str, ...]:
    counts = result.trend_summary.category_counts
    count_text = "；".join(
        f"{_CATEGORY_LABELS[category]} {counts.get(category, 0)}"
        for category in (
            Category.LASER_COMMUNICATION,
            Category.LASER_WEAPON,
            Category.EO_TURRET,
            Category.COMMERCIAL_SPACE_FINANCING,
        )
    )
    metrics = result.metrics
    availability = (
        f"- 信息可用：最终候选 {metrics.final_candidate_count} 条"
        if metrics.information_available
        else (
            f"- 信息不足：最终候选 {metrics.final_candidate_count} 条，"
            "未达到 5 条验收门槛"
        )
    )
    return (
        f"- {_safe_text(result.trend_summary.summary)}",
        f"- 分类计数：{count_text}",
        (
            f"- 采集漏斗：博查原始 {metrics.raw_search_count}；"
            f"结构有效 {metrics.valid_shape_count}；"
            f"主题相关 {metrics.relevance_pass_count}；"
            f"近 7 天 {metrics.recent_7d_count}；"
            f"8–30 天补充 {metrics.fallback_8_30d_count}；"
            f"日期未知补充 {metrics.unknown_date_count}；"
            f"最终候选 {metrics.final_candidate_count}；"
            f"正文抓取失败 {metrics.fetch_failure_count}"
        ),
        availability,
        (
            f"- 数据完整性：候选 {metrics.candidate_count}；"
            f"已核实 {metrics.verified_count}；"
            f"待核实 {metrics.pending_count}；已去重 {metrics.deduplicated_count}；"
            f"失败域 {len(metrics.failed_domains)}"
        ),
        f"- 覆盖：{_coverage_text(result)}",
    )


def _pending_time_label(
    source_published_at: datetime | None, window_end: datetime
) -> str:
    if source_published_at is None:
        return "日期未知"
    age = _as_beijing(window_end) - _as_beijing(source_published_at)
    if age.days <= 7:
        return "近 7 天"
    if age.days <= 30:
        return "8–30 天补充"
    return "时间范围外"


def _pending_sort_key(item: object, window_end: datetime) -> tuple[object, ...]:
    source_published_at = getattr(item, "source_published_at", None)
    label = _pending_time_label(source_published_at, window_end)
    bucket = {"近 7 天": 0, "8–30 天补充": 1, "日期未知": 2}.get(label, 3)
    published_rank = (
        -_as_beijing(source_published_at).timestamp()
        if source_published_at is not None
        else 0
    )
    return (bucket, published_rank, getattr(item, "item_id", ""))


def _coverage_text(result: RunResult) -> str:
    metrics = result.metrics
    degraded: list[str] = []
    if metrics.search_coverage_degraded:
        search_details = [
            _search_failure_text(reason)
            for reason in dict.fromkeys(metrics.search_failure_reasons)
        ]
        if metrics.failed_domains:
            failed_domains = "、".join(
                _safe_text(domain) for domain in sorted(set(metrics.failed_domains))
            )
            search_details.append(f"官方来源访问失败：{failed_domains}")
        degraded.append(
            f"搜索（{'；'.join(search_details)}）" if search_details else "搜索"
        )
    if metrics.model_coverage_degraded or result.trend_summary.degraded:
        degraded.append("AI")
    return f"降级（{'、'.join(degraded)}）" if degraded else "正常"


def _coverage_status_text(result: RunResult) -> str:
    metrics = result.metrics
    degraded = (
        metrics.search_coverage_degraded
        or metrics.model_coverage_degraded
        or result.trend_summary.degraded
    )
    return "降级" if degraded else "正常"


_SEARCH_FAILURE_TEXT = {
    "authentication": "博查 API 认证失败",
    "quota_or_rate_limit": "博查 API 配额不足或触发限流",
    "network_or_timeout": "博查 API 网络连接或请求超时",
    "server_error": "博查 API 服务端异常",
    "request_rejected": "博查 API 请求被拒绝",
    "invalid_response": "博查 API 返回格式异常",
}


def _search_failure_text(reason: str) -> str:
    """Render only allow-listed, secret-safe search failure categories."""
    return _SEARCH_FAILURE_TEXT.get(reason, "博查 API 调用失败")


def _open_project_sort_key(project: Project) -> tuple[object, ...]:
    deadlines = sorted(_supported_deadlines(project).values(), key=_datetime_key)
    next_deadline = deadlines[0] if deadlines else None
    return (
        0 if next_deadline else 1,
        _datetime_key(next_deadline) if next_deadline else (9999, 12, 31, 23, 59, 59, 0),
        project.project_id,
    )


def _deadline_text(project: Project) -> str:
    return "、".join(
        f"{_DEADLINE_LABELS.get(label, _safe_text(label))} {_format_datetime(deadline)}"
        for label, deadline in sorted(
            _supported_deadlines(project).items(),
            key=lambda item: (_datetime_key(item[1]), item[0]),
        )
    )


def _supported_deadlines(project: Project) -> dict[str, datetime]:
    return {
        name: deadline
        for name, deadline in project.deadlines.items()
        if name in _DEADLINE_LABELS
        and name in project.deadline_evidence
        and name in project.deadline_precision
    }


def _actionable_deadline(project: Project, now: datetime) -> datetime | None:
    candidates = [
        deadline
        for name, deadline in _supported_deadlines(project).items()
        if name in {"registration", "bid_submission"}
        and deadline_is_current(deadline, project.deadline_precision[name], now)
    ]
    return min(candidates, key=_datetime_key, default=None)


def _latest_project_event(
    project: Project | None, event_by_id: dict[str, Event]
) -> Event | None:
    if project is None:
        return None
    return max(
        (
            event_by_id[event_id]
            for event_id in project.event_ids
            if event_id in event_by_id
        ),
        key=lambda event: (_datetime_key(event.published_at), event.event_id),
        default=None,
    )


def _project_latest_at(
    project: Project, event_by_id: dict[str, Event]
) -> datetime | None:
    latest = _latest_project_event(project, event_by_id)
    candidates = [
        value
        for value in (
            project.latest_event_at,
            latest.published_at if latest else None,
        )
        if value is not None
    ]
    return max(candidates, key=_datetime_key, default=None)


def _in_window(value: datetime, start: datetime, end: datetime) -> bool:
    key = _datetime_key(value)
    return _datetime_key(start) <= key <= _datetime_key(end)


def _format_date(value: datetime) -> str:
    return _as_beijing(value).strftime("%Y-%m-%d")


def _format_datetime(value: datetime) -> str:
    return _as_beijing(value).strftime("%Y-%m-%d %H:%M")


def _datetime_key(value: datetime) -> tuple[int, int, int, int, int, int, int]:
    beijing = _as_beijing(value)
    return (
        beijing.year,
        beijing.month,
        beijing.day,
        beijing.hour,
        beijing.minute,
        beijing.second,
        beijing.microsecond,
    )


def _as_beijing(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BEIJING_TIMEZONE)


def _safe_text(value: object) -> str:
    text = str(value).replace("\\", "\\\\")
    text = re.sub(r"(?m)^(\s*)([#\-+])", r"\1\\\2", text)
    text = re.sub(r"(?m)^(\s*)(\d+)\.(?=\s)", r"\1\2\\.", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for character in ("`", "*", "_", "[", "]", "|", "~"):
        text = text.replace(character, f"\\{character}")
    return " ".join(text.split())


def _link(label: str, url: str) -> str:
    safe_url = quote(url.strip(), safe="/:?#[]@!$&'*,;=+%")
    return f"[{_safe_text(label)}]({safe_url})"
