from __future__ import annotations

import pytest

from memory.service import MemoryService


def test_sqlite_vec_enabled_without_implementation_raises(tmp_path):
    with pytest.raises(NotImplementedError):
        MemoryService(conn=None, vault_path=str(tmp_path), sqlite_vec_enabled=True)  # type: ignore[arg-type]
