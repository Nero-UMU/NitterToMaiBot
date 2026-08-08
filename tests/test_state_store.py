"""持久化去重状态测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

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
