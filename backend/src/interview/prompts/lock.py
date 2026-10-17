"""提示词锁文件 versions.lock：登记每个提示词的版本号和正文哈希，保证“改了内容就一定加了版本号”。

- check_lock()：对比提示词目录与锁文件，返回问题列表（单元测试里调用，make check 会跑到）：
    改了正文但版本号没加一 / 版本号变小 / 新提示词或已删除的提示词未登记 / 锁文件未同步。
- 改完提示词并把 version 加一后，运行 make prompts-lock 更新锁文件（与提示词一起提交）。
  如果存在“改了正文没加版本号”的问题，更新会被拒绝，必须先改版本号。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from interview.prompts.loader import PROMPTS_DIR, PromptLibrary

LOCK_PATH = PROMPTS_DIR / "versions.lock"


@dataclass(frozen=True)
class LockEntry:
    version: int
    hash: str


def read_lock(path: Path = LOCK_PATH) -> dict[str, LockEntry]:
    if not path.exists():
        return {}
    raw: dict[str, dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
    return {
        prompt_id: LockEntry(int(str(entry["version"])), str(entry["hash"]))
        for prompt_id, entry in raw.items()
    }


def current_entries(library: PromptLibrary) -> dict[str, LockEntry]:
    entries: dict[str, LockEntry] = {}
    for prompt_id in library.ids():
        template = library.get(prompt_id)
        entries[prompt_id] = LockEntry(template.meta.version, template.content_hash)
    return entries


def blocking_problems(library: PromptLibrary, lock: dict[str, LockEntry]) -> list[str]:
    """更新锁文件也不能解决、必须先改提示词的问题。"""
    problems: list[str] = []
    for prompt_id, now in current_entries(library).items():
        before = lock.get(prompt_id)
        if before is None:
            continue
        if now.version < before.version:
            problems.append(f"{prompt_id}：版本号从 {before.version} 变小为 {now.version}")
        elif now.hash != before.hash and now.version == before.version:
            problems.append(
                f"{prompt_id}：正文改了但版本号仍是 {now.version}，请在文件头把 version 加一"
            )
    return problems


def check_lock(library: PromptLibrary, path: Path = LOCK_PATH) -> list[str]:
    """返回全部问题；空列表表示锁文件与提示词一致。"""
    lock = read_lock(path)
    problems = blocking_problems(library, lock)
    now = current_entries(library)
    for prompt_id in sorted(now.keys() - lock.keys()):
        problems.append(f"{prompt_id}：新提示词未登记，运行 make prompts-lock")
    for prompt_id in sorted(lock.keys() - now.keys()):
        problems.append(f"{prompt_id}：提示词已删除但锁文件里还有，运行 make prompts-lock")
    if not problems and now != lock:
        changed = sorted(k for k in now if now[k] != lock.get(k))
        problems.append(f"锁文件未同步（{', '.join(changed)}），运行 make prompts-lock")
    return problems


def write_lock(library: PromptLibrary, path: Path = LOCK_PATH) -> None:
    """有阻断性问题时拒绝更新（抛 ValueError）。"""
    problems = blocking_problems(library, read_lock(path))
    if problems:
        raise ValueError("\n".join(problems))
    data = {k: {"version": v.version, "hash": v.hash} for k, v in current_entries(library).items()}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    try:
        write_lock(PromptLibrary())
    except ValueError as exc:
        print(f"未更新锁文件：\n{exc}", file=sys.stderr)
        return 1
    print(f"已更新 {LOCK_PATH.name}（{len(read_lock())} 个提示词）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
