"""Validated, JSON-compatible domain models for the intelligence pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Category(StrEnum):
    LASER_COMMUNICATION = "laser_communication"
    LASER_WEAPON = "laser_weapon"
    EO_TURRET = "eo_turret"
    COMMERCIAL_SPACE_FINANCING = "commercial_space_financing"


class EventType(StrEnum):
    PROCUREMENT_INTENTION = "procurement_intention"
    TENDER = "tender"
    INQUIRY = "inquiry"
    COMPARISON = "comparison"
    CHANGE = "change"
    EXTENSION = "extension"
    TERMINATION = "termination"
    CANDIDATE = "candidate"
    AWARD = "award"
    FAILED = "failed"
    REBID = "rebid"
    FINANCING = "financing"


class SourceGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    PENDING = "pending"
    REJECTED = "rejected"


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(DomainModel):
    title: str
    url: str
    summary: str = ""
    discovered_at: datetime
    discovery_source: str
    category_hint: Category | None = None
    source_published_at: datetime | None = None


class Evidence(DomainModel):
    field: str
    quote: str
    source_url: str


class AnalysisResult(DomainModel):
    in_china: bool
    in_scope: bool
    category: Category | None = None
    event_type: EventType | None = None
    title: str
    organization: str | None = None
    published_at: datetime | None = None
    project_codes: list[str] = Field(default_factory=list)
    amount: str | None = None
    amount_disclosed: bool | None = None
    financing_round: str | None = None
    financing_subtype: Literal[
        "round_equity", "strategic", "capital_increase", "merger_acquisition"
    ] | None = None
    business_area: str | None = None
    investors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    registration_deadline: datetime | None = None
    bid_submission_deadline: datetime | None = None
    opening_deadline: datetime | None = None
    deadline_precision: dict[
        Literal["registration", "bid_submission", "opening"],
        Literal["date", "minute", "second"],
    ] = Field(default_factory=dict)
    source_url: str
    degraded: bool = False

    @field_validator(
        "registration_deadline",
        "bid_submission_deadline",
        "opening_deadline",
    )
    @classmethod
    def deadlines_must_have_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("deadline must include timezone")
        return value


class TrendSummary(DomainModel):
    window_start: datetime
    window_end: datetime
    summary: str
    event_count: int = Field(ge=0)
    category_counts: dict[Category, int] = Field(default_factory=dict)
    degraded: bool = False


class SourceRecord(DomainModel):
    source_url: str
    source_grade: SourceGrade
    published_at: datetime
    content_hash: str
    content_version_id: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class Event(DomainModel):
    event_id: str
    category: Category
    title: str
    organization: str
    published_at: datetime
    source_url: str
    source_grade: SourceGrade
    verification_status: VerificationStatus
    event_type: EventType = EventType.CANDIDATE
    formal_record: bool = True
    evidence: list[Evidence] = Field(default_factory=list)
    analysis: AnalysisResult | None = None
    discovered_at: datetime | None = None
    content_hash: str = ""
    content_version_id: str = ""
    first_seen_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def formal_records_must_be_verified(self) -> "Event":
        if self.formal_record and self.verification_status is not VerificationStatus.VERIFIED:
            raise ValueError("formal records must be verified")
        return self


class Project(DomainModel):
    project_id: str
    name: str
    organization: str
    category: Category
    status: str
    event_ids: list[str] = Field(default_factory=list)
    project_codes: list[str] = Field(default_factory=list)
    normalized_name: str | None = None
    current_stage: EventType | None = None
    amount: str | None = None
    first_published_at: datetime | None = None
    latest_event_at: datetime | None = None
    deadlines: dict[str, datetime] = Field(default_factory=dict)
    deadline_evidence: dict[str, Evidence] = Field(default_factory=dict)
    deadline_precision: dict[str, Literal["date", "minute", "second"]] = Field(
        default_factory=dict
    )
    latest_source_url: str | None = None
    needs_recheck: bool = True
    lot: str | None = None
    batch: str | None = None
    year: int | None = None
    first_seen_at: datetime | None = None
    updated_at: datetime | None = None


class Financing(DomainModel):
    financing_id: str
    company: str
    announced_at: datetime
    round_name: str | None = None
    financing_subtype: Literal[
        "round_equity", "strategic", "capital_increase", "merger_acquisition"
    ] | None = None
    amount_cny: float | None = Field(default=None, ge=0)
    amount_disclosed: bool = False
    business_area: str | None = None
    investors: list[str] = Field(default_factory=list)
    source_url: str
    source_urls: list[str] = Field(default_factory=list)
    source_published_at: dict[str, datetime] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    source_records: list[SourceRecord] = Field(default_factory=list)
    fingerprint: str = ""
    verification_status: VerificationStatus
    discovered_at: datetime | None = None
    content_hash: str = ""
    content_version_id: str = ""
    source_content_hashes: dict[str, str] = Field(default_factory=dict)
    source_content_version_ids: list[str] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    updated_at: datetime | None = None


class PendingItem(DomainModel):
    item_id: str
    title: str
    summary: str = ""
    reason: str
    source_url: str
    discovered_at: datetime
    category_hint: Category | None = None
    source_published_at: datetime | None = None


class RunMetrics(DomainModel):
    started_at: datetime
    finished_at: datetime | None = None
    sources_checked: int = Field(default=0, ge=0)
    events_created: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)
    search_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    raw_search_count: int = Field(default=0, ge=0)
    valid_shape_count: int = Field(default=0, ge=0)
    relevance_pass_count: int = Field(default=0, ge=0)
    recent_7d_count: int = Field(default=0, ge=0)
    fallback_8_30d_count: int = Field(default=0, ge=0)
    fallback_window_days: int = Field(default=30, ge=8, le=90)
    unknown_date_count: int = Field(default=0, ge=0)
    final_candidate_count: int = Field(default=0, ge=0)
    fetch_failure_count: int = Field(default=0, ge=0)
    information_available: bool = False
    official_candidate_count: int = Field(default=0, ge=0)
    verified_count: int = Field(default=0, ge=0)
    pending_count: int = Field(default=0, ge=0)
    new_project_count: int = Field(default=0, ge=0)
    status_update_count: int = Field(default=0, ge=0)
    deduplicated_count: int = Field(default=0, ge=0)
    failed_domains: list[str] = Field(default_factory=list)
    deepseek_tokens: int = Field(default=0, ge=0)
    search_api_usage: int = Field(default=0, ge=0)
    search_failure_reasons: list[str] = Field(default_factory=list)
    model_coverage_degraded: bool = False
    search_coverage_degraded: bool = False
    search_budget: int = Field(default=0, ge=0)
    search_budget_used: int = Field(default=0, ge=0)
    agent_round_count: int = Field(default=0, ge=0)
    duplicate_query_count: int = Field(default=0, ge=0)
    event_filter_rejected_count: int = Field(default=0, ge=0)
    event_duplicate_count: int = Field(default=0, ge=0)
    agent_search_degraded: bool = False
    agent_stop_reason: str = ""


class StateBundle(DomainModel):
    schema_version: Literal[2] = 2
    events: list[Event] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    financings: list[Financing] = Field(default_factory=list)
    pending: list[PendingItem] = Field(default_factory=list)
