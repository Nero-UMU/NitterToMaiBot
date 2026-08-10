"""插件订阅统一迁移测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

import json
import tomllib

from maibot_sdk.context import PluginContext, PluginPaths

from plugins.NitterToMaiBot.config_mirror import SubscriptionConfigMirror
from plugins.NitterToMaiBot.plugin import LEGACY_PLUGIN_ID, PLUGIN_ID, create_plugin


class PluginMigrationTests(IsolatedAsyncioTestCase):
    """验证旧订阅文件和旧全局配置会在加载时一次性合并。"""

    def test_legacy_id_keeps_using_its_current_data_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            legacy_data_dir = temp_path / LEGACY_PLUGIN_ID
            legacy_data_dir.mkdir()
            state_path = legacy_data_dir / "state.json"
            state_path.write_text("{}", encoding="utf-8")
            plugin = create_plugin()
            plugin._set_context(
                PluginContext(
                    LEGACY_PLUGIN_ID,
                    paths=PluginPaths(
                        data_dir=legacy_data_dir,
                        runtime_dir=temp_path / "runtime",
                    ),
                )
            )

            self.assertFalse(plugin._migrate_legacy_plugin_id_data())
            self.assertTrue(state_path.is_file())

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
                    PLUGIN_ID,
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

            self.assertEqual(
                targets,
                {"elonmusk": {"10001": False}, "OpenAI": {"10002": False}},
            )
            self.assertEqual(subscriptions["version"], 3)
            self.assertEqual(mirrored_config["nitter"]["accounts"], [])
            self.assertEqual(mirrored_config["delivery"]["qq_groups"], [])
            self.assertEqual(mirrored_config["subscriptions"]["groups"], subscriptions["groups"])
            self.assertEqual(mirrored_config["subscriptions"]["accounts"], subscriptions["accounts"])

    async def test_on_load_migrates_legacy_plugin_id_data_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            plugin_data_root = temp_path / "plugin-data"
            legacy_data_dir = plugin_data_root / LEGACY_PLUGIN_ID
            legacy_data_dir.mkdir(parents=True)
            (legacy_data_dir / "state.json").write_text(
                json.dumps({"version": 1, "seen": {"OpenAI": ["123"]}, "progress": {}}),
                encoding="utf-8",
            )
            (legacy_data_dir / "subscriptions.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "groups": [{"group_id": "10001", "enabled": True}],
                        "accounts": [{"account": "OpenAI", "qq_groups": ["10001"]}],
                    }
                ),
                encoding="utf-8",
            )
            config_path = temp_path / "config.toml"
            config_path.write_text(
                "[nitter]\naccounts = []\n\n[delivery]\nqq_groups = []\n",
                encoding="utf-8",
            )
            new_data_dir = plugin_data_root / PLUGIN_ID

            plugin = create_plugin()
            plugin._config_mirror = SubscriptionConfigMirror(config_path)
            plugin.set_plugin_config(
                {"plugin": {"enabled": False, "config_version": "1.5.3"}}
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    paths=PluginPaths(
                        data_dir=new_data_dir,
                        runtime_dir=temp_path / "runtime",
                    ),
                )
            )

            await plugin.on_load()
            targets = plugin._build_scan_targets()
            state_store = plugin._require_state_store()
            await plugin.on_unload()

            self.assertFalse(legacy_data_dir.exists())
            self.assertTrue((new_data_dir / "state.json").is_file())
            self.assertTrue((new_data_dir / "subscriptions.json").is_file())
            self.assertEqual(targets, {"OpenAI": {"10001": False}})
            self.assertTrue(state_store.is_seen("OpenAI", "123"))
