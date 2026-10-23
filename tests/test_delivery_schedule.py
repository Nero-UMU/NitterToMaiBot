"""独立更新检查/群转发周期，以及跨检查周期的暂存和失败汇总。"""

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import List
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import asyncio
import threading
import time

from plugins.NitterToMaiBot.models import MediaAttachment, PendingDelivery, StagedPost
from plugins.NitterToMaiBot.tests.test_subscription_forwarding import FakeQQ, TimelineClient, post, running_plugin


class DeliveryScheduleTests(IsolatedAsyncioTestCase):
    async def test_account_wait_applies_after_failure_and_baseline_without_extra_last_wait(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([])
        events = []

        async def fetch(account):
            events.append("fetch:" + account)
            if account == "first":
                raise RuntimeError("模拟账号失败")
            return []

        async def wait():
            events.append("wait")

        async with running_plugin(qq, client, nitter={
            "accounts": ["first", "second", "third"], "send_existing_on_first_run": False,
        }) as plugin:
            with (
                patch.object(client, "fetch_timeline", side_effect=fetch),
                patch.object(plugin, "_wait_between_accounts", side_effect=wait),
            ):
                result = await plugin._collect_updates()
            self.assertEqual(result.scanned_accounts, 3)
            self.assertEqual(result.failed_accounts, 1)
            self.assertEqual(events, ["fetch:first", "wait", "fetch:second", "wait", "fetch:third"])
            with patch("plugins.NitterToMaiBot.plugin.asyncio.sleep", new_callable=AsyncMock) as sleep:
                await plugin._wait_between_accounts()
                sleep.assert_awaited_once_with(2)

    async def test_forward_deadline_runs_while_check_waits_between_accounts_and_unload_stops_both(self) -> None:
        forwarded = asyncio.Event()
        waiting = asyncio.Event()
        release = asyncio.Event()

        class ObservedQQ(FakeQQ):
            async def rpc_call(self, method, plugin_id, payload, timeout_ms=None):
                result = await super().rpc_call(method, plugin_id, payload, timeout_ms)
                if payload["capability"] == "api.call" and payload["args"]["args"]["action_name"] == "send_group_forward_msg":
                    forwarded.set()
                return result

        qq = ObservedQQ()
        client = TimelineClient([
            replace(post(1), account="first"), replace(post(2), account="second"),
        ])
        async with running_plugin(qq, client, nitter={"accounts": ["first", "second"]}) as plugin:
            async def wait():
                waiting.set()
                await release.wait()

            with patch.object(plugin, "_wait_between_accounts", side_effect=wait):
                plugin._start_polling()
                await asyncio.wait_for(waiting.wait(), 2)
                self.assertEqual(plugin._require_state_store().pending_post_count(), 1)
                self.assertFalse(qq.forward_attempts)
                plugin._require_state_store().last_forward_at = time.time() - plugin.config.nitter.poll_interval_seconds
                plugin._forward_wake_event.set()
                await asyncio.wait_for(forwarded.wait(), 2)
                self.assertFalse(release.is_set())
                self.assertEqual(len(qq.forward_attempts), 1)
                self.assertIn("/status/1", qq.text(qq.forward_attempts[0][1]))
                self.assertNotIn("/status/2", qq.text(qq.forward_attempts[0][1]))
                await plugin._stop_polling()
            self.assertIsNone(plugin._poll_task)
            self.assertFalse(any(
                task.get_name() in {"nitter_update_check", "nitter_group_forward"} and not task.done()
                for task in asyncio.all_tasks()
            ))

    async def test_scan_duration_is_added_before_the_300_second_wait(self) -> None:
        qq = FakeQQ()
        starts = []
        clock = SimpleNamespace(now=0)
        async with running_plugin(qq, TimelineClient([])) as plugin:
            async def collect():
                starts.append(clock.now)
                clock.now += 12

            async def wait(timeout, event=None):
                self.assertEqual(timeout, 300)
                clock.now += timeout
                if len(starts) == 2:
                    raise asyncio.CancelledError
                return False

            with (
                patch.object(plugin, "_collect_updates", side_effect=collect),
                patch.object(plugin, "_wait_for_schedule", side_effect=wait),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._check_loop()
            self.assertEqual(starts, [0, 312])

    async def test_staging_does_not_restore_a_post_removed_during_upload(self) -> None:
        qq = FakeQQ()
        uploading, release = asyncio.Event(), asyncio.Event()
        async with running_plugin(qq, TimelineClient([post(1)])) as plugin:
            async def stage(*args):
                uploading.set()
                await release.wait()
                return StagedPost()

            with patch.object(plugin, "_stage_subscription_post", side_effect=stage):
                collect = asyncio.create_task(plugin._collect_updates())
                try:
                    await asyncio.wait_for(uploading.wait(), 2)
                    plugin._require_subscription_store().unsubscribe("10001", "example")
                    await plugin._forward_once()
                finally:
                    release.set()
                    await collect
            self.assertEqual(plugin._require_state_store().pending_post_count(), 0)
            self.assertFalse(qq.forward_attempts)

    async def test_concurrent_saves_use_independent_snapshots_and_preserve_new_pending_posts(self) -> None:
        qq = FakeQQ()
        entered, release = threading.Event(), threading.Event()
        async with running_plugin(qq, TimelineClient([])) as plugin:
            store = plugin._require_state_store()
            saved = []
            original_save = store.save

            def slow_save(snapshot):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("测试未释放写入")
                saved.append(snapshot)
                original_save(snapshot)

            store.queue_delivery(PendingDelivery(post(1), ["10001"]))
            with patch.object(store, "save", side_effect=slow_save):
                first = asyncio.create_task(plugin._save_state())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    store.queue_delivery(PendingDelivery(post(2), ["10001"]))
                    second = asyncio.create_task(plugin._save_state())
                finally:
                    release.set()
                await asyncio.gather(first, second)
            self.assertEqual([len(snapshot["delivery_queue"]) for snapshot in saved], [1, 2])
            store.load()
            self.assertEqual(store.pending_post_count(), 2)

    async def test_checks_stage_only_then_flush_combines_multiple_checks_after_reload(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1)])
        async with running_plugin(qq, client) as plugin:
            await plugin._collect_updates()
            self.assertEqual(plugin._require_state_store().pending_post_count(), 1)
            self.assertFalse(qq.forward_attempts)
            client.posts = [post(2), post(1)]
            await plugin._collect_updates()
            self.assertEqual(plugin._require_state_store().pending_post_count(), 2)
            self.assertFalse(qq.forward_attempts)
            store = plugin._require_state_store()
            original_time = store.last_forward_at
            store.load()
            self.assertEqual(store.last_forward_at, original_time)
            client.posts = []  # RSS 翻页不能丢失已暂存的推文。
            with patch.object(client, "fetch_timeline", side_effect=AssertionError("群发不能读取 RSS")):
                result = await plugin._forward_once()
            self.assertEqual(result.forwarded_posts, 2)
            self.assertEqual(store.pending_post_count(), 0)
            self.assertEqual(len(qq.forward_attempts), 1)
            self.assertEqual(len(qq.forward_attempts[0][1]), 2)
            self.assertNotIn("send_private_msg", qq.events)

    async def test_failed_links_accumulate_across_checks_and_are_last_once(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1), post(2, [MediaAttachment("https://media/bad.mp4", "video")])])
        async with running_plugin(qq, client) as plugin:
            await plugin._collect_updates()
            self.assertFalse(qq.notices)
            client.posts = [replace(post(3), has_video=True), post(4)]
            client.fail_enrich = True
            await plugin._collect_updates()
            self.assertFalse(qq.notices)
            plugin._require_state_store().load()
            await plugin._forward_once()
            self.assertEqual(len(qq.forward_attempts), 1)
            self.assertEqual(qq.events[-1], "failed_links_notice")
            self.assertEqual(len(qq.notices), 1)
            links = [node["segments"][0]["content"] for node in qq.notices[0][1:]]
            self.assertEqual(links, ["https://x.com/example/status/2", "https://x.com/example/status/3"])
            await plugin._forward_once()
            self.assertEqual(len(qq.notices), 1)

    async def test_unfollow_before_flush_does_not_send_old_pending_posts(self) -> None:
        qq = FakeQQ()
        async with running_plugin(qq, TimelineClient([post(1)])) as plugin:
            await plugin._collect_updates()
            plugin._require_subscription_store().unsubscribe("10001", "example")
            await plugin._forward_once()
            self.assertFalse(qq.forward_attempts)
            self.assertEqual(plugin._require_state_store().pending_post_count(), 0)

    async def test_missing_cached_file_drops_only_that_post(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1, [MediaAttachment("https://media/1.jpg", "image")]), post(2)])
        async with running_plugin(qq, client) as plugin:
            await plugin._collect_updates()
            store = plugin._require_state_store()
            item = store.pending_delivery("example", "1")
            self.assertIsNotNone(item)
            plugin._require_media_cache().path(item.staged.media[0]).unlink()
            result = await plugin._forward_once()
            self.assertEqual((result.forwarded_posts, result.dropped_posts), (1, 1))
            self.assertEqual(len(qq.forward_attempts), 1)
            self.assertIn("/status/2", qq.text(qq.forward_attempts[0][1]))
            self.assertEqual(qq.notices[0][-1]["segments"][0]["content"], "https://x.com/example/status/1")

    async def test_legacy_quiet_queue_is_staged_even_without_rss_entries(self) -> None:
        qq = FakeQQ()
        async with running_plugin(qq, TimelineClient([])) as plugin:
            store = plugin._require_state_store()
            store.mark_baseline("example", [])
            store.queue_quiet_post(post(1))
            store.save()
            with patch.object(plugin, "_is_quiet_period", return_value=True):
                await plugin._collect_updates()
                await plugin._forward_once()
            self.assertEqual(plugin._require_state_store().pending_post_count(), 1)
            self.assertFalse(qq.forward_attempts)
            store.load()
            await plugin._forward_once()
            self.assertEqual(len(qq.forward_attempts), 1)
            self.assertNotIn("send_private_msg", qq.events)

    async def test_cancelled_staging_is_recovered_without_rss(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1)])
        async with running_plugin(qq, client) as plugin:
            with patch.object(plugin, "_stage_subscription_post", side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._collect_updates()
            plugin._require_state_store().load()
            await plugin._forward_once()
            self.assertFalse(qq.notices)
            client.posts = []
            await plugin._collect_updates()
            await plugin._forward_once()
            self.assertEqual(len(qq.forward_attempts), 1)

    async def test_scheduler_checks_each_120_seconds_and_sends_each_600_seconds(self) -> None:
        await self._assert_schedule(120, 600, 601, [0, 120, 240, 360, 480, 600], [600])

    async def test_short_group_interval_does_not_force_extra_checks(self) -> None:
        await self._assert_schedule(120, 30, 91, [0], [30, 60, 90])

    async def test_quiet_period_still_checks_but_defers_group_sending(self) -> None:
        qq = FakeQQ()
        async with running_plugin(qq, TimelineClient([]), quiet_hours={"enabled": True}) as plugin:
            plugin._require_state_store().last_forward_at = 1
            with patch("plugins.NitterToMaiBot.plugin.datetime") as clock:
                from plugins.NitterToMaiBot.plugin import BEIJING_TIMEZONE

                clock.now.return_value = datetime(2026, 10, 20, 0, 30, tzinfo=BEIJING_TIMEZONE)
                self.assertEqual(plugin._forward_delay(), 5.5 * 3600)

    async def test_delayed_initial_check_does_not_delay_group_clock(self) -> None:
        await self._assert_schedule(120, 30, 121, [120], [30, 60, 90, 120], scan_on_load=False)

    async def test_wake_checks_immediately_without_advancing_forward_deadline(self) -> None:
        qq = FakeQQ()
        clock = SimpleNamespace(now=0.0, woke=False)
        checks = []
        async with running_plugin(qq, TimelineClient([])) as plugin:
            plugin._require_state_store().last_forward_at = 1000

            async def collect():
                checks.append(clock.now)

            async def wait(timeout):
                if not clock.woke:
                    clock.now = 50
                    clock.woke = True
                    return True
                raise asyncio.CancelledError

            forward = AsyncMock()
            with (
                patch("plugins.NitterToMaiBot.plugin.time", SimpleNamespace(
                    monotonic=lambda: clock.now, time=lambda: 1000 + clock.now,
                )),
                patch.object(plugin, "_collect_updates", side_effect=collect),
                patch.object(plugin, "_forward_once", forward),
                patch.object(plugin, "_wait_for_schedule", side_effect=wait),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._check_loop()
            self.assertEqual(checks, [0, 50])
            forward.assert_not_awaited()

    async def _assert_schedule(
        self, check_interval: int, forward_interval: int, stop_at: int,
        expected_checks: List[int], expected_forwards: List[int],
        scan_on_load: bool = True,
    ) -> None:
        qq = FakeQQ()
        clock = SimpleNamespace(now=0.0)
        checks: List[int] = []
        forwards: List[int] = []
        async with running_plugin(
            qq, TimelineClient([]), nitter={
                "check_interval_seconds": check_interval,
                "poll_interval_seconds": forward_interval,
                "scan_on_load": scan_on_load,
            },
        ) as plugin:
            plugin._require_state_store().last_forward_at = 1000

            async def collect():
                checks.append(round(clock.now))

            async def forward():
                forwards.append(round(clock.now))
                plugin._require_state_store().last_forward_at = 1000 + clock.now

            async def wait(timeout, event=None):
                clock.now += timeout
                if clock.now >= stop_at:
                    raise asyncio.CancelledError
                return False

            with (
                patch("plugins.NitterToMaiBot.plugin.time", SimpleNamespace(
                    monotonic=lambda: clock.now, time=lambda: 1000 + clock.now,
                )),
                patch.object(plugin, "_collect_updates", new=AsyncMock(side_effect=collect)),
                patch.object(plugin, "_forward_once", new=AsyncMock(side_effect=forward)),
                patch.object(plugin, "_wait_for_schedule", side_effect=wait),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._check_loop()
                clock.now = 0
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._forward_loop()
        self.assertEqual(checks, expected_checks)
        self.assertEqual(forwards, expected_forwards)
