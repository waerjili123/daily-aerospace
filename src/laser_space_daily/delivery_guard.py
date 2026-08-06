"""Prevent duplicate production delivery runs on the same Beijing date."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BEIJING = timezone(timedelta(hours=8))
LIVE_RUN_TITLES = frozenset({"scheduled-live", "manual-live"})


@dataclass(frozen=True)
class DeliveryDecision:
    """Result of checking whether a production workflow may continue."""

    should_run: bool
    reason: str
    blocking_run_id: int | None = None


def decide_daily_delivery(
    runs: Sequence[Mapping[str, Any]],
    *,
    current_run_id: int,
    beijing_date: date,
) -> DeliveryDecision:
    """Block when another successful live run exists on the Beijing date."""

    for run in runs:
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            continue
        if run_id == current_run_id:
            continue
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        if run.get("display_title") not in LIVE_RUN_TITLES:
            continue
        created_at = _parse_github_datetime(run.get("created_at"))
        if created_at is None or created_at.astimezone(BEIJING).date() != beijing_date:
            continue
        return DeliveryDecision(
            should_run=False,
            reason="live_delivery_already_succeeded",
            blocking_run_id=run_id,
        )
    return DeliveryDecision(should_run=True, reason="no_successful_live_delivery_today")


def fetch_workflow_runs(
    *,
    repository: str,
    token: str,
    attempts: int = 3,
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[Mapping[str, Any]]:
    """Fetch recent workflow runs with bounded retries and no token logging."""

    if attempts < 1 or attempts > 5:
        raise ValueError("attempts must be between one and five")
    query = urlencode({"per_page": 100})
    url = f"https://api.github.com/repos/{repository}/actions/runs?{query}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "daily-aerospace-delivery-guard",
        },
    )
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw_runs = payload.get("workflow_runs")
            if not isinstance(raw_runs, list):
                raise ValueError("GitHub response has no workflow_runs list")
            return [item for item in raw_runs if isinstance(item, Mapping)]
        except Exception as error:
            last_error = error
            if attempt < attempts:
                sleeper(float(attempt))
    raise RuntimeError("unable to query prior workflow runs") from last_error


def main() -> int:
    """Write the GitHub Actions gate output for the current workflow run."""

    event_name = _required_env("GITHUB_EVENT_NAME")
    delivery_mode = _required_env("DELIVERY_MODE")
    output_path = Path(_required_env("GITHUB_OUTPUT"))
    summary_path = Path(_required_env("GITHUB_STEP_SUMMARY"))

    if event_name != "schedule" and delivery_mode != "dingtalk_live":
        decision = DeliveryDecision(True, "non_live_manual_run")
    else:
        runs = fetch_workflow_runs(
            repository=_required_env("GITHUB_REPOSITORY"),
            token=_required_env("GITHUB_TOKEN"),
        )
        decision = decide_daily_delivery(
            runs,
            current_run_id=int(_required_env("GITHUB_RUN_ID")),
            beijing_date=datetime.now(UTC).astimezone(BEIJING).date(),
        )

    _write_github_output(output_path, decision)
    _write_summary(summary_path, decision)
    return 0


def _parse_github_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _write_github_output(path: Path, decision: DeliveryDecision) -> None:
    lines = [
        f"should_run={'true' if decision.should_run else 'false'}",
        f"reason={decision.reason}",
    ]
    if decision.blocking_run_id is not None:
        lines.append(f"blocking_run_id={decision.blocking_run_id}")
    with path.open("a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")


def _write_summary(path: Path, decision: DeliveryDecision) -> None:
    with path.open("a", encoding="utf-8") as summary:
        summary.write("## Daily delivery guard\n\n")
        summary.write(f"- Decision: `{'run' if decision.should_run else 'skip'}`\n")
        summary.write(f"- Reason: `{decision.reason}`\n")
        if decision.blocking_run_id is not None:
            summary.write(f"- Existing successful run: `{decision.blocking_run_id}`\n")


if __name__ == "__main__":
    raise SystemExit(main())
