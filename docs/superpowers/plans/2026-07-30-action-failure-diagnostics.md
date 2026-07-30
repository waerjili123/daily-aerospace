# Actions Failure Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Project policy prohibits subagent-driven execution.

**Goal:** Make failed workflow runs expose a secret-safe code location and prevent stale reports or repository data from appearing as current-run output.

**Architecture:** The CLI will maintain two mutually exclusive run metadata files under `data/`: a failure diagnostic containing a stable stage, exception type, timestamp, and repository-only stack frames; and a success manifest containing the exact report path. The workflow will use its own job status plus those files to route success summaries/artifacts separately from failure summaries/artifacts.

**Tech Stack:** Python 3.13, standard-library `traceback`/`pathlib`/`json`, pytest 8.4, GitHub Actions YAML, pinned `actions/upload-artifact@v4.6.2`.

## Global Constraints

- Workflow remains manual-only: `on: workflow_dispatch`.
- Workflow continues to force `--dry-run --discovery-mode daily --max-queries 12`.
- Discovery budgets remain base 12, elastic at most 3, total at most 15.
- Keep existing exit codes: configuration 2, notification 3, pipeline 4.
- Never log exception messages, locals, environment values, HTTP headers, response bodies, model payloads, webhook values, or Secrets.
- Do not merge a PR, push this implementation without explicit authorization, change Secrets, change repository visibility, enable/suspend workflow, add scheduling, set `dry_run=false`, or send DingTalk.
- Every shell command begins with `rtk`.
- Use `apply_patch` for code and documentation edits.

---

### Task 1: Add secret-safe CLI run diagnostics

**Files:**
- Modify: `tests/test_report_notifier.py:1130-1410`
- Modify: `src/laser_space_daily/cli.py:1-415`

**Interfaces:**
- Consumes: existing `Settings.data_dir`, `_atomic_write_json`, `RenderedReport`, and stable CLI exit codes.
- Produces:
  - `_safe_traceback_frames(error: BaseException) -> list[dict[str, str | int]]`
  - `_write_failure_diagnostic(settings: Settings, stage: str, error: BaseException, now: datetime) -> None`
  - `_write_run_result(settings: Settings, report_path: Path, now: datetime) -> None`
  - `data/failure-diagnostics.json` on failure
  - `data/run-result.json` on success

- [ ] **Step 1: Write failing tests for safe failure diagnostics**

Add tests that raise a `TypeError` whose message and local variables contain markers such as
`secret-query-token` and `https://example.invalid/?access_token=secret-query-token`.
Assert:

```python
diagnostic_path = cli_deps.settings.data_dir / "failure-diagnostics.json"
payload = json.loads(diagnostic_path.read_text(encoding="utf-8"))

assert code == 4
assert payload["schema_version"] == 1
assert payload["status"] == "failure"
assert payload["stage"] == "pipeline_run"
assert payload["error_type"] == "TypeError"
assert payload["occurred_at"] == "2026-07-22T07:30:00+08:00"
assert payload["frames"]
assert all(set(frame) == {"path", "line", "function"} for frame in payload["frames"])
assert all(not Path(frame["path"]).is_absolute() for frame in payload["frames"])

serialized = diagnostic_path.read_text(encoding="utf-8") + caplog.text
assert "secret-query-token" not in serialized
assert "access_token" not in serialized
assert "pipeline_run" in caplog.text
```

Add parameterized tests for:

```python
[
    ("pipeline_factory", "pipeline_build", 4),
    ("pipeline.run", "pipeline_run", 4),
    ("renderer.render", "report_render", 4),
    ("report write", "report_write", 4),
    ("candidate diagnostics write", "diagnostics_write", 4),
    ("notifier.send", "notification", 3),
]
```

Each case must assert the stage, exception type, and existing exit code without
asserting exception-message text.

- [ ] **Step 2: Run the new failure tests and verify RED**

Run:

```powershell
rtk python -m pytest tests/test_report_notifier.py -k "failure_diagnostic or failure_stage" -q
```

Expected: FAIL because `failure-diagnostics.json`, stable stages, and safe frames do not yet exist.

- [ ] **Step 3: Implement safe traceback projection and atomic failure metadata**

In `src/laser_space_daily/cli.py`, import `traceback` and define:

```python
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FAILURE_DIAGNOSTICS_NAME = "failure-diagnostics.json"
_RUN_RESULT_NAME = "run-result.json"


def _safe_traceback_frames(
    error: BaseException,
) -> list[dict[str, str | int]]:
    frames: list[dict[str, str | int]] = []
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename).resolve()
        try:
            relative = path.relative_to(_PROJECT_ROOT)
        except ValueError:
            continue
        if relative.suffix != ".py":
            continue
        frames.append(
            {
                "path": relative.as_posix(),
                "line": frame.lineno,
                "function": frame.name,
            }
        )
    return frames
```

Change `_log_failure` without including `str(error)`:

```python
def _log_failure(code: str, stage: str, error: BaseException) -> None:
    LOGGER.error(
        "cli_failure code=%s error=%s stage=%s frames=%s",
        code,
        type(error).__name__,
        stage,
        json.dumps(_safe_traceback_frames(error), ensure_ascii=True),
    )
```

Write the failure payload through `_atomic_write_json`:

```python
def _write_failure_diagnostic(
    settings: Settings,
    stage: str,
    error: BaseException,
    now: datetime,
) -> None:
    _atomic_write_json(
        settings.data_dir / _FAILURE_DIAGNOSTICS_NAME,
        {
            "schema_version": 1,
            "status": "failure",
            "stage": stage,
            "error_type": type(error).__name__,
            "occurred_at": now.isoformat(),
            "frames": _safe_traceback_frames(error),
        },
    )
```

Add a guarded recorder that never replaces the primary exit code:

```python
def _record_failure(
    settings: Settings,
    *,
    code: str,
    stage: str,
    error: BaseException,
    now: datetime,
) -> None:
    _log_failure(code, stage, error)
    try:
        (settings.data_dir / _RUN_RESULT_NAME).unlink(missing_ok=True)
        _write_failure_diagnostic(settings, stage, error, now)
    except Exception as diagnostic_error:
        LOGGER.error(
            "diagnostic_write_failed error=%s",
            type(diagnostic_error).__name__,
        )
```

The `diagnostic_write_failed` log must not include its exception message or traceback.

- [ ] **Step 4: Implement stable stage boundaries**

At the start of `_run_locked_cycle`, remove both generated metadata files:

```python
for name in (_FAILURE_DIAGNOSTICS_NAME, _RUN_RESULT_NAME):
    (settings.data_dir / name).unlink(missing_ok=True)
```

Execute operations with an explicit `stage` variable:

```python
stage = "pipeline_build"
pipeline = selected.pipeline_factory(settings)

stage = "pipeline_run"
result = pipeline.run(now)

stage = "report_render"
renderer = selected.renderer_factory(settings)
report = renderer.render(result)

stage = "report_write"
_atomic_write_report(report_path, report)

stage = "diagnostics_write"
_atomic_write_research_trace(...)
_atomic_write_json(...)
```

On an exception, call `_record_failure` with `code="pipeline"` and return 4.
Retain the existing special case where `ConfigurationError` during
`pipeline_build` returns 2, but record stage `pipeline_build`.

For notification failures, record `code="notification"`, stage `notification`,
and return 3. Do not create the success manifest until the dry-run is complete
or notification succeeds.

- [ ] **Step 5: Write failing tests for success manifest and stale-file cleanup**

Pre-create both metadata files with stale content, execute a successful dry-run,
and assert:

```python
assert not (cli_deps.settings.data_dir / "failure-diagnostics.json").exists()

payload = json.loads(
    (cli_deps.settings.data_dir / "run-result.json").read_text(encoding="utf-8")
)
assert payload == {
    "schema_version": 1,
    "status": "success",
    "occurred_at": "2026-07-22T07:30:00+08:00",
    "report_path": "reports/2026-07-22.md",
}
```

Run a failing cycle after a stale success file and assert
`run-result.json` no longer exists.

- [ ] **Step 6: Run success-manifest tests and verify RED**

Run:

```powershell
rtk python -m pytest tests/test_report_notifier.py -k "run_result or stale_run_metadata" -q
```

Expected: FAIL because success manifests and cleanup do not yet exist.

- [ ] **Step 7: Implement the success manifest**

Add:

```python
def _write_run_result(
    settings: Settings,
    report_path: Path,
    now: datetime,
) -> None:
    report_reference = (
        report_path.resolve()
        .relative_to(settings.report_dir.parent.resolve())
        .as_posix()
    )
    _atomic_write_json(
        settings.data_dir / _RUN_RESULT_NAME,
        {
            "schema_version": 1,
            "status": "success",
            "occurred_at": now.isoformat(),
            "report_path": report_reference,
        },
    )
```

For `--dry-run`, write the manifest after all report/data writes and immediately
before returning 0. For notification-enabled runs, write it only after
`notifier.send(report)` succeeds.

- [ ] **Step 8: Run focused CLI tests and verify GREEN**

Run:

```powershell
rtk python -m pytest tests/test_report_notifier.py -k "dry_run or pipeline_failure or config_failure or push_failure or failure_diagnostic or failure_stage or run_result or stale_run_metadata" -q
```

Expected: all selected tests PASS.

- [ ] **Step 9: Commit CLI diagnostics**

```powershell
rtk git add -- src/laser_space_daily/cli.py tests/test_report_notifier.py
rtk git commit -m "feat: add safe action failure diagnostics"
```

---

### Task 2: Split workflow success and failure outputs

**Files:**
- Modify: `tests/test_config_models.py:50-85`
- Modify: `tests/test_config_models.py:176-216`
- Modify: `.github/workflows/daily-intelligence.yml:53-89`

**Interfaces:**
- Consumes:
  - `data/run-result.json` with `status="success"` and `report_path`
  - `data/failure-diagnostics.json` with failure metadata
- Produces:
  - success summary and `daily-intelligence-report`
  - failure summary and `daily-intelligence-failure-diagnostics`

- [ ] **Step 1: Write failing workflow contract tests**

Update workflow assertions to require:

```python
pipeline_step = _workflow_step(steps, "Run daily pipeline")
assert pipeline_step["id"] == "daily_pipeline"

success_summary = _workflow_step(steps, "Publish dry-run report summary")
assert success_summary["if"] == "success()"
assert "data/run-result.json" in success_summary["run"]
assert 'glob("*.md")' not in success_summary["run"]

failure_summary = _workflow_step(steps, "Publish failure diagnostics summary")
assert failure_summary["if"] == "failure()"
assert "data/failure-diagnostics.json" in failure_summary["run"]

success_artifact = _workflow_step(steps, "Upload generated report")
assert success_artifact["if"] == "success()"
assert success_artifact["with"]["name"] == "daily-intelligence-report"

failure_artifact = _workflow_step(steps, "Upload failure diagnostics")
assert failure_artifact["if"] == "failure()"
assert failure_artifact["with"]["name"] == (
    "daily-intelligence-failure-diagnostics"
)
assert failure_artifact["with"]["path"] == "data/failure-diagnostics.json"
```

Keep existing assertions that prohibit scheduling, DingTalk Secrets, commits,
pushes, and state writeback.

- [ ] **Step 2: Run workflow tests and verify RED**

Run:

```powershell
rtk python -m pytest tests/test_config_models.py -k "workflow or restored" -q
```

Expected: FAIL because the workflow still uses `always()` and has one mixed artifact.

- [ ] **Step 3: Implement exact success-summary routing**

Give the pipeline step `id: daily_pipeline`.

Change the success summary to `if: success()` and have its Python body:

```python
from pathlib import Path
import json
import os

workspace = Path.cwd().resolve()
manifest_path = workspace / "data" / "run-result.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("status") != "success":
    raise SystemExit("current run success manifest is invalid")

report_reference = manifest.get("report_path")
if not isinstance(report_reference, str):
    raise SystemExit("current run report path is missing")

report_path = (workspace / report_reference).resolve()
reports_root = (workspace / "reports").resolve()
if not report_path.is_relative_to(reports_root) or report_path.suffix != ".md":
    raise SystemExit("current run report path is unsafe")
if not report_path.is_file():
    raise SystemExit("current run report is missing")

with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
    summary.write(report_path.read_text(encoding="utf-8"))
```

This code must not fall back to directory sorting or another report.

- [ ] **Step 4: Implement failure summary and artifact routing**

Add `Publish failure diagnostics summary` with `if: failure()` and Python that:

1. Writes heading `## Pipeline failure diagnostics`.
2. If the file is missing, writes
   `Structured diagnostics were not generated; inspect the failed step log.`
3. Otherwise reads only `stage`, `error_type`, and `frames`.
4. Renders each frame as ``path:line (function)``.
5. Never renders unknown keys.

Change `Upload generated report` to `if: success()`.

Add:

```yaml
- name: Upload failure diagnostics
  if: failure()
  uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02
  with:
    name: daily-intelligence-failure-diagnostics
    path: data/failure-diagnostics.json
    if-no-files-found: warn
```

- [ ] **Step 5: Run workflow tests and verify GREEN**

Run:

```powershell
rtk python -m pytest tests/test_config_models.py -k "workflow or restored" -q
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit workflow routing**

```powershell
rtk git add -- .github/workflows/daily-intelligence.yml tests/test_config_models.py
rtk git commit -m "ci: separate success and failure artifacts"
```

---

### Task 3: Record progress and run complete verification

**Files:**
- Modify: `docs/PROGRESS.md`

**Interfaces:**
- Consumes: completed CLI diagnostics and workflow routing.
- Produces: durable handoff record for Actions #24 and the remediation.

- [ ] **Step 1: Add the Actions #24 diagnosis and remediation record**

Append a dated entry containing these exact facts:

- Actions #24 ran commit `1b35fdc` on
  `codex/verification-promotion-20260728`.
- Tests and dry-run branch guard passed.
- `Run daily pipeline` failed with `TypeError`, exit code 4.
- The old 2026-07-26 summary and old Artifact were produced by `if: always()`,
  not by the failed run.
- The remediation adds secret-safe stage/frame diagnostics and splits success
  and failure outputs.
- No Actions were triggered, no DingTalk message was sent, and Secrets,
  visibility, workflow scheduling/enabled state, and `dry_run` policy were not
  changed during local development.

- [ ] **Step 2: Run focused regression tests**

Run:

```powershell
rtk python -m pytest tests/test_report_notifier.py tests/test_config_models.py -q
```

Expected: all tests PASS.

- [ ] **Step 3: Run the full suite**

Run:

```powershell
rtk python -m pytest -q
```

Expected: all tests PASS.

- [ ] **Step 4: Run repository safety checks**

Run:

```powershell
rtk git diff --check
rtk rg -n "schedule:|secrets\\.DINGTALK|dry_run=false|git push origin|git add data reports" .github/workflows/daily-intelligence.yml
rtk git status --short --branch
```

Expected:

- `git diff --check` exits 0.
- The restricted-pattern search finds no prohibited workflow behavior.
- Only the intended progress document remains uncommitted before its commit.

- [ ] **Step 5: Commit progress**

```powershell
rtk git add -- docs/PROGRESS.md
rtk git commit -m "docs: record action failure diagnostics"
```

- [ ] **Step 6: Final verification**

Run:

```powershell
rtk git status --short --branch
rtk git log -5 --oneline --decorate
```

Expected:

- Worktree is clean.
- Branch is ahead of the remote only by the local design, plan, implementation,
  workflow, and progress commits.
- No push, PR merge, workflow run, DingTalk send, Secret change, visibility
  change, or workflow enable/schedule change has occurred.
