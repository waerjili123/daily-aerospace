from datetime import date
import json
from types import SimpleNamespace

import pytest

from laser_space_daily.delivery_guard import (
    decide_daily_delivery,
    fetch_workflow_runs,
)


TODAY = date(2026, 8, 5)


def run_row(
    run_id: int,
    *,
    title: str = "scheduled-live",
    status: str = "completed",
    conclusion: str | None = "success",
    created_at: str = "2026-08-05T00:01:00Z",
) -> dict:
    return {
        "id": run_id,
        "display_title": title,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
    }


@pytest.mark.parametrize("title", ["scheduled-live", "manual-live"])
def test_successful_live_run_today_blocks_delivery(title: str) -> None:
    decision = decide_daily_delivery(
        [run_row(10, title=title)], current_run_id=11, beijing_date=TODAY
    )

    assert decision.should_run is False
    assert decision.reason == "live_delivery_already_succeeded"
    assert decision.blocking_run_id == 10


def test_current_run_and_non_live_runs_do_not_block() -> None:
    decision = decide_daily_delivery(
        [
            run_row(11),
            run_row(10, title="manual-dry-run"),
            run_row(9, title="manual-test"),
        ],
        current_run_id=11,
        beijing_date=TODAY,
    )

    assert decision.should_run is True


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("in_progress", None), ("completed", "failure"), ("completed", "cancelled")],
)
def test_unsuccessful_prior_live_run_allows_failover(
    status: str, conclusion: str | None
) -> None:
    decision = decide_daily_delivery(
        [run_row(10, status=status, conclusion=conclusion)],
        current_run_id=11,
        beijing_date=TODAY,
    )

    assert decision.should_run is True


def test_beijing_date_boundary_is_used() -> None:
    decision = decide_daily_delivery(
        [run_row(10, created_at="2026-08-04T15:59:59Z")],
        current_run_id=11,
        beijing_date=TODAY,
    )

    assert decision.should_run is True


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_fetch_workflow_runs_retries_without_exposing_token() -> None:
    calls = []
    sleeps = []

    def opener(request, timeout):
        calls.append(SimpleNamespace(request=request, timeout=timeout))
        if len(calls) < 3:
            raise TimeoutError("temporary")
        return FakeResponse({"workflow_runs": [run_row(10)]})

    rows = fetch_workflow_runs(
        repository="owner/repo",
        token="sensitive-token",
        opener=opener,
        sleeper=sleeps.append,
    )

    assert rows == [run_row(10)]
    assert sleeps == [1.0, 2.0]
    assert all(call.timeout == 15 for call in calls)
    assert all(
        call.request.full_url
        == "https://api.github.com/repos/owner/repo/actions/runs?per_page=100"
        for call in calls
    )
    assert all("sensitive-token" not in call.request.full_url for call in calls)


def test_fetch_workflow_runs_fails_closed_after_bounded_retries() -> None:
    calls = 0

    def opener(_request, timeout):
        nonlocal calls
        del timeout
        calls += 1
        raise TimeoutError("unavailable")

    with pytest.raises(RuntimeError, match="unable to query prior workflow runs"):
        fetch_workflow_runs(
            repository="owner/repo",
            token="token",
            opener=opener,
            sleeper=lambda _seconds: None,
        )

    assert calls == 3
