"""插件订阅统一迁移测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

import json
import tomllib

from maibot_sdk.context import PluginContext, PluginPaths

from plugins.NitterToMaiBot.config_mirror import SubscriptionConfigMirror
from plugins.NitterToMaiBot.plugin import create_plugin


class PluginMigrationTests(IsolatedAsyncioTestCase):
    """验证旧订阅文件和旧全局配置会在加载时一次性合并。"""

    async def test_on_load_migrates_all_legacy_subscription_sources(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "data"
            data_dir.mkdir()
            subscription_path = data_dir / "subscriptions.json"
            subscription_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": {
                            "10001": {"enabled": True, "accounts": ["elonmusk"]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = temp_path / "config.toml"
            config_path.write_text(
                "[nitter]\naccounts = [\"OpenAI\"]\n\n[delivery]\nqq_groups = [\"10002\"]\n",
                encoding="utf-8",
            )

            plugin = create_plugin()
            plugin._config_mirror = SubscriptionConfigMirror(config_path)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.3.0"},
                    "nitter": {
                        "base_url": "http://127.0.0.1:8080",
                        "accounts": ["OpenAI"],
                    },
                    "delivery": {"qq_groups": ["10002"]},
                }
            )
            plugin._set_context(
                PluginContext(
                    "third-party.nitter-to-maibot",
                    paths=PluginPaths(
                        data_dir=data_dir,
                        runtime_dir=temp_path / "runtime",
                    ),
                )
            )

            await plugin.on_load()
            targets = plugin._build_scan_targets()
            await plugin.on_unload()

            with subscription_path.open("r", encoding="utf-8") as subscription_file:
                subscriptions = json.load(subscription_file)
            with config_path.open("rb") as config_file:
                mirrored_config = tomllib.load(config_file)

            self.assertEqual(targets, {"elonmusk": ["10001"], "OpenAI": ["10002"]})
            self.assertEqual(subscriptions["version"], 2)
            self.assertEqual(mirrored_config["nitter"]["accounts"], [])
            self.assertEqual(mirrored_config["delivery"]["qq_groups"], [])
            self.assertEqual(mirrored_config["subscriptions"]["groups"], subscriptions["groups"])
            self.assertEqual(mirrored_config["subscriptions"]["accounts"], subscriptions["accounts"])
