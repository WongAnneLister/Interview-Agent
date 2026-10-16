"""从文本文件创建百炼热词表并打印 vocabulary_id；也可以删除热词表（每个账号最多 10 个）。

用法：
  make hotwords file=words.txt prefix=interview          创建，打印 vocabulary_id
  uv run python tools/create_hotwords.py --delete <vocabulary_id>

文件格式：每行一个热词，可写成“热词<TAB>权重”（权重 1–5，默认 4）；空行和 # 开头的行忽略。
target_model 默认取 .env 的 STT_MODEL（必须与识别时的型号一致）；
需要 BAILIAN_API_KEY 和 BAILIAN_API_BASE_URL。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from interview.config import DEFAULT_ENV_FILE, BailianSettings, STTSettings
from interview.providers.bailian_hotwords import BailianHotwords, Hotword


def read_words(path: Path, default_weight: int) -> list[Hotword]:
    words: list[Hotword] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        text, _, weight = line.partition("\t")
        try:
            words.append(Hotword(text.strip(), int(weight) if weight.strip() else default_weight))
        except ValueError as exc:
            raise ValueError(f"第 {number} 行权重不是整数：{raw!r}") from exc
    return words


async def run(args: argparse.Namespace) -> int:
    vendor = BailianSettings(_env_file=DEFAULT_ENV_FILE)  # pyright: ignore[reportCallIssue]
    stt = STTSettings(_env_file=DEFAULT_ENV_FILE)  # pyright: ignore[reportCallIssue]
    if not vendor.configured or not vendor.api_base_url:
        print("BAILIAN_API_KEY / BAILIAN_API_BASE_URL 未配置（见 .env.example）", file=sys.stderr)
        return 1
    client = BailianHotwords(
        api_key=vendor.api_key.get_secret_value(),
        base_url=vendor.api_base_url,
        timeout_s=stt.timeout_s,
    )
    try:
        if args.delete:
            await client.delete(args.delete)
            print(f"已删除 {args.delete}")
            return 0
        target_model = args.target_model or stt.model
        if not args.file or not args.prefix or not target_model:
            print("创建需要 file、--prefix，以及 --target-model 或 STT_MODEL", file=sys.stderr)
            return 1
        words = read_words(Path(args.file), args.weight)
        vocabulary_id = await client.create(
            target_model=target_model, prefix=args.prefix, words=words
        )
        print(vocabulary_id)
        return 0
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("file", nargs="?", help="热词文本文件")
    parser.add_argument("--prefix", help="热词表前缀（小写字母和数字，最多 10 位）")
    parser.add_argument("--target-model", help="识别型号，默认取 STT_MODEL")
    parser.add_argument("--weight", type=int, default=4, help="未写权重的行使用的权重（1–5）")
    parser.add_argument("--delete", metavar="VOCABULARY_ID", help="删除指定热词表")
    try:
        return asyncio.run(run(parser.parse_args()))
    except ValueError as exc:
        print(f"热词不合法：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
