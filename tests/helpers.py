"""NitterToMaiBot 测试辅助函数。"""

from pathlib import Path
from typing import Any

from plugins.NitterToMaiBot.config_mirror import SubscriptionConfigMirror


def use_temporary_config_mirror(plugin: Any, temp_dir: str) -> None:
    """让测试只写临时配置，避免订阅镜像改动插件随附模板。"""

    config_path = Path(temp_dir) / "config.toml"
    config_path.write_text(
        "[nitter]\naccounts = []\n\n[delivery]\nqq_groups = []\n",
        encoding="utf-8",
    )
    plugin._config_mirror = SubscriptionConfigMirror(config_path)
