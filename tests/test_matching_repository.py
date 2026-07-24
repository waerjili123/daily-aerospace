from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from laser_space_daily.matching import (
    ProjectMatcher,
    event_fingerprint,
    financing_fingerprint,
    stable_event_id,
    stable_project_id,
)
from laser_space_daily.models import (
    AnalysisResult,
    Category,
    Event,
    EventType,
    Financing,
    Project,
    SourceGrade,
    StateBundle,
    VerificationStatus,
)
from laser_space_daily.repository import StateCorruptionError, StateRepository


NOW = datetime(2026, 5, 20, 3, 0, tzinfo=UTC)


def make_event(
    *,
    title: str = "2026年星间激光通信终端采购中标结果",
    organization: str = "中国星网",
    event_type: EventType = EventType.AWARD,
    project_codes: list[str] | None = None,
    published_at: datetime = NOW,
    source_url: str = "https://example.cn/notices/1",
    event_id: str = "event-input-id",
) -> Event:
    analysis = AnalysisResult(
        in_china=True,
        in_scope=True,
        category=Category.LASER_COMMUNICATION,
        event_type=event_type,
        title=title,
        organization=organization,
        published_at=published_at,
        project_codes=project_codes or [],
        source_url=source_url,
    )
    return Event(
        event_id=event_id,
        category=Category.LASER_COMMUNICATION,
        title=title,
        organization=organization,
        published_at=published_at,
        source_url=source_url,
        source_grade=SourceGrade.A,
        verification_status=VerificationStatus.VERIFIED,
        event_type=event_type,
        analysis=analysis,
    )


@pytest.fixture
def existing_project() -> Project:
    return Project(
        project_id="project-1",
        name="2026年星间激光通信终端采购",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
        project_codes=["XW-LASER-2026-01"],
        first_published_at=datetime(2026, 2, 1, tzinfo=UTC),
        latest_event_at=datetime(2026, 4, 1, tzinfo=UTC),
        year=2026,
        batch="1",
        lot="1",
    )


def test_exact_project_code_wins(existing_project: Project) -> None:
    event = make_event(project_codes=[existing_project.project_codes[0]])

    decision = ProjectMatcher().match(event, [existing_project])

    assert decision.project_id == existing_project.project_id
    assert decision.reason == "exact_project_code"
    assert decision.relation == "same_project"


def test_exact_same_buyer_and_core_title_matches() -> None:
    project = Project(
        project_id="project-1",
        name="星间激光通信终端采购",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
        latest_event_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    event = make_event(title="星间激光通信终端采购中标结果")

    decision = ProjectMatcher().match(event, [project])

    assert decision.relation == "same_project"
    assert decision.reason == "same_buyer_title"


def test_similarity_at_point_nine_matches_with_valid_timing() -> None:
    project = Project(
        project_id="project-1",
        name="abcdefghij",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
        latest_event_at=datetime(2026, 4, 1, tzinfo=UTC),
    )

    decision = ProjectMatcher().match(make_event(title="abcdefghiX"), [project])

    assert decision.score == pytest.approx(0.9)
    assert decision.relation == "same_project"


def test_medium_similarity_is_suspected_not_merged() -> None:
    project = Project(
        project_id="project-1",
        name="abcdefghij",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
    )

    decision = ProjectMatcher().match(make_event(title="abcdefghXY"), [project])

    assert decision.score == pytest.approx(0.8)
    assert decision.relation == "suspected"
    assert decision.project_id == project.project_id


@pytest.mark.parametrize(
    ("field", "value", "title"),
    [
        ("year", 2025, "2026年星间激光通信终端采购中标结果"),
        ("batch", "2", "星间激光通信终端第1批采购中标结果"),
        ("lot", "2", "星间激光通信终端第1标段采购中标结果"),
    ],
)
def test_different_year_batch_or_lot_is_new_project(
    field: str, value: object, title: str
) -> None:
    project = Project(
        project_id="project-1",
        name=title.replace("中标结果", ""),
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
        **{field: value},
    )

    decision = ProjectMatcher().match(make_event(title=title), [project])

    assert decision.relation == "new_project"
    assert decision.reason == f"conflicting_{field}"


def test_rebid_links_to_original_but_different_lot_does_not(
    existing_project: Project,
) -> None:
    rebid = make_event(
        title="2026年星间激光通信终端采购重新招标",
        event_type=EventType.REBID,
        project_codes=["XW-LASER-2026-01"],
    )
    lot_two = make_event(
        title="2026年星间激光通信终端采购第2标段重新招标",
        event_type=EventType.REBID,
        project_codes=["XW-LASER-2026-01"],
    )

    assert ProjectMatcher().match(rebid, [existing_project]).relation == "same_project"
    assert ProjectMatcher().match(lot_two, [existing_project]).relation == "new_project"


def test_failed_project_rebid_links_when_original_code_aligns(
    existing_project: Project,
) -> None:
    project = existing_project.model_copy(
        update={"current_stage": EventType.FAILED, "status": "failed"}
    )
    rebid = make_event(
        title="2026年星间激光通信终端采购二次招标",
        event_type=EventType.REBID,
        project_codes=["XW-LASER-2026-01"],
    )

    assert ProjectMatcher().match(rebid, [project]).relation == "same_project"


@pytest.mark.parametrize("suffix", ["-R2", "_R2", "/R2"])
def test_rebid_derived_code_suffix_links_to_original(suffix: str) -> None:
    project = Project(
        project_id="project-1",
        name="激光通信终端采购",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="failed",
        project_codes=["LASER-2026-01"],
    )
    rebid = make_event(
        title="激光通信终端采购重新招标",
        event_type=EventType.REBID,
        project_codes=[f"LASER-2026-01{suffix}"],
    )

    decision = ProjectMatcher().match(rebid, [project])

    assert decision.relation == "same_project"
    assert decision.project_id == project.project_id


def test_rebid_code_aligned_candidate_is_not_masked_by_tied_candidate() -> None:
    unaligned = Project(
        project_id="a-unaligned",
        name="激光通信终端采购",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
        project_codes=["OTHER-2026"],
    )
    aligned = unaligned.model_copy(
        update={"project_id": "z-aligned", "project_codes": ["LASER-2026-01"]}
    )
    rebid = make_event(
        title="激光通信终端采购重新招标",
        event_type=EventType.REBID,
        project_codes=["LASER-2026-01-R2"],
    )

    decision = ProjectMatcher().match(rebid, [unaligned, aligned])

    assert decision.relation == "same_project"
    assert decision.project_id == aligned.project_id


def test_rebid_without_original_code_alignment_is_not_auto_merged(
    existing_project: Project,
) -> None:
    rebid = make_event(
        title="2026年星间激光通信终端采购重新招标",
        event_type=EventType.REBID,
        project_codes=["OTHER-2026"],
    )

    assert ProjectMatcher().match(rebid, [existing_project]).relation == "suspected"


def test_rebid_with_same_code_but_unrelated_core_title_is_not_auto_merged(
    existing_project: Project,
) -> None:
    rebid = make_event(
        title="2026年高分辨率相机采购重新招标",
        event_type=EventType.REBID,
        project_codes=existing_project.project_codes,
    )

    assert ProjectMatcher().match(rebid, [existing_project]).relation != "same_project"


def test_rebid_point_nine_title_with_aligned_code_is_only_suspected() -> None:
    project = Project(
        project_id="project-1",
        name="abcdefghij",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="failed",
        project_codes=["LASER-2026-01"],
    )
    rebid = make_event(
        title="abcdefghiX重新招标",
        event_type=EventType.REBID,
        project_codes=["LASER-2026-01-R2"],
    )

    decision = ProjectMatcher().match(rebid, [project])

    assert decision.score == pytest.approx(0.9)
    assert decision.relation == "suspected"
    assert decision.project_id == project.project_id
    assert decision.reason == "rebid_core_title_mismatch"


def test_standalone_second_marker_requires_rebid_code_alignment() -> None:
    project = Project(
        project_id="project-1",
        name="激光通信终端采购",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="failed",
    )
    rebid = make_event(
        title="激光通信终端采购二次",
        event_type=EventType.AWARD,
    )

    decision = ProjectMatcher().match(rebid, [project])

    assert decision.relation == "suspected"
    assert decision.reason == "rebid_code_unaligned"


@pytest.mark.parametrize(
    "title",
    [
        "二次星间激光通信终端综合测试设备采购项目",
        "二次 星间激光通信终端综合测试设备采购项目",
        "二次-星间激光通信终端综合测试设备采购项目",
    ],
)
def test_bare_second_prefix_is_rebid_and_cannot_similarity_auto_merge(
    title: str,
) -> None:
    project = Project(
        project_id="project-1",
        name="星间激光通信终端综合测试设备采购项目",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="failed",
    )

    decision = ProjectMatcher().match(make_event(title=title), [project])

    assert decision.relation == "suspected"
    assert decision.reason == "rebid_code_unaligned"


def test_lifecycle_token_is_not_removed_from_internal_title_text() -> None:
    project = Project(
        project_id="project-1",
        name="元激光设备采购中标结果",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
    )

    decision = ProjectMatcher().match(
        make_event(title="二次元激光设备采购"), [project]
    )

    assert decision.relation != "same_project"


@pytest.mark.parametrize(
    ("project_title", "event_title"),
    [
        (
            "超长星间激光通信终端综合测试设备采购项目 LOT 1",
            "超长星间激光通信终端综合测试设备采购项目 LOT 2中标结果",
        ),
        (
            "超长星间激光通信终端综合测试设备采购项目 lot-1",
            "超长星间激光通信终端综合测试设备采购项目 lot-2中标结果",
        ),
        ("激光终端第1标段采购", "激光终端标段2采购中标结果"),
        ("激光终端第1包采购", "激光终端包2采购中标结果"),
        ("激光终端第一标段采购", "激光终端标段二采购中标结果"),
    ],
)
def test_conflicting_lot_forms_guard_before_similarity(
    project_title: str, event_title: str
) -> None:
    project = Project(
        project_id="project-1",
        name=project_title,
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
    )

    decision = ProjectMatcher().match(make_event(title=event_title), [project])

    assert decision.relation == "new_project"
    assert decision.reason == "conflicting_lot"


def test_equivalent_chinese_and_arabic_lot_markers_do_not_conflict() -> None:
    project = Project(
        project_id="project-1",
        name="激光终端第一标段采购",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
        lot="一",
    )

    decision = ProjectMatcher().match(
        make_event(title="激光终端第1标段采购中标结果"), [project]
    )

    assert decision.reason != "conflicting_lot"


def test_same_title_different_buyer_is_new_project(existing_project: Project) -> None:
    event = make_event(
        title=existing_project.name,
        organization="另一采购单位",
        project_codes=existing_project.project_codes,
    )

    decision = ProjectMatcher().match(event, [existing_project])

    assert decision.relation == "new_project"
    assert decision.project_id is None


def test_exact_code_cannot_merge_across_categories(existing_project: Project) -> None:
    base = make_event(project_codes=existing_project.project_codes)
    event = base.model_copy(
        update={
            "category": Category.LASER_WEAPON,
            "analysis": base.analysis.model_copy(
                update={"category": Category.LASER_WEAPON}
            ),
        }
    )

    decision = ProjectMatcher().match(event, [existing_project])

    assert decision.relation == "new_project"
    assert decision.reason == "category_mismatch"


def test_title_without_year_uses_publication_year_guard(
    existing_project: Project,
) -> None:
    event = make_event(
        title="星间激光通信终端采购中标结果",
        published_at=datetime(2027, 2, 1, tzinfo=UTC),
    )

    decision = ProjectMatcher().match(event, [existing_project])

    assert decision.relation == "new_project"
    assert decision.reason == "conflicting_year"


def test_exact_code_overrides_only_inferred_incoming_year_conflict(
    existing_project: Project,
) -> None:
    event = make_event(
        title="星间激光通信终端采购中标结果",
        project_codes=existing_project.project_codes,
        published_at=datetime(2027, 2, 1, tzinfo=UTC),
    )

    decision = ProjectMatcher().match(event, [existing_project])

    assert decision.relation == "same_project"
    assert decision.reason == "exact_project_code"


def test_year_guard_falls_back_to_project_event_dates() -> None:
    project = Project(
        project_id="project-1",
        name="星间激光通信终端采购",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
        first_published_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    event = make_event(
        title="星间激光通信终端采购中标结果",
        published_at=datetime(2027, 2, 1, tzinfo=UTC),
    )

    decision = ProjectMatcher().match(event, [project])

    assert decision.relation == "new_project"
    assert decision.reason == "conflicting_year"


def test_lifecycle_timing_must_be_nondecreasing_and_within_eighteen_months() -> None:
    project = Project(
        project_id="project-1",
        name="abcdefghij",
        organization="中国星网",
        category=Category.LASER_COMMUNICATION,
        status="active",
        latest_event_at=NOW,
    )
    older = make_event(title="abcdefghiX", published_at=datetime(2026, 5, 19, tzinfo=UTC))
    too_late = make_event(title="abcdefghiX", published_at=datetime(2028, 1, 1, tzinfo=UTC))

    assert ProjectMatcher().match(older, [project]).relation == "suspected"
    too_late_decision = ProjectMatcher().match(too_late, [project])
    assert too_late_decision.relation == "new_project"
    assert too_late_decision.reason == "conflicting_year"


def test_event_fingerprint_and_ids_are_stable_under_evidence_and_url_noise() -> None:
    first = make_event(
        source_url="HTTPS://Example.CN:443/notices/1?utm_source=x&b=2&a=1#top"
    )
    second = make_event(source_url="https://example.cn/notices/1?a=1&b=2")
    second = second.model_copy(update={"evidence": list(reversed(first.evidence))})

    assert event_fingerprint(first) == event_fingerprint(second)
    assert stable_event_id(first) == stable_event_id(second)
    assert stable_project_id(first) == stable_project_id(second)


def make_financing(**updates: object) -> Financing:
    values: dict[str, object] = {
        "financing_id": "financing-1",
        "company": " 星河 动力 ",
        "announced_at": NOW,
        "round_name": "C轮",
        "amount_cny": 1_000_000_000,
        "amount_disclosed": True,
        "investors": ["投资方乙", "投资方甲"],
        "source_url": "https://media.example/1",
        "source_urls": ["https://media.example/2", "https://media.example/1"],
        "verification_status": VerificationStatus.VERIFIED,
    }
    values.update(updates)
    return Financing(**values)


def test_financing_fingerprint_ignores_source_and_investor_order() -> None:
    first = make_financing()
    second = make_financing(
        investors=["投资方甲", "投资方乙"],
        source_url="https://another.example/repost",
        source_urls=list(reversed(first.source_urls)),
    )

    assert financing_fingerprint(first) == financing_fingerprint(second)


def test_financing_fingerprint_uses_announcement_date_not_time() -> None:
    morning = make_financing(announced_at=datetime(2026, 5, 20, 1, 0, tzinfo=UTC))
    evening = make_financing(announced_at=datetime(2026, 5, 20, 22, 0, tzinfo=UTC))

    assert financing_fingerprint(morning) == financing_fingerprint(evening)


def test_missing_repository_files_load_empty_state(tmp_path: Path) -> None:
    assert StateRepository(tmp_path).load() == StateBundle()


def test_repository_same_event_twice_is_idempotent(tmp_path: Path) -> None:
    repo = StateRepository(tmp_path)
    event = make_event()

    repo.append_event(event)
    repo.append_event(event)

    assert len(repo.load().events) == 1


def test_repository_financing_reposts_dedupe_by_fingerprint(tmp_path: Path) -> None:
    repo = StateRepository(tmp_path)
    first = make_financing()
    repost = make_financing(
        financing_id="financing-repost",
        source_url="https://another.example/repost",
    )

    repo.commit(StateBundle(financings=[first, repost]))

    assert len(repo.load().financings) == 1


def test_repository_round_trips_financing_source_publication_dates(
    tmp_path: Path,
) -> None:
    repo = StateRepository(tmp_path)
    source_dates = {
        "https://media.example/1": datetime(2026, 5, 20, tzinfo=UTC),
        "https://media.example/2": datetime(2026, 6, 1, tzinfo=UTC),
    }

    repo.commit(
        StateBundle(
            financings=[make_financing(source_published_at=source_dates)]
        )
    )

    assert repo.load().financings[0].source_published_at == source_dates


def test_repository_commit_is_deterministic_and_stably_sorted(tmp_path: Path) -> None:
    later = make_event(event_id="z", published_at=datetime(2026, 6, 1, tzinfo=UTC))
    earlier = make_event(event_id="a", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    state = StateBundle(
        events=[later, earlier],
        projects=[
            Project(
                project_id="z",
                name="中文项目",
                organization="单位",
                category=Category.LASER_COMMUNICATION,
                status="active",
            ),
            Project(
                project_id="a",
                name="较早项目",
                organization="单位",
                category=Category.LASER_COMMUNICATION,
                status="active",
            ),
        ],
    )
    repo = StateRepository(tmp_path)

    repo.commit(state)
    first = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    repo.commit(state)
    second = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    assert first == second
    assert "中文项目" in (tmp_path / "projects.json").read_text(encoding="utf-8")
    assert [event.event_id for event in repo.load().events] == ["a", "z"]
    assert [project.project_id for project in repo.load().projects] == ["a", "z"]


def test_repository_sort_is_total_for_naive_and_aware_datetimes(tmp_path: Path) -> None:
    naive_earlier = make_event(
        event_id="naive",
        published_at=datetime(2026, 1, 1),
        source_url="https://example.cn/naive",
    )
    aware_later = make_event(
        event_id="aware",
        published_at=datetime(2026, 2, 1, tzinfo=UTC),
        source_url="https://example.cn/aware",
    )
    repo = StateRepository(tmp_path)

    repo.commit(StateBundle(events=[aware_later, naive_earlier]))

    assert [event.event_id for event in repo.load().events] == ["naive", "aware"]


@pytest.mark.parametrize(
    ("filename", "content", "line_number"),
    [
        ("projects.json", "{broken", None),
        ("events.jsonl", "{}\n{broken\n", 1),
        ("events.jsonl", "\n{broken\n", 2),
    ],
)
def test_corrupt_json_or_jsonl_raises_with_location(
    tmp_path: Path, filename: str, content: str, line_number: int | None
) -> None:
    (tmp_path / filename).write_text(content, encoding="utf-8")

    with pytest.raises(StateCorruptionError) as caught:
        StateRepository(tmp_path).load()

    assert caught.value.path == tmp_path / filename
    assert caught.value.line_number == line_number


def test_repository_rejects_nonformal_or_unverified_events(tmp_path: Path) -> None:
    event = make_event().model_copy(
        update={
            "formal_record": False,
            "verification_status": VerificationStatus.PENDING,
        }
    )

    with pytest.raises(ValueError, match="VERIFIED formal"):
        StateRepository(tmp_path).commit(StateBundle(events=[event]))


def test_failed_commit_cleans_temps_and_retains_original_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = StateRepository(tmp_path)
    original = StateBundle(projects=[])
    repo.commit(original)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    real_replace = Path.replace
    calls = 0

    def fail_second_replace(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replace failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_replace)
    changed = StateBundle(
        projects=[
            Project(
                project_id="new",
                name="新项目",
                organization="单位",
                category=Category.LASER_COMMUNICATION,
                status="active",
            )
        ]
    )

    with pytest.raises(OSError, match="simulated"):
        repo.commit(changed)

    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == before
    assert not list(tmp_path.glob("*.tmp"))


def test_repository_initializes_all_files_with_git_friendly_json(tmp_path: Path) -> None:
    StateRepository(tmp_path).commit(StateBundle())

    assert (tmp_path / "events.jsonl").read_bytes() == b""
    for filename in ("projects.json", "financings.json", "pending.json"):
        assert (tmp_path / filename).read_bytes() == b"[]\n"
        assert json.loads((tmp_path / filename).read_text(encoding="utf-8")) == []
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {
        "schema_version": 2
    }


def test_repository_migrates_v1_state_to_schema_v2_content_history(
    tmp_path: Path,
) -> None:
    legacy_event = make_event()
    legacy_project = Project(
        project_id="legacy-project",
        name=legacy_event.title,
        organization=legacy_event.organization,
        category=legacy_event.category,
        status="awarded",
        event_ids=[legacy_event.event_id],
        first_published_at=legacy_event.published_at,
        latest_event_at=legacy_event.published_at,
    )
    (tmp_path / "events.jsonl").write_text(
        legacy_event.model_dump_json(
            exclude={
                "discovered_at",
                "content_hash",
                "content_version_id",
                "first_seen_at",
                "updated_at",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "projects.json").write_text(
        json.dumps(
            [
                legacy_project.model_dump(
                    mode="json", exclude={"first_seen_at", "updated_at"}
                )
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "financings.json").write_text("[]", encoding="utf-8")
    (tmp_path / "pending.json").write_text("[]", encoding="utf-8")

    repo = StateRepository(tmp_path)
    migrated = repo.load()

    assert migrated.schema_version == 2
    event = migrated.events[0]
    assert event.discovered_at == legacy_event.published_at
    assert event.first_seen_at == legacy_event.published_at
    assert event.updated_at == legacy_event.published_at
    assert len(event.content_hash) == 64
    assert event.content_version_id
    project = migrated.projects[0]
    assert project.first_seen_at == legacy_event.published_at
    assert project.updated_at == legacy_event.published_at

    repo.commit(migrated)

    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {
        "schema_version": 2
    }
    assert repo.load() == migrated
