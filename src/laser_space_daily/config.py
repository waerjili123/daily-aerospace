"""Application configuration loaded from non-secret YAML and environment secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BochaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=30, ge=1)


class DiscoverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_queries: int = Field(default=40, ge=0)
    fetch_timeout_seconds: float = Field(default=15.0, gt=0)


class DeepSeekSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=60, ge=1)
    base_url: str = "https://api.deepseek.com"
    flash_model: str = "deepseek-v4-flash"
    pro_model: str = "deepseek-v4-pro"


class ReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chars: int = Field(default=18000, ge=1)


class NotifierSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=15.0, gt=0)


class FinancingSourcesSettings(BaseModel):
    """Curated financing sources shared by discovery and formal verification."""

    model_config = ConfigDict(extra="forbid")

    official_company_domains: dict[str, str | list[str]]
    official_investor_domains: dict[str, str | list[str]] = Field(default_factory=dict)
    independent_media_domains: list[str]

    @field_validator("official_company_domains")
    @classmethod
    def validate_company_domains(
        cls, value: dict[str, str | list[str]]
    ) -> dict[str, str | list[str]]:
        if not value:
            raise ValueError("financing official company registry must not be empty")
        normalized: dict[str, str | list[str]] = {}
        for domain, company in value.items():
            host = cls._domain(domain)
            aliases = [company] if isinstance(company, str) else company
            aliases = list(
                dict.fromkeys(alias.strip() for alias in aliases if alias.strip())
            )
            if not aliases:
                raise ValueError("financing company name must not be empty")
            normalized[host] = aliases[0] if isinstance(company, str) else aliases
        return normalized

    @field_validator("official_investor_domains")
    @classmethod
    def validate_investor_domains(
        cls, value: dict[str, str | list[str]]
    ) -> dict[str, str | list[str]]:
        normalized: dict[str, str | list[str]] = {}
        for domain, investor in value.items():
            host = cls._domain(domain)
            aliases = [investor] if isinstance(investor, str) else investor
            aliases = list(
                dict.fromkeys(alias.strip() for alias in aliases if alias.strip())
            )
            if not aliases:
                raise ValueError("financing investor aliases must not be empty")
            normalized[host] = aliases[0] if isinstance(investor, str) else aliases
        return normalized

    @field_validator("independent_media_domains")
    @classmethod
    def validate_media_domains(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("financing independent media registry must not be empty")
        return list(dict.fromkeys(cls._domain(domain) for domain in value))

    @model_validator(mode="after")
    def registries_must_be_disjoint(self) -> "FinancingSourcesSettings":
        company = set(self.official_company_domains)
        investor = set(self.official_investor_domains)
        media = set(self.independent_media_domains)
        overlap = (company & investor) | (company & media) | (investor & media)
        if overlap:
            raise ValueError("financing source registries must be disjoint")
        return self

    @staticmethod
    def _domain(value: str) -> str:
        normalized = value.strip().lower().rstrip(".")
        if (
            not normalized
            or "://" in normalized
            or "/" in normalized
            or normalized.startswith(".")
        ):
            raise ValueError(f"invalid financing source domain: {value}")
        return normalized


class Settings(BaseModel):
    """All runtime settings; secret fields are populated only from environment."""

    model_config = ConfigDict(extra="forbid")

    deepseek_api_key: str
    bocha_api_key: str
    dingtalk_webhook: str
    dingtalk_secret: str
    bocha: BochaSettings = Field(default_factory=BochaSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    deepseek: DeepSeekSettings = Field(default_factory=DeepSeekSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)
    notifier: NotifierSettings = Field(default_factory=NotifierSettings)
    data_dir: Path = Path("data")
    report_dir: Path = Path("reports")
    official_sources_path: Path = Path("config/official_sources.yaml")
    financing_sources: FinancingSourcesSettings


_SECRET_ENVIRONMENT_VARIABLES = (
    "DEEPSEEK_API_KEY",
    "BOCHA_API_KEY",
    "DINGTALK_WEBHOOK",
    "DINGTALK_SECRET",
)


def load_settings(path: Path) -> Settings:
    """Load non-secret YAML settings and require each secret from the environment."""
    raw_config: Any
    with path.open(encoding="utf-8") as config_file:
        raw_config = yaml.safe_load(config_file) or {}
    if not isinstance(raw_config, dict):
        raise ValueError("configuration must be a mapping")

    secrets: dict[str, str] = {}
    for environment_name in _SECRET_ENVIRONMENT_VARIABLES:
        value = os.getenv(environment_name)
        if not value:
            raise ValueError(f"missing required environment variable: {environment_name}")
        secrets[environment_name.lower()] = value

    return Settings.model_validate({**raw_config, **secrets})
