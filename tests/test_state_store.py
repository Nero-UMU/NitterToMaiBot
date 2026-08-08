"""持久化去重状态测试。"""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import json

from plugins.NitterToMaiBot.models import MediaAttachment, NitterPost
from plugins.NitterToMaiBot.state_store import StateStore


class StateStoreTests(TestCase):
    """验证基线、投递进度和原子持久化。"""

    def test_round_trip_and_delivery_progress(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = StateStore(state_path, max_seen_per_account=3)
            store.mark_baseline("example", ["1", "2"])
            store.mark_token_completed("example", "3", "10001", "message")
            store.save()

            restored = StateStore(state_path, max_seen_per_account=3)
            restored.load()

            self.assertTrue(restored.is_seen("example", "1"))
            self.assertEqual(restored.completed_tokens("example", "3", "10001"), {"message"})

            restored.mark_seen("example", "3")
            self.assertTrue(restored.is_seen("example", "3"))
            self.assertEqual(restored.completed_tokens("example", "3", "10001"), set())

    def test_seen_history_is_bounded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir) / "state.json", max_seen_per_account=2)
            store.mark_baseline("example", ["1", "2", "3"])

            self.assertFalse(store.is_seen("example", "1"))
            self.assertTrue(store.is_seen("example", "2"))
            self.assertTrue(store.is_seen("example", "3"))

    def test_legacy_state_without_quiet_queue_still_loads(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with state_path.open("w", encoding="utf-8") as state_file:
                json.dump(
                    {
                        "version": 1,
                        "seen": {"example": ["1"]},
                        "progress": {},
                    },
                    state_file,
                )

            store = StateStore(state_path, max_seen_per_account=10)
            store.load()

            self.assertTrue(store.is_seen("example", "1"))
            self.assertEqual(store.quiet_post_count(), 0)

    def test_quiet_queue_survives_restart_and_clears_when_seen(self) -> None:
        post = NitterPost(
            account="example",
            post_id="1001",
            author="@example",
            text="静默积压测试",
            published_at=datetime(2026, 8, 8, 16, 30, tzinfo=timezone.utc),
            url="https://nitter.net/example/status/1001",
            media=[MediaAttachment("https://nitter.net/pic/test.jpg", "image", "image/jpeg")],
            translated_text="静默积压翻译",
        )

        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = StateStore(state_path, max_seen_per_account=10)
            self.assertTrue(store.queue_quiet_post(post))
            self.assertFalse(store.queue_quiet_post(post))
            store.save()

            restored = StateStore(state_path, max_seen_per_account=10)
            restored.load()

            self.assertEqual(restored.quiet_post_count(), 1)
            self.assertEqual(restored.quiet_posts(), [post])
            restored.mark_seen(post.account, post.post_id)
            self.assertEqual(restored.quiet_post_count(), 0)
