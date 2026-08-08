"""后台只读订阅镜像测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import tomllib

from plugins.NitterToMaiBot.config_mirror import SubscriptionConfigMirror


class SubscriptionConfigMirrorTests(TestCase):
    """验证镜像只替换订阅展示和旧订阅字段。"""

    def test_sync_preserves_other_settings_and_clears_legacy_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                """[plugin]
enabled = true

[nitter]
base_url = "http://127.0.0.1:8080"
accounts = ["old_account"]

[delivery]
qq_groups = ["10000"]
forward_batch_threshold = 3

[subscriptions]
groups = []
accounts = []
""",
                encoding="utf-8",
            )
            mirror = SubscriptionConfigMirror(config_path)
            snapshot = {
                "version": 2,
                "groups": [{"group_id": "10001", "enabled": True}],
                "accounts": [
                    {
                        "account": "OpenAI",
                        "display_name": "OpenAI 官方",
                        "qq_groups": ["10001"],
                    }
                ],
            }

            self.assertTrue(mirror.sync(snapshot))
            self.assertFalse(mirror.sync(snapshot))
            with config_path.open("rb") as config_file:
                config = tomllib.load(config_file)

            self.assertTrue(config["plugin"]["enabled"])
            self.assertEqual(config["nitter"]["base_url"], "http://127.0.0.1:8080")
            self.assertEqual(config["nitter"]["accounts"], [])
            self.assertEqual(config["delivery"]["qq_groups"], [])
            self.assertEqual(config["delivery"]["forward_batch_threshold"], 3)
            self.assertEqual(config["subscriptions"]["groups"], snapshot["groups"])
            self.assertEqual(config["subscriptions"]["accounts"], snapshot["accounts"])
