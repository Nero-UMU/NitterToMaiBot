"""持久化去重状态测试。"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import json

from plugins.NitterToMaiBot.models import CachedMedia, MediaAttachment, NitterPost, PendingDelivery, StagedPost
from plugins.NitterToMaiBot.state_store import StateStore


class StateStoreTests(TestCase):
    """验证基线、投递进度和原子持久化。"""

    def test_old_self_message_queue_rebuilds_local_cache_without_losing_group_progress(self) -> None:
        post = NitterPost(
            account="example", post_id="1001", author="@example", text="旧队列",
            published_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            url="https://x.com/example/status/1001",
            media=[MediaAttachment("https://media/1.jpg", "image")],
        )
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            original = StateStore(path, 500)
            original.queue_delivery(PendingDelivery(post, ["10001", "10002"]))
            original.mark_token_completed("example", "1001", "10001", "forward")
            raw = original.snapshot()
            raw["version"] = 3
            raw["delivery_queue"][0]["staged"] = {"message_ids": ["-123"], "failed_media_tokens": []}
            raw["delivery_queue"][0]["self_id"] = "123456"
            path.write_text(json.dumps(raw), encoding="utf-8")
            restored = StateStore(path, 500)
            restored.load()
            item = restored.pending_delivery("example", "1001")
            self.assertEqual(item.post, post)
            self.assertEqual(item.group_ids, ["10001", "10002"])
            self.assertIsNone(item.staged)
            self.assertEqual(restored.completed_tokens("example", "1001", "10001"), {"forward"})
            self.assertNotIn("self_id", restored.snapshot()["delivery_queue"][0])

    def test_delivery_queue_round_trip_preserves_ids_failures_targets_and_schedule(self) -> None:
        post = NitterPost(
            account="example", post_id="1001", author="@example", text="待转发测试",
            published_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
            url="https://x.com/example/status/1001",
        )
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = StateStore(path, 500)
            staged = PendingDelivery(post, ["10001", "10002"], StagedPost([
                CachedMedia("a" * 64 + "/1.jpg", "image"),
            ], ["media_dropped:1"]))
            failed = PendingDelivery(replace(post, post_id="1002"), ["10001"], staging_failed=True)
            store.queue_delivery(staged)
            store.queue_delivery(failed)
            store.last_forward_at = 1234567890.5
            store.save()
            restored = StateStore(path, 500)
            restored.load()
            self.assertEqual(restored.pending_deliveries(), [staged, failed])
            self.assertEqual(restored.last_forward_at, 1234567890.5)
            restored.mark_seen("example", "1001")
            self.assertEqual(restored.pending_deliveries(), [failed])

    def test_url_seen_ids_are_normalized_before_deduplication_and_trimming(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with state_path.open("w", encoding="utf-8") as state_file:
                json.dump(
                    {
                        "version": 2,
                        "seen": {
                            "example": [
                                "1", "https://old.nitter/example/status/2#m", "2", "3",
                                "https://new.nitter/example/status/1?x=1#m",
                            ],
                        },
                        "progress": {},
                        "quiet_queue": [],
                    },
                    state_file,
                )
            store = StateStore(state_path, max_seen_per_account=3)
            store.load()
            for post_id in ("1", "2", "3"):
                self.assertTrue(store.is_seen("example", post_id))
            store.save()
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["seen"]["example"], ["2", "3", "1"],
            )

    def test_legacy_progress_and_quiet_queue_share_canonical_ids(self) -> None:
        post = NitterPost(
            account="example",
            post_id="2001",
            author="@example",
            text="待发送",
            published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
            url="https://nitter.net/example/status/2001",
        )
        legacy_id = "http://old.nitter/example/status/2001#m"
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            seed = StateStore(state_path, max_seen_per_account=10)
            seed.mark_baseline("example", ["1001"])
            seed.mark_token_completed("example", legacy_id, "10001", "forward")
            seed.mark_token_completed("example", legacy_id, "10001", "media:old")
            seed.mark_token_completed("example", "2001", "10001", "media:new")
            seed.mark_token_completed("example", "http://old.nitter/example/status/1001#m", "10001", "forward")
            seed.queue_quiet_post(replace(post, post_id=legacy_id))
            seed.queue_quiet_post(post)
            seed.queue_quiet_post(replace(post, post_id="http://old.nitter/example/status/1001#m"))
            seed.save()

            store = StateStore(state_path, max_seen_per_account=10)
            store.load()
            self.assertEqual(
                store.completed_tokens("example", "2001", "10001"), {"forward", "media:old", "media:new"},
            )
            self.assertEqual(store.completed_tokens("example", "1001", "10001"), set())
            self.assertEqual([item.post_id for item in store.quiet_posts()], ["2001"])
            self.assertTrue(store.is_quiet_queued("example", "2001"))
            store.mark_seen("example", "2001")
            self.assertEqual(store.completed_tokens("example", "2001", "10001"), set())
            self.assertEqual(store.quiet_post_count(), 0)
            store.save()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["progress"], {})
            self.assertEqual(saved["quiet_queue"], [])

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
