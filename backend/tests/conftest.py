"""全局 pytest 配置。"""

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """在 CI 中强制跳过 live 测试，即使命令行显式传了 -m live。

    live 测试会调用真实厂商、产生费用，CI 里也没有密钥，所以 CI 中永远不跑。
    GitHub Actions 会自动设置 CI=true。
    """
    if os.environ.get("CI", "").lower() not in {"1", "true"}:
        return
    skip_live = pytest.mark.skip(reason="live 测试在 CI 中永远不跑")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
