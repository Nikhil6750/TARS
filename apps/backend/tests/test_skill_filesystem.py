from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.action_contracts import (
    ActionRequest,
    ActionSource,
    ActionStatus,
    RiskLevel,
    SkillValidationError,
)
from skills.filesystem import FilesystemSkill, resolve_within_safe_roots


def _request(action: str, arguments: dict) -> ActionRequest:
    return ActionRequest(
        skill="filesystem", action=action, arguments=arguments, source=ActionSource.hud
    )


@pytest.fixture
def home_tmp_dir(tmp_path, monkeypatch):
    """Points the skill's "home directory" safe-root at a real temp
    directory so tests can list/search/open real files without touching
    the actual user profile."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr("skills.filesystem.Path.home", staticmethod(lambda: fake_home))
    return fake_home


def test_classify_risk():
    skill = FilesystemSkill()
    assert skill.classify_risk("list", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("search", {}) == RiskLevel.READ_ONLY
    assert skill.classify_risk("open", {}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("delete", {}) == RiskLevel.BLOCKED
    assert skill.classify_risk("write", {}) == RiskLevel.BLOCKED
    assert skill.classify_risk("move", {}) == RiskLevel.BLOCKED


async def test_validate_rejects_write_delete_shaped_actions():
    skill = FilesystemSkill()
    for action in ("delete", "write", "move", "rename", "rmdir"):
        with pytest.raises(SkillValidationError):
            await skill.validate(action, {"path": "."})


async def test_validate_rejects_missing_path():
    skill = FilesystemSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("list", {})


async def test_validate_search_requires_query(home_tmp_dir):
    skill = FilesystemSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("search", {"path": "."})
    await skill.validate("search", {"path": ".", "query": "foo"})


def test_resolve_within_safe_roots_accepts_home_subpath(home_tmp_dir):
    (home_tmp_dir / "docs").mkdir()
    resolved = resolve_within_safe_roots("docs")
    assert resolved == (home_tmp_dir / "docs").resolve()


def test_resolve_within_safe_roots_rejects_traversal_outside_home(home_tmp_dir):
    with pytest.raises(SkillValidationError):
        resolve_within_safe_roots("..\\..\\Windows\\System32")


def test_resolve_within_safe_roots_rejects_absolute_path_outside_allowlist(home_tmp_dir):
    with pytest.raises(SkillValidationError):
        resolve_within_safe_roots("C:\\Windows\\System32")


def test_resolve_within_safe_roots_rejects_empty_string():
    with pytest.raises(SkillValidationError):
        resolve_within_safe_roots("")


async def test_execute_list_returns_real_directory_entries(home_tmp_dir):
    (home_tmp_dir / "a.txt").write_text("hello", encoding="utf-8")
    (home_tmp_dir / "subdir").mkdir()

    skill = FilesystemSkill()
    request = _request("list", {"path": "."})
    result = await skill.execute(request)

    assert result.status == ActionStatus.SUCCEEDED
    names = {e["name"] for e in result.data["entries"]}
    assert names == {"a.txt", "subdir"}
    entry_by_name = {e["name"]: e for e in result.data["entries"]}
    assert entry_by_name["subdir"]["is_dir"] is True
    assert entry_by_name["a.txt"]["is_dir"] is False


async def test_execute_list_fails_cleanly_on_non_directory(home_tmp_dir):
    (home_tmp_dir / "file.txt").write_text("x", encoding="utf-8")
    skill = FilesystemSkill()
    result = await skill.execute(_request("list", {"path": "file.txt"}))
    assert result.status == ActionStatus.FAILED
    assert result.error is not None


async def test_execute_search_finds_real_matching_filenames(home_tmp_dir):
    (home_tmp_dir / "strategy_notes.md").write_text("x", encoding="utf-8")
    (home_tmp_dir / "unrelated.txt").write_text("x", encoding="utf-8")
    nested = home_tmp_dir / "nested"
    nested.mkdir()
    (nested / "strategy_backup.md").write_text("x", encoding="utf-8")

    skill = FilesystemSkill()
    result = await skill.execute(_request("search", {"path": ".", "query": "strategy"}))

    assert result.status == ActionStatus.SUCCEEDED
    matched_names = {Path(m).name for m in result.data["matches"]}
    assert matched_names == {"strategy_notes.md", "strategy_backup.md"}


async def test_execute_open_calls_os_startfile(home_tmp_dir):
    target = home_tmp_dir / "openme.txt"
    target.write_text("x", encoding="utf-8")

    skill = FilesystemSkill()
    with patch("skills.filesystem.os.startfile", create=True) as mock_startfile:
        result = await skill.execute(_request("open", {"path": "openme.txt"}))

    assert result.status == ActionStatus.SUCCEEDED
    mock_startfile.assert_called_once()
    assert mock_startfile.call_args[0][0] == str(target.resolve())


async def test_execute_open_fails_when_path_missing(home_tmp_dir):
    skill = FilesystemSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("open", {"path": "does_not_exist.txt"})
