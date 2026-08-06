"""Deterministic DingTalk-compatible Markdown daily report rendering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from urllib.parse import quote, urlsplit

from .deadlines import deadline_is_current
from .matching import financing_round_identities, organization_identity
from .models import (
    Category,
    DomainModel,
    Event,
    EventType,
    Financing,
    Project,
    SourceGrade,
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


@dataclass(frozen=True)
class _ShortSignal:
    category: Category
    label: str
    title: str
    organization: str
    published_at: datetime | None
    amount: str
    round_name: str
    summary: str
    source_urls: tuple[str, ...]
    identity: str
    event_type: EventType | None = None
    registration_deadline: datetime | None = None
    bid_submission_deadline: datetime | None = None
    opening_deadline: datetime | None = None
    deadline_precision: dict[str, str] | None = None
    deadline_evidence_fields: tuple[str, ...] = ()
    awarded_supplier: str = ""
    awarded_amount: str = ""


@dataclass(frozen=True)
class _ShortItem:
    lines: tuple[str, ...]
    category: Category
    status: str
    strict: bool
    daily: bool
    historical: bool
    followup: str | None = None
    procurement_group: str | None = None
    sort_at: datetime | None = None


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
    "classification_country_evidence_invalid": "境内主体证据不足",
    "classification_category_evidence_invalid": "目标业务证据不足",
    "classification_event_evidence_invalid": "事件动作证据不足",
    "classification_scope_evidence_invalid": "范围证据不足",
    "financing_requires_official_or_two_independent_b_sources": (
        "缺少官方来源或第二个独立 B 级来源"
    ),
    "financing_requires_independent_sources": "缺少独立来源互证",
    "financing_corroboration_insufficient": "融资来源互证不足",
    "financing_corroboration_conflict": "融资来源信息冲突",
    "financing_corroboration_date_outside_window": "融资来源时间距离过远",
    "financing_corroboration_round_conflict": "融资来源轮次冲突",
    "financing_corroboration_amount_conflict": "融资来源金额冲突",
    "financing_corroboration_attribute_conflict": "融资来源关键属性冲突",
    "financing_source_event_evidence_missing": "单一来源的融资事件证据不足",
    "financing_source_organization_evidence_invalid": "融资主体证据不匹配",
    "financing_source_publication_date_evidence_invalid": "来源发布日期证据不匹配",
    "financing_source_round_evidence_invalid": "融资轮次证据不匹配",
    "financing_source_subtype_evidence_invalid": "融资类型证据不匹配",
    "financing_source_amount_evidence_invalid": "融资金额证据不匹配",
    "financing_source_investor_evidence_invalid": "投资方证据不匹配",
    "financing_source_business_area_evidence_invalid": "业务方向证据不匹配",
    "financing_source_action_evidence_invalid": "融资动作证据不匹配",
    "financing_missing_required_evidence": "融资关键证据不足",
    "missing_required_fields:organization": "核验字段缺失（主体）",
    "missing_required_fields:published_at": "核验字段缺失（发布日期）",
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
        current_run_groups = (
            tuple(
                _format_project(project, event_by_id, compact=False)
                for project in changed_projects
            )
            + tuple((_format_event(event),) for event in changed_events)
            + tuple((_format_financing(item),) for item in changed_financings)
        )
        daily_projects = tuple(
            project
            for project in changed_projects
            if (
                (latest_at := _project_latest_at(project, event_by_id))
                is not None
                and _in_window(
                    latest_at,
                    result.window_start,
                    result.window_end,
                )
            )
        )
        daily_events = tuple(
            event
            for event in changed_events
            if _in_window(
                event.published_at,
                result.window_start,
                result.window_end,
            )
        )
        daily_financings = tuple(
            item
            for item in changed_financings
            if _in_window(
                _financing_latest_at(item),
                result.window_start,
                result.window_end,
            )
        )
        daily_lines = (
            tuple(
                line
                for project in daily_projects
                for line in _format_project(project, event_by_id, compact=False)
            )
            + tuple(_format_event(event) for event in daily_events)
            + tuple(_format_financing(item) for item in daily_financings)
        )
        backfill_project_ids = {
            project.project_id for project in changed_projects
        } - {
            project.project_id for project in daily_projects
        }
        backfill_event_ids = {
            event.event_id for event in changed_events
        } - {
            event.event_id for event in daily_events
        }
        backfill_financing_ids = {
            item.financing_id for item in changed_financings
        } - {
            item.financing_id for item in daily_financings
        }
        backfill_lines = (
            tuple(
                line
                for project in changed_projects
                if project.project_id in backfill_project_ids
                for line in _format_project(project, event_by_id, compact=False)
            )
            + tuple(
                _format_event(event)
                for event in changed_events
                if event.event_id in backfill_event_ids
            )
            + tuple(
                _format_financing(item)
                for item in changed_financings
                if item.financing_id in backfill_financing_ids
            )
        )
        top_lines = _top_signal_lines(result, current_run_groups)

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
                heading="今日最值得看",
                lines=top_lines,
                protected=True,
            ),
            _Section(
                heading="过去24小时新增/变化",
                lines=daily_lines,
                protected=True,
                project_ids=tuple(
                    project.project_id for project in daily_projects
                ),
            ),
            _Section(
                heading="本轮新核实/历史补录",
                lines=backfill_lines,
                protected=True,
                project_ids=tuple(sorted(backfill_project_ids)),
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
            candidate_lines = _category_candidate_lines(result, category)
            lines = (*lines, *candidate_lines)
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
                lines=_formal_and_candidate_lines(
                    tuple(_format_financing(item) for item in rolling_financings),
                    _category_candidate_lines(
                        result, Category.COMMERCIAL_SPACE_FINANCING
                    ),
                ),
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
        sections = _deduplicate_section_signals(sections)

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


class DingTalkShortReportRenderer:
    """Render the business-only report sent to DingTalk and stored as the report."""

    def __init__(self, max_chars: int = 18000) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def render(self, result: RunResult) -> RenderedReport:
        attempts = (
            (3, 80, True),
            (2, 60, True),
            (1, 40, False),
        )
        for max_items, summary_limit, include_followups in attempts:
            markdown = _render_short_document(
                result,
                max_items=max_items,
                summary_limit=summary_limit,
                include_followups=include_followups,
            )
            if len(markdown) <= self.max_chars:
                return RenderedReport(
                    title=ReportRenderer._title(result),
                    markdown=markdown,
                )
        raise ReportTooLong(
            f"protected short report content exceeds max_chars={self.max_chars}"
        )


def _render_short_document(
    result: RunResult,
    *,
    max_items: int,
    summary_limit: int,
    include_followups: bool,
) -> str:
    candidate_signals = _short_candidate_signals(result)
    financing_candidates = [
        signal
        for signal in candidate_signals
        if signal.category is Category.COMMERCIAL_SPACE_FINANCING
    ]
    procurement_candidates = [
        signal
        for signal in candidate_signals
        if signal.category in _ROLLING_CATEGORIES
    ]

    financing_items = [
        *_short_verified_financings(result),
        *(
            _short_signal_item(signal, summary_limit=summary_limit)
            for signal in financing_candidates
        ),
    ][:max_items]
    procurement_pool = [
        *_short_verified_procurements(result),
        *(
            _short_signal_item(signal, summary_limit=summary_limit)
            for signal in procurement_candidates
        ),
    ]
    procurement_items = _select_procurement_items(
        procurement_pool,
        max_items=max_items,
    )

    displayed = [*financing_items, *procurement_items]
    daily_financing = sum(
        item.strict and item.daily for item in financing_items
    )
    daily_procurement = sum(
        item.strict and item.daily for item in procurement_items
    )
    historical = sum(item.strict and item.historical for item in displayed)
    pending = sum(not item.strict for item in displayed)

    title = ReportRenderer._title(result)
    end_text = _format_datetime(result.window_end)
    blocks = [
        "\n".join(
            (
                title,
                f"- 统计截至：北京时间 {end_text}",
                (
                    f"- 概览：过去24小时融资新增 {daily_financing} 条；"
                    f"招标变化 {daily_procurement} 条；历史补录 {historical} 条；"
                    f"待核实线索 {pending} 条。"
                ),
            )
        ),
        _short_section(
            "一、商业航天融资新闻",
            financing_items,
            empty_text="过去24小时及滚动池内暂无可展示的融资信息。",
            statistics_label="融资统计",
        ),
        _short_procurement_section(procurement_items),
        _short_other_dynamics(procurement_items),
    ]
    if include_followups:
        followups = _short_followups(
            result,
            financing_items=financing_items,
            procurement_items=procurement_items,
        )
        if followups:
            blocks.append("## 四、重点跟进\n" + "\n".join(followups))
    blocks.append(_short_system_status(result, displayed))
    return "\n\n".join(blocks) + "\n"


def _short_verified_financings(result: RunResult) -> list[_ShortItem]:
    changed_ids = set(result.changed_financing_ids)
    rows = sorted(
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
            0 if item.financing_id in changed_ids else 1,
            -_as_beijing(_financing_latest_at(item)).timestamp(),
            item.financing_id,
        ),
    )
    unique_rows: list[Financing] = []
    for item in rows:
        if any(
            _same_verified_financing_display_event(existing, item)
            for existing in unique_rows
        ):
            continue
        unique_rows.append(item)
    items: list[_ShortItem] = []
    for item in unique_rows:
        latest_at = _financing_latest_at(item)
        daily = _in_window(
            latest_at,
            result.window_start,
            result.window_end,
        )
        historical = not daily and item.financing_id in changed_ids
        if daily:
            label = "已核实·今日新增"
        elif historical:
            label = "已核实·历史补录"
        else:
            label = "已核实·滚动池"
        round_text = _safe_text(
            item.round_name
            or {
                "strategic": "战略融资",
                "capital_increase": "产业基金增资",
                "merger_acquisition": "并购融资",
            }.get(item.financing_subtype, "融资")
        )
        headline = f"- **【{label}】{_safe_text(item.company)}完成{round_text}**"
        details = [
            f"企业：{_safe_text(item.company)}",
            f"时间：{_format_date(item.announced_at)}",
            f"轮次：{round_text}",
        ]
        amount_text = _financing_amount(item)
        details.append(f"金额：{amount_text or '未明确披露'}")
        if item.investors:
            details.append(
                "投资方："
                + "、".join(
                    _safe_text(value) for value in sorted(item.investors)
                )
            )
        if item.business_area:
            details.append(f"业务方向：{_safe_text(item.business_area)}")
        summary = item.brief_summary or _structured_financing_summary(item)
        details.append(f"摘要：{_short_summary(summary, 120)}")
        lines = [headline, *(f"  - {detail}" for detail in details)]
        source_links = _short_financing_source_links(item)
        if source_links:
            lines.append("  - 来源：" + "｜".join(source_links))
        items.append(
            _ShortItem(
                lines=tuple(lines),
                category=Category.COMMERCIAL_SPACE_FINANCING,
                status="strict",
                strict=True,
                daily=daily,
                historical=historical,
            )
        )
    return items


def _same_verified_financing_display_event(
    left: Financing,
    right: Financing,
) -> bool:
    if organization_identity(left.company) != organization_identity(right.company):
        return False
    left_rounds = _round_identities(left.round_name or "")
    right_rounds = _round_identities(right.round_name or "")
    if (
        not left_rounds
        or not right_rounds
        or (
            not left_rounds.issubset(right_rounds)
            and not right_rounds.issubset(left_rounds)
        )
    ):
        return False
    if abs((left.announced_at.date() - right.announced_at.date()).days) > 45:
        return False
    left_sources = {left.source_url, *left.source_urls}
    right_sources = {right.source_url, *right.source_urls}
    return bool(left_sources & right_sources)


def _short_verified_procurements(result: RunResult) -> list[_ShortItem]:
    event_by_id = {event.event_id: event for event in result.state.events}
    changed_project_ids = set(result.changed_project_ids)
    changed_event_ids = set(result.changed_event_ids)
    rows: list[tuple[datetime, str, Project | Event]] = []
    covered_event_ids: set[str] = set()

    for project in result.state.projects:
        if project.category not in _ROLLING_CATEGORIES:
            continue
        latest = _latest_project_event(project, event_by_id)
        latest_at = _project_latest_at(project, event_by_id)
        if (
            latest is None
            or latest_at is None
            or latest.verification_status is not VerificationStatus.VERIFIED
            or not _in_window(latest_at, result.rolling_start, result.window_end)
        ):
            continue
        rows.append((latest_at, project.project_id, project))
        covered_event_ids.update(project.event_ids)

    for event in result.state.events:
        if (
            event.event_id in covered_event_ids
            or event.category not in _ROLLING_CATEGORIES
            or event.verification_status is not VerificationStatus.VERIFIED
            or not event.formal_record
            or not _in_window(
                event.published_at,
                result.rolling_start,
                result.window_end,
            )
        ):
            continue
        rows.append((event.published_at, event.event_id, event))

    rows.sort(
        key=lambda row: (
            0
            if (
                isinstance(row[2], Project)
                and row[2].project_id in changed_project_ids
            )
            or (
                isinstance(row[2], Event)
                and row[2].event_id in changed_event_ids
            )
            else 1,
            -_as_beijing(row[0]).timestamp(),
            row[1],
        )
    )
    items: list[_ShortItem] = []
    for published_at, _, value in rows:
        daily = _in_window(
            published_at,
            result.window_start,
            result.window_end,
        )
        changed = (
            value.project_id in changed_project_ids
            if isinstance(value, Project)
            else value.event_id in changed_event_ids
        )
        historical = not daily and changed
        if daily:
            label = "已核实·今日新增"
        elif historical:
            label = "已核实·历史补录"
        else:
            label = "已核实·滚动池"
        if isinstance(value, Project):
            latest = _latest_project_event(value, event_by_id)
            source_url = value.latest_source_url or (
                latest.source_url if latest is not None else ""
            )
            event_type = latest.event_type if latest is not None else value.current_stage
            group, bid_deadline = _verified_project_procurement_group(
                value,
                event_type=event_type,
                now=result.window_end,
            )
            status_text = _STATUS_LABELS.get(
                value.status,
                _safe_text(value.status),
            )
            lines = _procurement_item_lines(
                title=value.name,
                summary=(
                    latest.analysis.brief_summary
                    if latest is not None and latest.analysis is not None
                    else ""
                ),
                organization=value.organization,
                category=value.category,
                group=group,
                status_text=status_text,
                published_at=published_at,
                bid_deadline=bid_deadline,
                amount=value.amount or "",
                awarded_supplier=(
                    latest.analysis.awarded_supplier
                    if latest is not None and latest.analysis is not None
                    else None
                ),
                awarded_amount=(
                    latest.analysis.awarded_amount
                    if latest is not None and latest.analysis is not None
                    else None
                ),
                trust_label=label,
                source_url=source_url,
                event_type=event_type,
                source_is_aggregator=False,
                now=result.window_end,
                deadline_supported=(
                    "bid_submission" in value.deadline_evidence
                ),
            )
            category = value.category
        else:
            source_url = value.source_url
            group, bid_deadline = _verified_event_procurement_group(
                value,
                now=result.window_end,
            )
            lines = _procurement_item_lines(
                title=value.title,
                summary=(
                    value.analysis.brief_summary
                    if value.analysis is not None
                    else ""
                ),
                organization=value.organization,
                category=value.category,
                group=group,
                status_text=_EVENT_LABELS[value.event_type],
                published_at=value.published_at,
                bid_deadline=bid_deadline,
                amount=(
                    value.analysis.amount
                    if value.analysis is not None and value.analysis.amount
                    else ""
                ),
                awarded_supplier=(
                    value.analysis.awarded_supplier
                    if value.analysis is not None
                    else None
                ),
                awarded_amount=(
                    value.analysis.awarded_amount
                    if value.analysis is not None
                    else None
                ),
                trust_label=label,
                source_url=source_url,
                event_type=value.event_type,
                source_is_aggregator=False,
                now=result.window_end,
                deadline_supported=(
                    value.analysis is not None
                    and any(
                        evidence.field == "bid_submission_deadline"
                        for evidence in value.analysis.evidence
                    )
                ),
            )
            category = value.category
        items.append(
            _ShortItem(
                lines=tuple(lines),
                category=category,
                status="strict",
                strict=True,
                daily=daily,
                historical=historical,
                procurement_group=group,
                sort_at=bid_deadline or published_at,
            )
        )
    return items


_PROCUREMENT_ACTIONABLE_TYPES = {
    EventType.TENDER,
    EventType.INQUIRY,
    EventType.COMPARISON,
    EventType.REBID,
    EventType.CHANGE,
    EventType.EXTENSION,
}
_PROCUREMENT_RESULT_TYPES = {
    EventType.PROCUREMENT_INTENTION,
    EventType.CANDIDATE,
    EventType.AWARD,
    EventType.FAILED,
    EventType.TERMINATION,
}


def _verified_project_procurement_group(
    project: Project,
    *,
    event_type: EventType | None,
    now: datetime,
) -> tuple[str, datetime | None]:
    deadline = project.deadlines.get("bid_submission")
    precision = project.deadline_precision.get("bid_submission")
    evidence = project.deadline_evidence.get("bid_submission")
    if event_type in _PROCUREMENT_RESULT_TYPES or project.status in {
        "evaluating",
        "awarded",
        "terminated",
        "failed",
        "completed",
        "closed",
    }:
        return "results", deadline
    if deadline is not None and precision is not None:
        if not deadline_is_current(deadline, precision, now):
            return "results", deadline
        if evidence is not None and event_type in _PROCUREMENT_ACTIONABLE_TYPES:
            return "opportunities", deadline
    if event_type in _PROCUREMENT_ACTIONABLE_TYPES or project.status in {
        "upcoming",
        "open",
    }:
        return "confirmation", deadline
    return "results", deadline


def _verified_event_procurement_group(
    event: Event,
    *,
    now: datetime,
) -> tuple[str, datetime | None]:
    analysis = event.analysis
    deadline = analysis.bid_submission_deadline if analysis is not None else None
    precision = (
        analysis.deadline_precision.get("bid_submission")
        if analysis is not None
        else None
    )
    evidence_fields = (
        {item.field for item in analysis.evidence}
        if analysis is not None
        else set()
    )
    if event.event_type in _PROCUREMENT_RESULT_TYPES:
        return "results", deadline
    if deadline is not None and precision is not None:
        if not deadline_is_current(deadline, precision, now):
            return "results", deadline
        if (
            "bid_submission_deadline" in evidence_fields
            and event.event_type in _PROCUREMENT_ACTIONABLE_TYPES
        ):
            return "opportunities", deadline
    if event.event_type in _PROCUREMENT_ACTIONABLE_TYPES:
        return "confirmation", deadline
    return "results", deadline


def _procurement_item_lines(
    *,
    title: str,
    summary: str,
    organization: str,
    category: Category,
    group: str,
    status_text: str,
    published_at: datetime | None,
    bid_deadline: datetime | None,
    amount: str,
    awarded_supplier: str | None,
    awarded_amount: str | None,
    trust_label: str,
    source_url: str,
    event_type: EventType | None,
    source_is_aggregator: bool,
    now: datetime,
    deadline_supported: bool,
) -> list[str]:
    clean_title = _clean_procurement_title(title)
    trust = "已核实" if trust_label.startswith("已核实") else trust_label
    lines = [f"- **{clean_title}**"]
    matter_summary = _procurement_matter_summary(
        title=clean_title,
        summary=summary,
        event_type=event_type,
        status_text=status_text,
    )
    if matter_summary:
        lines.append(f"  - 摘要：{matter_summary}")
    known = [
        "采购方："
        + (_safe_text(organization) if organization else "需核实确认")
    ]
    known.append(f"方向：{_CATEGORY_LABELS[category]}")
    lines.append("  - " + "｜".join(known))

    if group == "opportunities" and bid_deadline is not None:
        remaining = _remaining_deadline_text(bid_deadline, now)
        lines.append(
            "  - "
            + f"投标截止：{_format_datetime(bid_deadline)}"
            + f"｜剩余：{remaining}"
        )
        optional = [
            "预算/最高限价："
            + (_safe_text(amount) if amount else "需核实确认")
        ]
        optional.append(f"可信：{trust}")
        lines.append("  - " + "｜".join(optional))
    elif group == "confirmation":
        stage = _EVENT_LABELS.get(event_type, "阶段待确认")
        lines.append(f"  - 疑似阶段：{stage}｜可信：{trust}")
        lines.append(
            "  - 预算/最高限价："
            + (_safe_text(amount) if amount else "需核实确认")
            + "｜投标截止："
            + (
                _format_datetime(bid_deadline)
                if bid_deadline is not None and deadline_supported
                else "需核实确认"
            )
        )
        missing = []
        if not organization:
            missing.append("采购方")
        if bid_deadline is None:
            missing.append("投标截止时间")
        elif not deadline_supported:
            missing.append("投标截止时间证据")
        if source_is_aggregator:
            missing.append("官方原始公告")
        if event_type is None:
            missing.append("公告阶段")
        if not missing:
            missing.append("是否仍可投标")
        lines.append("  - 待确认：" + "、".join(missing))
    else:
        date_text = (
            _format_date(published_at)
            if published_at is not None
            else "待确认"
        )
        lines.append(
            "  - "
            + f"结果：{_safe_text(status_text)}"
            + f"｜公示日期：{date_text}"
        )
        result_details = [
            "成交供应商："
            + (
                _safe_text(awarded_supplier)
                if awarded_supplier
                else "需核实确认"
            )
        ]
        result_amount = awarded_amount or (
            amount if event_type is EventType.AWARD else ""
        )
        result_details.append(
            "中标/成交金额："
            + (
                _safe_text(result_amount)
                if result_amount
                else "需核实确认"
            )
        )
        result_details.append(f"可信：{trust}")
        lines.append("  - " + "｜".join(result_details))

    if source_url:
        if source_is_aggregator:
            source_label = "查看聚合线索"
        elif group == "results":
            source_label = "查看结果公告"
        else:
            source_label = "查看官方公告"
        lines.append("  - " + _link(source_label, source_url))
    return lines


def _procurement_matter_summary(
    *,
    title: str,
    summary: str,
    event_type: EventType | None,
    status_text: str,
) -> str:
    if summary:
        concise = _short_summary(summary, 120)
        title_identity = _identity_text(title)
        summary_identity = _identity_text(concise)
        title_like = bool(
            title_identity
            and summary_identity
            and (
                title_identity in summary_identity
                or summary_identity in title_identity
            )
        )
        if concise and not title_like:
            return concise
    extracted = re.search(
        r"(?:采购内容|采购需求|采购标的|项目内容|项目概况)"
        r"[：:]\s*([^。；;\n]{4,90})",
        summary,
    )
    if extracted:
        return _safe_text(f"采购内容：{extracted.group(1).strip()}。")

    subject = title
    lifecycle_suffixes = (
        "单一来源采购成交结果公告",
        "单一来源成交结果公告",
        "单一来源采购成交公告",
        "单一来源成交公告",
        "中标候选人公示",
        "中标结果公告",
        "成交结果公告",
        "中标公告",
        "成交公告",
        "重新招标公告",
        "竞争性磋商公告",
        "竞争性谈判公告",
        "询价公告",
        "比选公告",
        "招标公告",
        "采购公告",
        "废标公告",
        "流标公告",
        "终止公告",
    )
    for suffix in lifecycle_suffixes:
        if subject.endswith(suffix):
            subject = subject[: -len(suffix)].rstrip(" -｜|_：:")
            break
    parts = [f"事项：{subject or title}"]
    methods = (
        ("单一来源", "单一来源"),
        ("公开招标", "公开招标"),
        ("竞争性磋商", "竞争性磋商"),
        ("竞争性谈判", "竞争性谈判"),
        ("询价", "询价"),
        ("比选", "比选"),
    )
    method = next((label for token, label in methods if token in title), None)
    if method:
        parts.append(f"方式：{method}")
    stage = _EVENT_LABELS.get(event_type, status_text)
    if "成交" in title and event_type is EventType.AWARD:
        stage = "成交结果"
    if stage:
        parts.append(f"当前：{_safe_text(stage)}")
    return _safe_text("；".join(parts) + "。")


def _remaining_deadline_text(deadline: datetime, now: datetime) -> str:
    days = (
        _as_beijing(deadline).date() - _as_beijing(now).date()
    ).days
    if days <= 0:
        return "今日截止"
    return f"{days}天"


def _clean_procurement_title(value: str) -> str:
    text = " ".join(value.replace("_", " ").split())
    text = re.sub(
        r"\s*[-｜|]\s*(?:官网[-｜|]?)?(?:[^-｜|]{0,30})?"
        r"(?:招投标|招标采购|公共服务平台|招标网|采购网).*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*[-｜|]\s*(?:官网|公告详情|详情页).*$", "", text)
    return _safe_text(text.strip(" -｜|"))


def _short_candidate_signals(result: RunResult) -> list[_ShortSignal]:
    groups: dict[str, list[object]] = {}
    for item in result.candidate_diagnostics:
        if item.status != "pending" or item.category_hint is None:
            continue
        if _overlaps_verified_financing(
            result,
            category=item.category_hint,
            title=item.title,
            organization=item.organization or "",
            round_name=item.financing_round or "",
            published_at=item.published_at,
        ):
            continue
        key = item.verification_event_key or "|".join(
            (
                item.category_hint.value,
                item.organization or "",
                item.financing_round or "",
                item.title,
            )
        )
        groups.setdefault(key, []).append(item)

    signals: list[_ShortSignal] = []
    surfaced_urls: set[str] = set()
    for key, rows in groups.items():
        representative = max(
            rows,
            key=lambda item: (
                item.evidence_count,
                1 if item.source_grade in {SourceGrade.A, SourceGrade.B} else 0,
                item.source_url,
            ),
        )
        source_urls = tuple(
            sorted({item.source_url for item in rows})
        )
        surfaced_urls.update(source_urls)
        signals.append(
            _ShortSignal(
                category=representative.category_hint,
                label=(
                    "高可信待核实"
                    if _high_confidence_group(rows)
                    else "候选线索"
                ),
                title=representative.title,
                organization=representative.organization or "",
                published_at=representative.published_at,
                amount=representative.amount or "",
                round_name=representative.financing_round or "",
                summary=representative.brief_summary,
                source_urls=source_urls,
                identity=key,
                event_type=representative.event_type,
                registration_deadline=representative.registration_deadline,
                bid_submission_deadline=representative.bid_submission_deadline,
                opening_deadline=representative.opening_deadline,
                deadline_precision=dict(representative.deadline_precision),
                deadline_evidence_fields=tuple(
                    representative.deadline_evidence_fields
                ),
                awarded_supplier=representative.awarded_supplier or "",
                awarded_amount=representative.awarded_amount or "",
            )
        )

    pending_urls = {
        item.source_url for item in result.state.pending
    }
    for item in sorted(
        result.state.pending,
        key=lambda row: _pending_sort_key(row, result.window_end),
    ):
        if item.category_hint is None or item.source_url in surfaced_urls:
            continue
        if _overlaps_verified_financing(
            result,
            category=item.category_hint,
            title=item.title,
            organization="",
            round_name="",
            published_at=item.source_published_at,
        ):
            continue
        surfaced_urls.add(item.source_url)
        signals.append(
            _ShortSignal(
                category=item.category_hint,
                label="候选线索",
                title=item.title,
                organization="",
                published_at=item.source_published_at,
                amount="",
                round_name="",
                summary="",
                source_urls=(item.source_url,),
                identity=f"pending:{item.item_id}",
            )
        )

    for item in result.discovery_candidates:
        if (
            item.category_hint is None
            or item.url in surfaced_urls
            or item.url in pending_urls
            or _overlaps_verified_financing(
                result,
                category=item.category_hint,
                title=item.title,
                organization="",
                round_name="",
                published_at=item.source_published_at,
            )
        ):
            continue
        surfaced_urls.add(item.url)
        signals.append(
            _ShortSignal(
                category=item.category_hint,
                label="候选线索",
                title=item.title,
                organization="",
                published_at=item.source_published_at,
                amount="",
                round_name="",
                summary="",
                source_urls=(item.url,),
                identity=f"candidate:{item.url}",
            )
        )
    signals = _merge_short_financing_signals(signals)
    signals.sort(
        key=lambda item: (
            0 if item.label == "高可信待核实" else 1,
            -(
                _as_beijing(item.published_at).timestamp()
                if item.published_at is not None
                else 0
            ),
            item.identity,
        )
    )
    return signals


def _merge_short_financing_signals(
    signals: list[_ShortSignal],
) -> list[_ShortSignal]:
    merged: list[_ShortSignal] = []
    for signal in signals:
        if signal.category is not Category.COMMERCIAL_SPACE_FINANCING:
            merged.append(signal)
            continue
        match_index = next(
            (
                index
                for index, existing in enumerate(merged)
                if _same_short_financing_event(existing, signal)
            ),
            None,
        )
        if match_index is None:
            merged.append(signal)
            continue
        merged[match_index] = _combine_short_financing_signals(
            merged[match_index],
            signal,
        )
    return merged


def _same_short_financing_event(
    left: _ShortSignal,
    right: _ShortSignal,
) -> bool:
    if (
        left.category is not Category.COMMERCIAL_SPACE_FINANCING
        or right.category is not Category.COMMERCIAL_SPACE_FINANCING
    ):
        return False
    left_subject = _identity_text(left.organization or left.title)
    right_subject = _identity_text(right.organization or right.title)
    if not left_subject or not right_subject:
        return False
    if left_subject not in right_subject and right_subject not in left_subject:
        return False
    left_rounds = _round_identities(left.round_name or left.title)
    right_rounds = _round_identities(right.round_name or right.title)
    if not left_rounds or not right_rounds:
        return False
    if (
        left_rounds != right_rounds
        and not left_rounds.issubset(right_rounds)
        and not right_rounds.issubset(left_rounds)
    ):
        return False
    if left.amount and right.amount:
        if _identity_text(left.amount) != _identity_text(right.amount):
            return False
    if left.published_at is not None and right.published_at is not None:
        left_date = _as_beijing(left.published_at).date()
        right_date = _as_beijing(right.published_at).date()
        if abs((left_date - right_date).days) > 45:
            return False
    return True


def _combine_short_financing_signals(
    left: _ShortSignal,
    right: _ShortSignal,
) -> _ShortSignal:
    source_urls = tuple(sorted({*left.source_urls, *right.source_urls}))
    representative = max(
        (left, right),
        key=lambda item: (
            1 if item.label == "高可信待核实" else 0,
            len(item.organization),
            (
                _as_beijing(item.published_at).timestamp()
                if item.published_at is not None
                else 0
            ),
            len(item.title),
        ),
    )
    published_at = max(
        (
            value
            for value in (left.published_at, right.published_at)
            if value is not None
        ),
        key=lambda value: _as_beijing(value).timestamp(),
        default=None,
    )
    organization = max(
        (left.organization, right.organization),
        key=len,
    )
    round_name = max((left.round_name, right.round_name), key=len)
    label = (
        "高可信待核实"
        if len(source_urls) >= 2
        or "高可信待核实" in {left.label, right.label}
        else "候选线索"
    )
    return _ShortSignal(
        category=representative.category,
        label=label,
        title=representative.title,
        organization=organization,
        published_at=published_at,
        amount=representative.amount or left.amount or right.amount,
        round_name=round_name,
        summary=representative.summary or left.summary or right.summary,
        source_urls=source_urls,
        identity=f"{left.identity}||{right.identity}",
        event_type=representative.event_type,
        registration_deadline=representative.registration_deadline,
        bid_submission_deadline=representative.bid_submission_deadline,
        opening_deadline=representative.opening_deadline,
        deadline_precision=representative.deadline_precision,
        deadline_evidence_fields=representative.deadline_evidence_fields,
        awarded_supplier=representative.awarded_supplier,
        awarded_amount=representative.awarded_amount,
    )


def _short_signal_item(
    signal: _ShortSignal,
    *,
    summary_limit: int,
) -> _ShortItem:
    financing = signal.category is Category.COMMERCIAL_SPACE_FINANCING
    if not financing:
        group = (
            "results"
            if signal.event_type in _PROCUREMENT_RESULT_TYPES
            else "confirmation"
        )
        source_url = signal.source_urls[0] if signal.source_urls else ""
        source_is_aggregator = bool(
            source_url and _looks_like_aggregator(source_url)
        )
        lines = _procurement_item_lines(
            title=signal.title,
            summary=signal.summary,
            organization=signal.organization,
            category=signal.category,
            group=group,
            status_text=_EVENT_LABELS.get(
                signal.event_type,
                "阶段待确认",
            ),
            published_at=signal.published_at,
            bid_deadline=signal.bid_submission_deadline,
            amount=signal.amount,
            awarded_supplier=signal.awarded_supplier or None,
            awarded_amount=signal.awarded_amount or None,
            trust_label=signal.label,
            source_url=source_url,
            event_type=signal.event_type,
            source_is_aggregator=source_is_aggregator,
            now=signal.published_at or datetime(1970, 1, 1, tzinfo=UTC),
            deadline_supported=(
                "bid_submission_deadline"
                in signal.deadline_evidence_fields
            ),
        )
        subject = signal.organization or _clean_procurement_title(signal.title)
        return _ShortItem(
            lines=tuple(lines),
            category=signal.category,
            status=signal.label,
            strict=False,
            daily=False,
            historical=False,
            followup=f"- 核实{_safe_text(subject)}的官方采购原始公告和投标截止时间。",
            procurement_group=group,
            sort_at=signal.bid_submission_deadline or signal.published_at,
        )

    title = _safe_text(signal.title)
    lines = [f"- **【{signal.label}】{title}**"]
    details: list[str] = [
        "企业：" + (
            _safe_text(signal.organization) if signal.organization else "需核实确认"
        ),
        "时间：" + (
            _format_date(signal.published_at)
            if signal.published_at is not None
            else "需核实确认"
        ),
        "轮次：" + (
            _safe_text(signal.round_name) if signal.round_name else "需核实确认"
        ),
        "金额：" + (
            _safe_text(signal.amount) if signal.amount else "需核实确认"
        ),
    ]
    summary = _short_summary(signal.summary, summary_limit)
    details.append(
        "摘要："
        + (
            summary
            or "当前仅确认该融资线索与商业航天相关，关键事实仍需核实。"
        )
    )
    lines.extend(f"  - {detail}" for detail in details)
    source_links = _short_signal_source_links(signal)
    if source_links:
        lines.append("  - 来源：" + "｜".join(source_links))
    subject = signal.organization or signal.title
    followup = f"- 核实{_safe_text(subject)}的企业或投资方官方融资公告。"
    return _ShortItem(
        lines=tuple(lines),
        category=signal.category,
        status=signal.label,
        strict=False,
        daily=False,
        historical=False,
        followup=followup,
    )


def _short_financing_source_links(item: Financing) -> list[str]:
    urls = sorted({item.source_url, *item.source_urls})
    labels = [_source_label(item, url) for url in urls]
    counts = {label: labels.count(label) for label in set(labels)}
    indexes: dict[str, int] = {}
    links: list[str] = []
    for url, label in zip(urls[:2], labels[:2], strict=True):
        if counts[label] > 1:
            indexes[label] = indexes.get(label, 0) + 1
            label = f"{label}{indexes[label]}"
        links.append(_link(label, url))
    return links


def _structured_financing_summary(item: Financing) -> str:
    round_text = item.round_name or "股权融资"
    sentence = f"{item.company}完成{round_text}"
    amount = _financing_amount(item)
    if amount:
        sentence += f"，披露金额{amount}"
    if item.investors:
        sentence += "，投资方包括" + "、".join(sorted(item.investors)[:3])
    if item.business_area:
        sentence += f"，业务聚焦{item.business_area}"
    return sentence[:120]


def _short_signal_source_links(signal: _ShortSignal) -> list[str]:
    links: list[str] = []
    for index, url in enumerate(signal.source_urls[:2], start=1):
        if (
            signal.category in _ROLLING_CATEGORIES
            and _looks_like_aggregator(url)
        ):
            label = "聚合线索"
        elif len(signal.source_urls) == 1:
            label = "原始来源"
        else:
            label = f"来源{index}"
        links.append(_link(label, url))
    return links


def _looks_like_aggregator(url: str) -> bool:
    domain = (urlsplit(url).hostname or "").lower()
    return any(
        marker in domain
        for marker in (
            "jianyu",
            "bidcenter",
            "zhaobiao",
            "qianlima",
            "chinabidding",
        )
    )


def _short_summary(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    text = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    text = re.sub(r"本条项目信息由[^。；]*[。；]?", "", text)
    text = re.sub(r"采购联系人.*$", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = text.strip(" ，,。；;：:")
    if not text:
        return ""
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip(" ，,。；;：:") + "…"
    return _safe_text(text)


def _select_procurement_items(
    items: list[_ShortItem],
    *,
    max_items: int,
) -> list[_ShortItem]:
    selected: list[_ShortItem] = []
    for group in ("opportunities", "confirmation", "results"):
        rows = [item for item in items if item.procurement_group == group]
        rows.sort(
            key=lambda item: (
                (
                    _as_beijing(item.sort_at).timestamp()
                    if item.sort_at is not None
                    else float("inf")
                )
                if group == "opportunities"
                else -(
                    _as_beijing(item.sort_at).timestamp()
                    if item.sort_at is not None
                    else 0
                )
            )
        )
        selected.extend(rows[:max_items])
    return selected


def _short_procurement_section(items: list[_ShortItem]) -> str:
    definitions = (
        (
            "opportunities",
            "### 🟢 可投标机会",
            "- 暂无已确认且仍在投标期内的机会。",
        ),
        (
            "confirmation",
            "### 🟡 需核实确认",
            "- 暂无需要人工确认的行动线索。",
        ),
        (
            "results",
            "### 🔵 结果与行业动态",
            "- 暂无新的结果或行业动态。",
        ),
    )
    blocks = ["## 二、招标采购情况"]
    for group, heading, empty_text in definitions:
        rows = [item for item in items if item.procurement_group == group]
        lines = [line for item in rows for line in item.lines]
        blocks.append(heading + "\n" + "\n".join(lines or [empty_text]))
    strict = sum(item.strict for item in items)
    high_confidence = sum(item.status == "高可信待核实" for item in items)
    candidates = sum(item.status == "候选线索" for item in items)
    blocks.append(
        (
            f"**招标统计：已核实 {strict} 条；"
            f"高可信待核实 {high_confidence} 条；"
            f"候选线索 {candidates} 条。**"
        )
    )
    return "\n\n".join(blocks)


def _short_section(
    heading: str,
    items: list[_ShortItem],
    *,
    empty_text: str,
    statistics_label: str,
) -> str:
    lines = [line for item in items for line in item.lines]
    if not lines:
        lines.append(f"- {empty_text}")
    strict = sum(item.strict for item in items)
    high_confidence = sum(
        item.status == "高可信待核实" for item in items
    )
    candidates = sum(item.status == "候选线索" for item in items)
    lines.append(
        (
            f"**{statistics_label}：已核实 {strict} 条；"
            f"高可信待核实 {high_confidence} 条；"
            f"候选线索 {candidates} 条。**"
        )
    )
    return f"## {heading}\n" + "\n".join(lines)


def _short_other_dynamics(procurement_items: list[_ShortItem]) -> str:
    counts = {
        category: sum(item.category is category for item in procurement_items)
        for category in _ROLLING_CATEGORIES
    }
    lines = []
    for category in _ROLLING_CATEGORIES:
        count = counts[category]
        if count:
            text = f"本期有 {count} 条采购信息，见上方。"
        else:
            text = "暂无重要新增。"
        lines.append(f"- {_CATEGORY_LABELS[category]}：{text}")
    return "## 三、其他行业动态\n" + "\n".join(lines)


def _short_followups(
    result: RunResult,
    *,
    financing_items: list[_ShortItem],
    procurement_items: list[_ShortItem],
) -> tuple[str, ...]:
    lines: list[str] = []
    for item in [*financing_items, *procurement_items]:
        if item.followup and item.followup not in lines:
            lines.append(item.followup)
        if len(lines) >= 3:
            return tuple(lines)
    for project in sorted(result.state.projects, key=_open_project_sort_key):
        if project.status not in _OPEN_STATUSES:
            continue
        deadline = _actionable_deadline(project, result.window_end)
        if deadline is None:
            continue
        line = (
            f"- {_safe_text(project.name)}将于"
            f"{_format_datetime(deadline)}截止。"
        )
        if project.latest_source_url:
            line += _link("查看公告", project.latest_source_url)
        if line not in lines:
            lines.append(line)
        if len(lines) >= 3:
            break
    return tuple(lines)


def _short_system_status(
    result: RunResult,
    displayed: list[_ShortItem],
) -> str:
    strict = sum(item.strict for item in displayed)
    return (
        f"系统状态：检索 {result.metrics.raw_search_count} 条，"
        f"形成 {result.metrics.final_candidate_count} 条候选；"
        f"本期展示严格已核实 {strict} 条；"
        f"覆盖{_coverage_status_text(result)}。"
    )


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
        f"- 严格已核实",
        _format_date(item.announced_at),
        _CATEGORY_LABELS[Category.COMMERCIAL_SPACE_FINANCING],
        _safe_text(item.company),
    ]
    round_text = (
        (
            _safe_text(item.round_name)
            if item.round_name
            else subtype_labels.get(item.financing_subtype)
        )
    )
    if round_text:
        parts.append(round_text)
    amount_text = _financing_amount(item)
    if amount_text is not None:
        parts.append(f"金额：{amount_text}")
    if item.investors:
        parts.append(
            f"投资方：{'、'.join(_safe_text(value) for value in sorted(item.investors))}"
        )
    if item.business_area:
        parts.append(f"领域：{_safe_text(item.business_area)}")
    urls = sorted({item.source_url, *item.source_urls})
    labels = [_source_label(item, url) for url in urls]
    label_counts = {
        label: labels.count(label)
        for label in set(labels)
    }
    label_indexes: dict[str, int] = {}
    source_links: list[str] = []
    for url, label in zip(urls, labels, strict=True):
        if label_counts[label] > 1:
            label_indexes[label] = label_indexes.get(label, 0) + 1
            label = f"{label}{label_indexes[label]}"
        source_links.append(_link(label, url))
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


def _financing_amount(item: Financing) -> str | None:
    if not item.amount_disclosed or item.amount_cny is None:
        if _explicitly_undisclosed_amount(item):
            return "未披露"
        return None
    amount = item.amount_cny
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.2f}亿元"
    if amount >= 10_000:
        return f"{amount / 10_000:.2f}万元"
    return f"{amount:.2f}元"


def _explicitly_undisclosed_amount(item: Financing) -> bool:
    markers = ("未披露", "未公布", "未透露")
    return any(
        (
            "amount" in evidence.field.lower()
            or "金额" in evidence.field
        )
        and any(marker in evidence.quote for marker in markers)
        for evidence in item.evidence
    )


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
            if item.category_hint is None
            if result.window_start
            <= _as_beijing(item.discovered_at)
            <= result.window_end
        ),
        key=lambda item: _pending_sort_key(item, result.window_end),
    )
    for item in current_pending[:10]:
        reason = _pending_reason_text(item.reason)
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
        time_label = _pending_time_label(
            item.source_published_at,
            result.window_end,
            result.metrics.fallback_window_days,
        )
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
    return tuple(lines)


def _formal_and_candidate_lines(
    formal_lines: tuple[str, ...], candidate_lines: tuple[str, ...]
) -> tuple[str, ...]:
    return (*formal_lines, *candidate_lines)


def _category_candidate_lines(
    result: RunResult, category: Category
) -> tuple[str, ...]:
    diagnostic_lines, diagnostic_urls = _diagnostic_signal_lines(result, category)
    pending_rows = sorted(
        (
            item
            for item in result.state.pending
            if item.category_hint is category
            and item.source_url not in diagnostic_urls
            and not _overlaps_verified_financing(
                result,
                category=item.category_hint,
                title=item.title,
                organization="",
                round_name="",
                published_at=item.source_published_at,
            )
            and result.window_start
            <= _as_beijing(item.discovered_at)
            <= result.window_end
        ),
        key=lambda item: _pending_sort_key(item, result.window_end),
    )
    surfaced_urls = {
        item.source_url for item in result.state.pending
    } | {
        item.source_url for item in result.state.events
    } | {
        url
        for item in result.state.financings
        for url in (item.source_urls or [item.source_url])
    }
    candidates = [
        item
        for item in result.discovery_candidates
        if item.category_hint is category
        and item.url not in surfaced_urls
        and item.url not in diagnostic_urls
        and not _overlaps_verified_financing(
            result,
            category=item.category_hint,
            title=item.title,
            organization="",
            round_name="",
            published_at=item.source_published_at,
        )
    ]
    lines: list[str] = list(diagnostic_lines)
    for item in pending_rows[: max(0, 5 - len(lines))]:
        source_date = (
            _format_date(item.source_published_at)
            if item.source_published_at is not None
            else "发布日期未知"
        )
        lines.append(
            "｜".join(
                (
                    "- 候选线索（未核实）",
                    source_date,
                    _pending_time_label(
                        item.source_published_at,
                        result.window_end,
                        result.metrics.fallback_window_days,
                    ),
                    _safe_text(item.title),
                    f"原因：{_pending_reason_text(item.reason)}",
                    _link("查看原始来源", item.source_url),
                )
            )
        )
        if item.summary.strip():
            lines.append(
                f"  - 摘要（未核实）：{_safe_text(item.summary[:180])}"
            )
    remaining = max(0, 5 - len([line for line in lines if line.startswith("- ")]))
    for item in candidates[:remaining]:
        source_date = (
            _format_date(item.source_published_at)
            if item.source_published_at is not None
            else "发布日期未知"
        )
        time_label = _pending_time_label(
            item.source_published_at,
            result.window_end,
            result.metrics.fallback_window_days,
        )
        lines.append(
            "｜".join(
                (
                    "- 候选线索（未核实）",
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
    if len(candidates) > remaining:
        lines.append(f"- 另有 {len(candidates) - remaining} 条候选线索未展开")
    return tuple(lines)


def _top_signal_lines(
    result: RunResult,
    formal_groups: tuple[tuple[str, ...], ...],
) -> tuple[str, ...]:
    selected_groups = list(formal_groups[:3])
    selected = [
        line
        for group in selected_groups
        for line in group
    ]
    if len(selected_groups) >= 3:
        return tuple(selected)
    diagnostic_lines, _ = _diagnostic_signal_lines(result, None)
    remaining = 3 - len(selected_groups)
    added = 0
    for line in diagnostic_lines:
        if line.startswith("- "):
            if added >= remaining:
                break
            added += 1
        selected.append(line)
    if selected:
        return tuple(selected)
    return ("- 本轮未形成可展示的境内主题相关信息。",)


def _deduplicate_section_signals(
    sections: list[_Section],
) -> list[_Section]:
    """Keep each fully rendered event line in its first, highest-priority section."""

    seen: set[str] = set()
    deduplicated: list[_Section] = []
    for section in sections:
        kept: list[str] = []
        skip_continuation = False
        for line in section.lines:
            if line.startswith("- "):
                skip_continuation = False
                signal_key = line if "](" in line else None
                if signal_key is not None and signal_key in seen:
                    skip_continuation = True
                    continue
                if signal_key is not None:
                    seen.add(signal_key)
                kept.append(line)
                continue
            if line.startswith("  - ") and skip_continuation:
                continue
            kept.append(line)
        deduplicated.append(
            _Section(
                heading=section.heading,
                lines=tuple(kept),
                protected=section.protected,
                project_ids=section.project_ids,
            )
        )
    return deduplicated


def _diagnostic_signal_lines(
    result: RunResult,
    category: Category | None,
) -> tuple[tuple[str, ...], set[str]]:
    candidates_by_url = {
        item.url: item for item in result.discovery_candidates
    }
    groups: dict[str, list[object]] = {}
    for item in result.candidate_diagnostics:
        if item.status != "pending":
            continue
        if category is not None and item.category_hint is not category:
            continue
        if _overlaps_verified_financing(
            result,
            category=item.category_hint,
            title=item.title,
            organization=item.organization or "",
            round_name=item.financing_round or "",
            published_at=item.published_at,
        ):
            continue
        if (
            not item.selected_for_report
            and item.source_grade not in {SourceGrade.A, SourceGrade.B}
        ):
            continue
        key = item.verification_event_key or "|".join(
            (
                item.category_hint.value if item.category_hint else "",
                item.organization or "",
                item.financing_round or "",
                item.title,
            )
        )
        groups.setdefault(key, []).append(item)

    ranked = sorted(
        groups.values(),
        key=lambda rows: (
            0 if _high_confidence_group(rows) else 1,
            -max(
                (
                    _as_beijing(item.published_at).timestamp()
                    if item.published_at is not None
                    else 0
                )
                for item in rows
            ),
            min(item.source_url for item in rows),
        ),
    )
    lines: list[str] = []
    urls: set[str] = set()
    for rows in ranked[:5]:
        representative = max(
            rows,
            key=lambda item: (
                item.evidence_count,
                1 if item.source_grade in {SourceGrade.A, SourceGrade.B} else 0,
                item.source_url,
            ),
        )
        label = (
            "高可信待核实"
            if _high_confidence_group(rows)
            else "候选线索（未核实）"
        )
        parts = [f"- {label}"]
        if representative.published_at is not None:
            parts.append(_format_date(representative.published_at))
        if representative.organization:
            parts.append(_safe_text(representative.organization))
        parts.append(_safe_text(representative.title))
        if representative.financing_round:
            parts.append(f"轮次：{_safe_text(representative.financing_round)}")
        if representative.amount:
            parts.append(f"明确金额：{_safe_text(representative.amount)}")
        source_urls = sorted({item.source_url for item in rows})
        urls.update(source_urls)
        for index, source_url in enumerate(source_urls[:2], start=1):
            label_text = "查看原始来源" if len(source_urls) == 1 else f"来源 {index}"
            parts.append(_link(label_text, source_url))
        lines.append("｜".join(parts))
        candidate = candidates_by_url.get(representative.source_url)
        summary = representative.summary or (
            candidate.summary if candidate is not None else ""
        )
        if summary.strip():
            lines.append(
                f"  - 摘要（未核实）：{_safe_text(summary[:180])}"
            )
    return tuple(lines), urls


def _overlaps_verified_financing(
    result: RunResult,
    *,
    category: Category | None,
    title: str,
    organization: str,
    round_name: str,
    published_at: datetime | None,
) -> bool:
    if category is not Category.COMMERCIAL_SPACE_FINANCING:
        return False
    identity_text = _identity_text(f"{organization} {title}")
    candidate_rounds = _round_identities(round_name or title)
    for item in result.state.financings:
        if item.verification_status is not VerificationStatus.VERIFIED:
            continue
        if _identity_text(item.company) not in identity_text:
            continue
        verified_rounds = _round_identities(item.round_name or "")
        if (
            verified_rounds
            and candidate_rounds
            and verified_rounds.isdisjoint(candidate_rounds)
        ):
            continue
        if verified_rounds and not candidate_rounds:
            continue
        if published_at is not None:
            candidate_date = _as_beijing(published_at)
            verified_date = _as_beijing(item.announced_at)
            if abs((candidate_date.date() - verified_date.date()).days) > 45:
                continue
        return True
    return False


def _identity_text(value: str) -> str:
    return re.sub(r"[^\w+]+", "", value.casefold())


def _round_identity(value: str) -> str:
    match = re.search(
        r"(?i)(pre[\s-]?[a-d]\+{0,2}|[a-d]\+{0,2}|"
        r"天使\+{0,2}|种子|战略投资|战略)",
        value,
    )
    if match is None:
        return ""
    return re.sub(r"[\s-]+", "", match.group(1).casefold())


def _round_identities(value: str) -> frozenset[str]:
    return financing_round_identities(value)


def _high_confidence_group(rows: list[object]) -> bool:
    source_urls = {item.source_url for item in rows}
    if len(source_urls) >= 2:
        return True
    return any(
        item.source_grade in {SourceGrade.A, SourceGrade.B}
        and item.evidence_count >= 5
        for item in rows
    )


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
    base_search_used = max(
        0, metrics.search_budget_used - metrics.elastic_search_calls
    )
    agent_budget_text = (
        f"基础预算 {metrics.search_budget}；"
        f"基础调用 {base_search_used}；"
        f"弹性调用 {metrics.elastic_search_calls}；"
        f"总调用 {metrics.search_budget_used}"
        if metrics.elastic_search_calls
        else (
            f"预算 {metrics.search_budget}；"
            f"实际调用 {metrics.search_budget_used}"
        )
    )
    agent_line = (
        f"- 智能检索：{agent_budget_text}；"
        f"模型轮次 {metrics.agent_round_count}；"
        f"重复查询拦截 {metrics.duplicate_query_count}；"
        f"事件过滤淘汰 {metrics.event_filter_rejected_count}；"
        f"事件级合并 {metrics.event_duplicate_count}；"
        f"停止原因 {_safe_text(metrics.agent_stop_reason or '未记录')}"
        if metrics.search_budget
        else None
    )
    verification_line = (
        f"- 定向核验：处理事件 {metrics.verification_targets_count}；"
        f"新增来源 {metrics.verification_new_source_count}；"
        f"重复来源 {metrics.verification_duplicate_source_count}；"
        "触发原因 "
        + (
            "、".join(
                _safe_text(reason)
                for reason in metrics.elastic_trigger_reasons
            )
            or "未记录"
        )
        if metrics.verification_targets_count or metrics.elastic_search_calls
        else None
    )
    lines = [
        f"- {_safe_text(result.trend_summary.summary)}",
        f"- 分类计数：{count_text}",
        (
            f"- 采集漏斗：博查原始 {metrics.raw_search_count}；"
            f"结构有效 {metrics.valid_shape_count}；"
            f"主题相关 {metrics.relevance_pass_count}；"
            f"近 7 天 {metrics.recent_7d_count}；"
            f"8–{metrics.fallback_window_days} 天补充 "
            f"{metrics.fallback_8_30d_count}；"
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
    ]
    if agent_line is not None:
        lines.insert(3, agent_line)
    if verification_line is not None:
        lines.insert(4, verification_line)
    return tuple(lines)


def _pending_time_label(
    source_published_at: datetime | None,
    window_end: datetime,
    fallback_window_days: int = 30,
) -> str:
    if source_published_at is None:
        return "日期未知"
    age = _as_beijing(window_end) - _as_beijing(source_published_at)
    if age.days <= 7:
        return "近 7 天"
    if age.days <= fallback_window_days:
        return f"8–{fallback_window_days} 天补充"
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


_MISSING_FIELD_LABELS = {
    "title": "标题",
    "organization": "主体",
    "published_at": "发布日期",
    "category": "分类",
    "event_type": "事件类型",
}


def _pending_reason_text(reason: str) -> str:
    prefix = "missing_required_fields:"
    if reason.startswith(prefix):
        fields = [
            _MISSING_FIELD_LABELS.get(name, _safe_text(name))
            for name in reason[len(prefix) :].split(",")
            if name
        ]
        return f"核验字段缺失（{'、'.join(fields)}）"
    return _PENDING_REASON_LABELS.get(reason, _safe_text(reason))


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
