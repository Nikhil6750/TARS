from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar
from uuid import uuid4

import pytest

from tools.tars_test_client import TarsTestClient


ROOT = Path(__file__).resolve().parents[2]
T = TypeVar("T")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "acceptance: requires running TARS services")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.getenv("TARS_ACCEPTANCE") == "1":
        return
    skip = pytest.mark.skip(reason="set TARS_ACCEPTANCE=1 or use tools/run_acceptance.py")
    for item in items:
        if item.get_closest_marker("acceptance") is not None:
            item.add_marker(skip)


@pytest.fixture
def client() -> TarsTestClient:
    instance = TarsTestClient(
        os.getenv("TARS_BASE_URL", "http://127.0.0.1:8000"),
        timeout_seconds=float(os.getenv("TARS_TEST_TIMEOUT", "5")),
    )
    yield instance
    instance.close()


@pytest.fixture
def valid_event() -> dict[str, object]:
    from datetime import timedelta
    event = json.loads(
        (
            ROOT / "tests" / "fixtures" / "valid" / "setup_valid.json"
        ).read_text(encoding="utf-8")
    )
    event["event_id"] = str(uuid4())
    now = datetime.now(timezone.utc)
    event["timestamp"] = now.isoformat().replace("+00:00", "Z")
    if event.get("expires_at"):
        event["expires_at"] = (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    event["source"] = "manual"
    event["symbol"] = f"TST{uuid4().hex[:8].upper()}"
    return event


def poll_until(
    operation: Callable[[], T], predicate: Callable[[T], bool], timeout: float = 5.0
) -> T:
    deadline = time.monotonic() + timeout
    last_value: T | None = None
    while time.monotonic() < deadline:
        last_value = operation()
        if predicate(last_value):
            return last_value
        threading.Event().wait(min(0.05, max(0.0, deadline - time.monotonic())))
    raise AssertionError(f"condition not met within {timeout}s; last value={last_value!r}")
