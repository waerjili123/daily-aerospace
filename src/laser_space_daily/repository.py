"""Atomic, deterministic JSON/JSONL persistence for pipeline state."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, TypeVar

from pydantic import ValidationError

from .matching import content_version_id, event_fingerprint, financing_fingerprint
from .models import (
    Event,
    Financing,
    PendingItem,
    Project,
    StateBundle,
    VerificationStatus,
)


class StateCorruptionError(RuntimeError):
    def __init__(self, path: Path, line_number: int | None = None) -> None:
        self.path = path
        self.line_number = line_number
        location = f" line {line_number}" if line_number is not None else ""
        super().__init__(f"corrupt state file {path}{location}")


_ModelT = TypeVar("_ModelT", Event, Project, Financing, PendingItem)


class StateRepository:
    STATE = "state.json"
    EVENTS = "events.jsonl"
    PROJECTS = "projects.json"
    FINANCINGS = "financings.json"
    PENDING = "pending.json"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def load(self) -> StateBundle:
        self._load_schema_version(self.root / self.STATE)
        events = self._load_events(self.root / self.EVENTS)
        projects = self._load_array(self.root / self.PROJECTS, Project)
        financings = self._load_array(self.root / self.FINANCINGS, Financing)
        pending = self._load_array(self.root / self.PENDING, PendingItem)
        return self._canonicalize(
            self._migrate_v2(
                StateBundle(
                events=events,
                projects=projects,
                financings=financings,
                pending=pending,
                )
            )
        )

    def append_event(self, event: Event) -> None:
        state = self.load()
        self.commit(state.model_copy(update={"events": [*state.events, event]}))

    def commit(self, bundle: StateBundle) -> None:
        for event in bundle.events:
            if (
                not event.formal_record
                or event.verification_status is not VerificationStatus.VERIFIED
            ):
                raise ValueError("repository accepts only VERIFIED formal events")

        state = self._canonicalize(self._migrate_v2(bundle))
        payloads = {
            self.STATE: self._state_json(),
            self.EVENTS: self._events_jsonl(state.events),
            self.PROJECTS: self._array_json(state.projects),
            self.FINANCINGS: self._array_json(state.financings),
            self.PENDING: self._array_json(state.pending),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        originals = {
            filename: self._snapshot(self.root / filename) for filename in payloads
        }
        temps: dict[str, Path] = {}
        replaced: list[str] = []
        rollback_temps: list[Path] = []
        try:
            for filename, payload in payloads.items():
                temp = self.root / f".{filename}.tmp"
                temps[filename] = temp
                self._write_synced(temp, payload)
            for filename in payloads:
                temps[filename].replace(self.root / filename)
                replaced.append(filename)
        except Exception:
            for filename in replaced:
                target = self.root / filename
                original = originals[filename]
                if original is None:
                    target.unlink(missing_ok=True)
                    continue
                rollback = self.root / f".{filename}.rollback.tmp"
                rollback_temps.append(rollback)
                self._write_synced(rollback, original)
                os.replace(rollback, target)
            raise
        finally:
            for temp in [*temps.values(), *rollback_temps]:
                temp.unlink(missing_ok=True)

    def _load_events(self, path: Path) -> list[Event]:
        if not path.exists():
            return []
        events: list[Event] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        events.append(Event.model_validate_json(line))
                    except (ValueError, ValidationError, json.JSONDecodeError) as error:
                        raise StateCorruptionError(path, line_number) from error
        except (OSError, UnicodeError) as error:
            raise StateCorruptionError(path) from error
        return events

    def _load_array(self, path: Path, model: type[_ModelT]) -> list[_ModelT]:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise TypeError("state JSON must contain an array")
            return [model.model_validate(item) for item in raw]
        except (OSError, UnicodeError, ValueError, TypeError, ValidationError) as error:
            raise StateCorruptionError(path) from error

    @staticmethod
    def _load_schema_version(path: Path) -> int:
        if not path.exists():
            return 1
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            version = raw.get("schema_version") if isinstance(raw, dict) else None
            if version not in {1, 2}:
                raise ValueError("unsupported state schema")
            return int(version)
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise StateCorruptionError(path) from error

    @staticmethod
    def _snapshot(path: Path) -> bytes | None:
        return path.read_bytes() if path.exists() else None

    @staticmethod
    def _write_synced(path: Path, payload: bytes) -> None:
        with path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _dump(model: Any) -> dict[str, Any]:
        return model.model_dump(mode="json")

    @classmethod
    def _stable_model_json(cls, model: Any) -> str:
        return json.dumps(
            cls._dump(model), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _sortable_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @classmethod
    def _events_jsonl(cls, events: Iterable[Event]) -> bytes:
        lines = [cls._stable_model_json(event) for event in events]
        return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")

    @staticmethod
    def _state_json() -> bytes:
        return b'{"schema_version":2}\n'

    @classmethod
    def _array_json(cls, models: Iterable[Any]) -> bytes:
        payload = [cls._dump(model) for model in models]
        return (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")

    @classmethod
    def _migrate_v2(cls, bundle: StateBundle) -> StateBundle:
        events = []
        for event in bundle.events:
            content_hash = event.content_hash or cls._legacy_content_hash(
                {
                    "event_type": event.event_type.value,
                    "organization": event.organization,
                    "published_at": cls._dump(event)["published_at"],
                    "source_url": event.source_url,
                    "title": event.title,
                }
            )
            discovered_at = event.discovered_at or event.published_at
            first_seen_at = event.first_seen_at or discovered_at
            events.append(
                event.model_copy(
                    update={
                        "discovered_at": discovered_at,
                        "content_hash": content_hash,
                        "content_version_id": event.content_version_id
                        or content_version_id(event.source_url, content_hash),
                        "first_seen_at": first_seen_at,
                        "updated_at": event.updated_at or first_seen_at,
                    }
                )
            )

        projects = []
        for project in bundle.projects:
            first_seen_at = (
                project.first_seen_at
                or project.first_published_at
                or project.latest_event_at
            )
            projects.append(
                project.model_copy(
                    update={
                        "first_seen_at": first_seen_at,
                        "updated_at": project.updated_at
                        or project.latest_event_at
                        or first_seen_at,
                    }
                )
            )

        financings = []
        for financing in bundle.financings:
            content_hash = financing.content_hash or cls._legacy_content_hash(
                {
                    "announced_at": cls._dump(financing)["announced_at"],
                    "company": financing.company,
                    "round_name": financing.round_name,
                    "source_url": financing.source_url,
                }
            )
            primary_version = financing.content_version_id or content_version_id(
                financing.source_url, content_hash
            )
            source_hashes = dict(financing.source_content_hashes)
            source_hashes.setdefault(financing.source_url, content_hash)
            source_versions = set(financing.source_content_version_ids)
            source_versions.add(primary_version)
            migrated_records = []
            for record in financing.source_records:
                record_version = record.content_version_id or content_version_id(
                    record.source_url, record.content_hash
                )
                migrated_records.append(
                    record.model_copy(update={"content_version_id": record_version})
                )
                source_hashes.setdefault(record.source_url, record.content_hash)
                source_versions.add(record_version)
            discovered_at = financing.discovered_at or financing.announced_at
            first_seen_at = financing.first_seen_at or discovered_at
            financings.append(
                financing.model_copy(
                    update={
                        "discovered_at": discovered_at,
                        "content_hash": content_hash,
                        "content_version_id": primary_version,
                        "source_content_hashes": source_hashes,
                        "source_content_version_ids": sorted(source_versions),
                        "source_records": migrated_records,
                        "first_seen_at": first_seen_at,
                        "updated_at": financing.updated_at or first_seen_at,
                    }
                )
            )
        return StateBundle(
            events=events,
            projects=projects,
            financings=financings,
            pending=bundle.pending,
        )

    @staticmethod
    def _legacy_content_hash(payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def _canonicalize(cls, bundle: StateBundle) -> StateBundle:
        events = sorted(
            bundle.events,
            key=lambda item: (
                cls._sortable_datetime(item.published_at),
                item.event_id,
                cls._stable_model_json(item),
            ),
        )
        unique_events: list[Event] = []
        event_ids: set[str] = set()
        event_fingerprints: set[str] = set()
        for event in events:
            fingerprint = event_fingerprint(event)
            if event.event_id in event_ids or fingerprint in event_fingerprints:
                continue
            event_ids.add(event.event_id)
            event_fingerprints.add(fingerprint)
            unique_events.append(event)

        financings_with_fingerprint = [
            financing.model_copy(
                update={"fingerprint": financing_fingerprint(financing)}
            )
            for financing in bundle.financings
        ]
        financings_with_fingerprint.sort(
            key=lambda item: (
                item.fingerprint,
                item.financing_id,
                cls._stable_model_json(item),
            )
        )
        unique_financings: list[Financing] = []
        seen_financings: set[str] = set()
        for financing in financings_with_fingerprint:
            if financing.fingerprint in seen_financings:
                continue
            seen_financings.add(financing.fingerprint)
            unique_financings.append(financing)

        return StateBundle(
            events=unique_events,
            projects=sorted(bundle.projects, key=lambda item: item.project_id),
            financings=unique_financings,
            pending=sorted(bundle.pending, key=lambda item: item.item_id),
        )
