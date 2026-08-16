from __future__ import annotations

from tools.security_checks import find_live_execution_operations


def test_execution_surface_detector_flags_mutating_order_paths() -> None:
    document = {
        "paths": {
            "/api/orders": {"post": {}},
            "/api/events": {"post": {}},
            "/api/execution": {"delete": {}},
        }
    }
    assert find_live_execution_operations(document) == [
        "DELETE /api/execution",
        "POST /api/orders",
    ]


def test_execution_surface_detector_ignores_read_only_documentation() -> None:
    document = {
        "paths": {
            "/api/order-history": {"get": {}},
            "/api/events/{event_id}/invalidate": {"post": {}},
        }
    }
    assert find_live_execution_operations(document) == []
