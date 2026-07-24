"""Deterministic identity and lifecycle matching for verified intelligence."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from dateutil.relativedelta import relativedelta
from pydantic import Field

from .discovery import normalize_url
from .models import DomainModel, Event, EventType, Financing, Project


_LIFECYCLE_TOKENS = (
    "采购意向公告",
    "采购意向",
    "招标公告",
    "招标文件",
    "中标候选人公示",
    "中标候选人",
    "中标结果公告",
    "中标结果",
    "中标公告",
    "成交结果公告",
    "成交结果",
    "成交公告",
    "更正公告",
    "变更公告",
    "延期公告",
    "终止公告",
    "废标公告",
    "流标公告",
    "重新招标公告",
    "重新招标",
    "二次招标公告",
    "二次招标",
    "二次采购公告",
    "二次采购",
    "二次",
    "更正",
    "变更",
    "延期",
    "终止",
    "废标",
    "流标",
)
_REBID_TOKENS = ("重新招标", "二次招标", "二次采购")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})年?")
_MARKER_NUMBER = r"[一二两三四五六七八九十百零〇\d]+"
_BATCH_RE = re.compile(rf"第?({_MARKER_NUMBER})批")
_LOT_NUMBER_FIRST_RE = re.compile(
    rf"第?\s*(?P<number>{_MARKER_NUMBER})\s*(?:标段|包|lot)", re.I
)
_LOT_MARKER_FIRST_RE = re.compile(
    rf"(?:标段|包|lot)\s*[-_:/]?\s*第?\s*(?P<number>{_MARKER_NUMBER})", re.I
)
_BARE_SECOND_PREFIX_RE = re.compile(r"^二次(?!元)", re.I)
_CODE_REBID_SUFFIX = re.compile(r"(?:[-_/](?:r?2|rebid)|二次|重新招标)$", re.I)


class MatchDecision(DomainModel):
    relation: Literal["same_project", "suspected", "new_project"]
    project_id: str | None = None
    reason: str
    score: float = Field(ge=0, le=1)


def normalize_text(value: str) -> str:
    """Normalize display text without erasing semantically meaningful digits."""
    normalized = unicodedata.normalize("NFKC", value).lower()
    characters: list[str] = []
    pending_space = False
    for character in normalized:
        category = unicodedata.category(character)
        if character.isspace():
            pending_space = bool(characters)
            continue
        if category.startswith(("P", "S")):
            continue
        if pending_space:
            characters.append(" ")
            pending_space = False
        characters.append(character)
    return "".join(characters).strip()


def _core_title(value: str) -> str:
    title = normalize_text(value)
    bare_second_prefix = _has_bare_second_prefix(value)
    tokens = sorted(
        {normalize_text(token) for token in _LIFECYCLE_TOKENS},
        key=len,
        reverse=True,
    )
    changed = True
    while changed and title:
        changed = False
        for token in tokens:
            if title == token:
                return ""
            if title.endswith(token):
                title = title[: -len(token)].strip()
                changed = True
                break
            if title.startswith(token) and (
                token != normalize_text("二次") or bare_second_prefix
            ):
                title = title[len(token) :].strip()
                changed = True
                break
    return title.strip()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _stable_hash(payload: object) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def content_version_id(source_url: str, content_hash: str) -> str:
    """Return one immutable identity for a normalized URL/content pair."""
    return _stable_hash(
        {"source_url": normalize_url(source_url), "content_hash": content_hash.lower()}
    )


def event_fingerprint(event: Event) -> str:
    """Return the semantic identity of one formal event."""
    return _stable_hash(
        {
            "event_type": event.event_type.value,
            "published_at": _timestamp(event.published_at),
            "source_url": normalize_url(event.source_url),
            "title": normalize_text(event.title),
            "content_version_id": event.content_version_id or None,
        }
    )


def _normalized_amount(financing: Financing) -> str | None:
    if financing.amount_cny is None:
        return None
    return format(financing.amount_cny, ".15g")


def financing_fingerprint(financing: Financing) -> str:
    """Deduplicate equivalent financing reports independent of their media source."""
    return _stable_hash(
        {
            "announced_at": financing.announced_at.date().isoformat(),
            "amount_cny": _normalized_amount(financing),
            "company": normalize_text(financing.company),
            "investors": sorted(normalize_text(name) for name in financing.investors),
            "round_name": normalize_text(financing.round_name or ""),
            "financing_subtype": financing.financing_subtype or "",
        }
    )


def stable_event_id(event: Event) -> str:
    return str(uuid5(NAMESPACE_URL, f"laser-space-daily:event:{event_fingerprint(event)}"))


def _event_codes(event: Event) -> tuple[str, ...]:
    if event.analysis is None:
        return ()
    return tuple(sorted({_normalize_code(code) for code in event.analysis.project_codes if code}))


def _event_source_codes(event: Event) -> tuple[str, ...]:
    if event.analysis is None:
        return ()
    return tuple(code for code in event.analysis.project_codes if code)


def stable_project_id(event: Event) -> str:
    payload = {
        "organization": normalize_text(event.organization),
        "project_codes": _event_codes(event),
        "title": _core_title(event.title),
    }
    return str(
        uuid5(
            NAMESPACE_URL,
            "laser-space-daily:project:"
            + json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    )


def _normalize_code(value: str) -> str:
    return "".join(character for character in normalize_text(value) if character.isalnum())


def _derived_code(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower().strip()
    without_suffix = _CODE_REBID_SUFFIX.sub("", normalized)
    return _normalize_code(without_suffix)


def _marker(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = normalize_text(str(value)).removeprefix("第")
    if normalized.isdigit():
        return str(int(normalized))
    chinese_number = _chinese_integer(normalized)
    return str(chinese_number) if chinese_number is not None else normalized


def _chinese_integer(value: str) -> int | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000}
    if not value or any(character not in digits | units for character in value):
        return None
    if not any(character in units for character in value):
        return int("".join(str(digits[character]) for character in value))
    total = 0
    current = 0
    for character in value:
        if character in digits:
            current = digits[character]
        else:
            current = current or 1
            total += current * units[character]
            current = 0
    return total + current


def _extract_markers(title: str) -> dict[str, object | None]:
    normalized = unicodedata.normalize("NFKC", title)
    year = _YEAR_RE.search(normalized)
    batch = _BATCH_RE.search(normalized)
    lot = _LOT_NUMBER_FIRST_RE.search(normalized) or _LOT_MARKER_FIRST_RE.search(
        normalized
    )
    return {
        "year": int(year.group(1)) if year else None,
        "batch": _marker(batch.group(1)) if batch else None,
        "lot": _marker(lot.group("number")) if lot else None,
    }


def _project_marker(project: Project, field: str) -> object | None:
    explicit = getattr(project, field)
    if explicit is not None:
        return _marker(explicit) if field != "year" else explicit
    title_marker = _extract_markers(project.name)[field]
    if title_marker is not None:
        return title_marker
    if field == "year":
        project_date = project.first_published_at or project.latest_event_at
        if project_date is not None:
            return project_date.year
    return None


def _has_bare_second_prefix(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).lstrip()
    return _BARE_SECOND_PREFIX_RE.match(normalized) is not None


def _is_rebid(event: Event) -> bool:
    normalized = normalize_text(event.title)
    if event.event_type is EventType.REBID:
        return True
    if any(normalize_text(token) in normalized for token in _REBID_TOKENS):
        return True
    if _has_bare_second_prefix(event.title):
        return True
    second = normalize_text("二次")
    return normalized == second or normalized.endswith(second)


def _codes_align_for_rebid(event_codes: set[str], project_codes: set[str]) -> bool:
    if not event_codes or not project_codes:
        return False
    normalized_event_codes = {_normalize_code(code) for code in event_codes}
    normalized_project_codes = {_normalize_code(code) for code in project_codes}
    if normalized_event_codes & normalized_project_codes:
        return True
    return bool({_derived_code(code) for code in event_codes} & {_derived_code(code) for code in project_codes})


def _valid_lifecycle_timing(event: Event, project: Project) -> bool:
    latest = project.latest_event_at or project.first_published_at
    if latest is None:
        return True
    published = event.published_at
    if (latest.tzinfo is None) != (published.tzinfo is None):
        latest = latest.replace(tzinfo=published.tzinfo)
    return latest <= published <= latest + relativedelta(months=18)


class ProjectMatcher:
    """Match lifecycle events conservatively with deterministic tie breaking."""

    def match(self, event: Event, projects: list[Project]) -> MatchDecision:
        buyer = normalize_text(event.organization)
        event_markers = _extract_markers(event.title)
        event_year_is_explicit = event_markers["year"] is not None
        if not event_year_is_explicit:
            event_markers["year"] = event.published_at.year
        event_codes = set(_event_codes(event))
        event_source_codes = set(_event_source_codes(event))
        event_title = _core_title(event.title)
        rebid = _is_rebid(event)
        eligible: list[Project] = []
        guard_failures: list[tuple[str, str]] = []

        for project in sorted(projects, key=lambda item: item.project_id):
            if normalize_text(project.organization) != buyer:
                guard_failures.append((project.project_id, "buyer_mismatch"))
                continue
            if project.category is not event.category:
                guard_failures.append((project.project_id, "category_mismatch"))
                continue
            conflict = self._marker_conflict(event_markers, project)
            project_codes = {
                _normalize_code(code) for code in project.project_codes if code
            }
            exact_code = bool(event_codes & project_codes)
            if conflict == "year" and exact_code and not event_year_is_explicit:
                conflict = None
            if conflict is not None:
                guard_failures.append((project.project_id, f"conflicting_{conflict}"))
                continue
            eligible.append(project)

        if not eligible:
            reason = guard_failures[0][1] if len(projects) == 1 else "no_match"
            return MatchDecision(relation="new_project", reason=reason, score=0)

        exact_codes = [
            project
            for project in eligible
            if event_codes & {_normalize_code(code) for code in project.project_codes}
            and (not rebid or event_title == _core_title(project.name))
        ]
        if exact_codes:
            project = exact_codes[0]
            return MatchDecision(
                relation="same_project",
                project_id=project.project_id,
                reason="exact_project_code",
                score=1,
            )

        aligned_rebid_projects = (
            [
                project
                for project in eligible
                if _codes_align_for_rebid(
                    event_source_codes, set(project.project_codes)
                )
            ]
            if rebid
            else []
        )
        aligned_rebid_titles = [
            project
            for project in aligned_rebid_projects
            if event_title == _core_title(project.name)
        ]
        if aligned_rebid_titles:
            project = aligned_rebid_titles[0]
            return MatchDecision(
                relation="same_project",
                project_id=project.project_id,
                reason="rebid_original_code",
                score=1,
            )

        ranking_pool = aligned_rebid_projects or eligible
        ranked: list[tuple[float, str, Project]] = []
        for project in ranking_pool:
            score = SequenceMatcher(None, event_title, _core_title(project.name)).ratio()
            ranked.append((score, project.project_id, project))
        score, _, project = max(ranked, key=lambda row: (row[0], tuple(-ord(c) for c in row[1])))
        project_codes = set(project.project_codes)

        if rebid:
            if _codes_align_for_rebid(event_source_codes, project_codes):
                return MatchDecision(
                    relation="suspected",
                    project_id=project.project_id,
                    reason="rebid_core_title_mismatch",
                    score=score,
                )
            if score >= 0.75:
                return MatchDecision(
                    relation="suspected",
                    project_id=project.project_id,
                    reason="rebid_code_unaligned",
                    score=score,
                )
            return MatchDecision(relation="new_project", reason="no_match", score=score)

        if event_title == _core_title(project.name):
            return MatchDecision(
                relation="same_project",
                project_id=project.project_id,
                reason="same_buyer_title",
                score=1,
            )

        if score >= 0.9 and _valid_lifecycle_timing(event, project):
            return MatchDecision(
                relation="same_project",
                project_id=project.project_id,
                reason="title_similarity",
                score=score,
            )
        if score >= 0.75:
            reason = "invalid_lifecycle_timing" if score >= 0.9 else "title_similarity"
            return MatchDecision(
                relation="suspected",
                project_id=project.project_id,
                reason=reason,
                score=score,
            )
        return MatchDecision(relation="new_project", reason="no_match", score=score)

    @staticmethod
    def _marker_conflict(
        event_markers: dict[str, object | None], project: Project
    ) -> str | None:
        for field in ("year", "batch", "lot"):
            event_value = event_markers[field]
            project_value = _project_marker(project, field)
            if event_value is not None and project_value is not None:
                if _marker(event_value) != _marker(project_value):
                    return field
        return None
