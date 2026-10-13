"""占位测试：保证工具链（pytest、导入路径）在空项目上可用，后续 PR 会替换为真实测试。"""

import interview


def test_package_importable() -> None:
    assert interview.__name__ == "interview"
