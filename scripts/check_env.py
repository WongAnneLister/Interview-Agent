"""检查开发依赖是否可用（需要先运行 scripts/tunnel.sh 和 scripts/livekit.sh）。

逐个检查端口连通性和健康接口，输出成功 / 失败，任一失败时退出码为 1。
只用标准库，不依赖项目虚拟环境。
"""

from __future__ import annotations

import socket
import sys
import urllib.request

CHECKS: list[tuple[str, str, int, str | None, bool]] = [
    # 名称, 主机, 端口, 健康检查 URL（None 表示只检查 TCP）, 是否可选
    ("Postgres", "127.0.0.1", 25432, None, False),
    ("Redis", "127.0.0.1", 26379, None, False),
    ("S3 (SeaweedFS)", "127.0.0.1", 29000, None, False),
    ("S3 文件浏览", "127.0.0.1", 29001, "http://127.0.0.1:29001/", False),
    # LiveKit 运行在本机（不经过隧道）
    ("LiveKit（本机）", "127.0.0.1", 7880, "http://127.0.0.1:7880/", False),
    # 可观测性服务（make obs-up 启动），未启动时只提示，不算失败
    ("Prometheus", "127.0.0.1", 29090, "http://127.0.0.1:29090/-/ready", True),
    ("Grafana", "127.0.0.1", 23000, "http://127.0.0.1:23000/api/health", True),
    ("Langfuse（本机）", "127.0.0.1", 23100, "http://127.0.0.1:23100/api/public/health", True),
]


def tcp_ok(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - 固定的本机地址
            return 200 <= resp.status < 300
    except OSError:
        return False


def main() -> int:
    # Windows 终端默认 GBK，统一按 UTF-8 输出中文
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    failed = 0
    for name, host, port, url, optional in CHECKS:
        ok = tcp_ok(host, port) and (url is None or http_ok(url))
        status = "OK  " if ok else ("SKIP" if optional else "FAIL")
        print(f"{status} {name:<15} {host}:{port}")
        failed += not ok and not optional
    if failed:
        print(f"\n{failed} 项失败：确认 scripts/tunnel.sh 正在运行，")
        print("且 VPS 上的容器都健康（make vps-ps）。")
    print("SKIP 表示可观测性服务未启动（需要时运行 make obs-up）。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
