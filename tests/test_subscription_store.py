"""QQ 群统一订阅存储测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import json

from plugins.NitterToMaiBot.subscription_store import SubscriptionStore


class SubscriptionStoreTests(TestCase):
    """验证分群订阅、大小写去重和推送开关。"""

    def test_round_trip_and_target_mapping(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "subscriptions.json"
            store = SubscriptionStore(path)
            self.assertTrue(store.subscribe("10001", "OpenAI"))
            self.assertFalse(store.subscribe("10001", "openai"))
            self.assertTrue(store.subscribe("10002", "openai"))
            self.assertTrue(store.set_display_name("openai", "OpenAI 官方"))
            store.set_push_enabled("10002", False)
            store.save()

            loaded = SubscriptionStore(path)
            loaded.load()

            self.assertEqual(loaded.accounts_for_group("10001"), ["OpenAI"])
            self.assertEqual(loaded.display_name("OPENAI"), "OpenAI 官方")
            self.assertEqual(loaded.target_groups_by_account(), {"OpenAI": ["10001"]})
            self.assertFalse(loaded.is_push_enabled("10002"))

    def test_unsubscribe_is_case_insensitive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = SubscriptionStore(Path(temp_dir) / "subscriptions.json")
            store.subscribe("10001", "OpenAI")

            self.assertTrue(store.unsubscribe("10001", "openai"))
            self.assertEqual(store.accounts_for_group("10001"), [])
            self.assertEqual(store.group_count(), 0)

    def test_version_one_is_migrated_to_account_mapping(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "subscriptions.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": {
                            "10001": {"enabled": True, "accounts": ["OpenAI"]},
                            "10002": {"enabled": False, "accounts": ["openai", "elonmusk"]},
                        },
                    }
                ),
                encoding="utf-8",
            )

            store = SubscriptionStore(path)
            store.load()

            self.assertTrue(store.needs_save)
            self.assertEqual(
                store.snapshot(),
                {
                    "version": 2,
                    "groups": [
                        {"group_id": "10001", "enabled": True},
                        {"group_id": "10002", "enabled": False},
                    ],
                    "accounts": [
                        {"account": "OpenAI", "qq_groups": ["10001", "10002"]},
                        {"account": "elonmusk", "qq_groups": ["10002"]},
                    ],
                },
            )
            store.save()
            with path.open("r", encoding="utf-8") as subscription_file:
                saved = json.load(subscription_file)
            self.assertEqual(saved["version"], 2)

    def test_legacy_global_subscriptions_are_merged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = SubscriptionStore(Path(temp_dir) / "subscriptions.json")
            store.subscribe("10001", "OpenAI")
            store.set_push_enabled("10001", False)

            changed = store.merge_legacy_global_subscriptions(
                ["OpenAI", "elonmusk"],
                ["10001", "10002"],
            )

            self.assertTrue(changed)
            self.assertEqual(
                store.target_groups_by_account(),
                {
                    "OpenAI": ["10001", "10002"],
                    "elonmusk": ["10001", "10002"],
                },
            )
            self.assertTrue(store.is_push_enabled("10001"))

    def test_toggle_does_not_create_empty_group(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = SubscriptionStore(Path(temp_dir) / "subscriptions.json")

            self.assertFalse(store.set_push_enabled("10001", False))
            self.assertFalse(store.merge_legacy_global_subscriptions([], ["10001"]))
            self.assertEqual(store.group_count(), 0)
