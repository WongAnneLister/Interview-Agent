"""检查开发依赖是否可用（需要先运行 scripts/tunnel.sh）。

逐个检查端口连通性和健康接口，输出成功 / 失败，任一失败时退出码为 1。
只用标准库，不依赖项目虚拟环境。
"""

from __future__ import annotations

import socket
import sys
import urllib.request

CHECKS: list[tuple[str, str, int, str | None]] = [
    # 名称, 主机, 端口, 健康检查 URL（None 表示只检查 TCP）
    ("Postgres", "127.0.0.1", 25432, None),
    ("Redis", "127.0.0.1", 26379, None),
    ("S3 (SeaweedFS)", "127.0.0.1", 29000, None),
    ("S3 文件浏览", "127.0.0.1", 29001, "http://127.0.0.1:29001/"),
    ("Prometheus", "127.0.0.1", 29090, "http://127.0.0.1:29090/-/ready"),
    ("Grafana", "127.0.0.1", 23000, "http://127.0.0.1:23000/api/health"),
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
    for name, host, port, url in CHECKS:
        ok = tcp_ok(host, port) and (url is None or http_ok(url))
        print(f"{'OK  ' if ok else 'FAIL'} {name:<15} {host}:{port}")
        failed += not ok
    if failed:
        print(f"\n{failed} 项失败：确认 scripts/tunnel.sh 正在运行，")
        print("且 VPS 上的容器都健康（make vps-ps）。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
