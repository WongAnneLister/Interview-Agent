"""种子脚本里回写 .env 的部分（建用户的部分见 tests/integration）。"""

from __future__ import annotations

from pathlib import Path

from interview.db.seed import write_env_var


def test_appends_when_key_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n# comment\n", encoding="utf-8")
    write_env_var(env, "DEFAULT_USER_ID", "abc")
    assert env.read_text(encoding="utf-8") == "A=1\n# comment\nDEFAULT_USER_ID=abc\n"


def test_appends_newline_if_file_lacks_trailing_newline(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1", encoding="utf-8")
    write_env_var(env, "DEFAULT_USER_ID", "abc")
    assert env.read_text(encoding="utf-8") == "A=1\nDEFAULT_USER_ID=abc\n"


def test_replaces_existing_value_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("DEFAULT_USER_ID=old\nB=2\n", encoding="utf-8")
    write_env_var(env, "DEFAULT_USER_ID", "new")
    assert env.read_text(encoding="utf-8") == "DEFAULT_USER_ID=new\nB=2\n"


def test_does_not_touch_similar_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("X_DEFAULT_USER_ID=keep\n", encoding="utf-8")
    write_env_var(env, "DEFAULT_USER_ID", "abc")
    assert env.read_text(encoding="utf-8") == "X_DEFAULT_USER_ID=keep\nDEFAULT_USER_ID=abc\n"


def test_creates_file_if_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    write_env_var(env, "DEFAULT_USER_ID", "abc")
    assert env.read_text(encoding="utf-8") == "DEFAULT_USER_ID=abc\n"
