"""自动订阅：服务器缓存、直接群合并转发、媒体失败及周期末链接汇总。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, AsyncIterator, List, Optional
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from maibot_sdk.context import PluginContext, PluginPaths

from plugins.NitterToMaiBot.models import MediaAttachment, NitterPost
from plugins.NitterToMaiBot.plugin import PLUGIN_ID, create_plugin
from plugins.NitterToMaiBot.tests.helpers import use_temporary_config_mirror


def post(post_id: int, media: Optional[List[MediaAttachment]] = None) -> NitterPost:
    return NitterPost(
        account="example", post_id=str(post_id), author="@example", text=f"正文 {post_id}",
        published_at=datetime(2026, 10, 20, tzinfo=timezone.utc) + timedelta(seconds=post_id),
        url=f"https://nitter.net/example/status/{post_id}", media=media or [],
        has_video=any(m.media_type == "video" for m in media or []),
    )


class TimelineClient:
    def __init__(self, posts: List[NitterPost]) -> None:
        self.posts = posts
        self.image_size = 10
        self.fail_enrich = False
        self.downloads = []

    async def fetch_timeline(self, account: str) -> List[NitterPost]:
        return [p for p in self.posts if p.account == account]

    def profile_name(self, account: str) -> str:
        return account

    async def download_media(self, url: str, max_bytes: int):
        self.downloads.append(url)
        if "bad" in url:
            raise RuntimeError("测试下载失败")
        if self.image_size > max_bytes:
            raise RuntimeError("测试媒体超限")
        return b"x" * self.image_size, "image/jpeg"

    async def enrich_status_media(self, p: NitterPost) -> NitterPost:
        if self.fail_enrich:
            raise RuntimeError("测试视频详情失败")
        return p


class FakeQQ:
    def __init__(self) -> None:
        self.forward_attempts = []
        self.events = []
        self.notices = []
        self.contexts = []
        self.media_paths = []
        self.fail_group = lambda group_id, nodes: False
        self.fail_notice = False

    async def rpc_call(self, method, plugin_id, payload, timeout_ms=None):
        del method, plugin_id, timeout_ms
        capability, args = payload["capability"], payload["args"]
        if capability == "api.list":
            return {"success": True, "apis": [{
                "name": "adapter.napcat.action.call", "version": "1",
                "full_name": "test.qq.adapter.napcat.action.call",
            }]}
        if capability == "api.call":
            request = args["args"]
            action, params = request["action_name"], request["params"]
            self.events.append(action)
            if action == "get_login_info":
                data = {"user_id": 123456}
            elif action == "send_private_msg":
                raise AssertionError("订阅缓存阶段不得向机器人自己发送任何消息")
            elif action == "send_group_forward_msg":
                nodes = params["messages"]
                for node in nodes:
                    assert node["type"] == "node" and "id" not in node["data"]
                    assert node["data"]["user_id"] == "123456"
                    for segment in node["data"]["content"]:
                        if segment["type"] != "text":
                            path = Path(segment["data"]["file"])
                            assert path.is_absolute() and path.is_file()
                            self.media_paths.append(str(path))
                self.forward_attempts.append((params["group_id"], nodes))
                if self.fail_group(params["group_id"], nodes):
                    return {"success": True, "result": {
                        "status": "failed", "retcode": 1200, "message": "测试群转发失败",
                    }}
                data = {"message_id": "500"}
            else:
                raise AssertionError(action)
            return {"success": True, "result": {"status": "ok", "retcode": 0, "data": data}}
        if capability == "chat.open_session":
            return {"success": True, "stream_id": "group-" + args["group_id"]}
        if capability == "send.forward":
            self.events.append("failed_links_notice")
            self.notices.append(args["messages"])
            return {"success": not self.fail_notice}
        if capability == "maisaka.context.append":
            self.contexts.append(args["visible_text"])
            return {"success": True}
        raise AssertionError("订阅不应调用旧发送接口：" + capability)

    @staticmethod
    def text(nodes) -> str:
        return "\n".join(
            segment["data"]["text"]
            for node in nodes for segment in node["data"]["content"]
            if segment["type"] == "text"
        )


@asynccontextmanager
async def running_plugin(qq: FakeQQ, client: TimelineClient, **sections) -> AsyncIterator[Any]:
    with TemporaryDirectory() as temp_dir:
        plugin = create_plugin()
        use_temporary_config_mirror(plugin, temp_dir)
        config = {
            "plugin": {"enabled": False, "config_version": "1.7.0"},
            "nitter": {"accounts": ["example"], "send_existing_on_first_run": True},
            "delivery": {"qq_groups": ["10001"], "forward_batch_threshold": 50},
        }
        for name, values in sections.items():
            config.setdefault(name, {}).update(values)
        plugin.set_plugin_config(config)
        plugin._set_context(PluginContext(
            PLUGIN_ID, rpc_call=qq.rpc_call,
            paths=PluginPaths(data_dir=Path(temp_dir) / "data", runtime_dir=Path(temp_dir) / "runtime"),
        ))
        await plugin.on_load()
        try:
            with patch.object(plugin, "_create_client", return_value=client):
                yield plugin
        finally:
            await plugin.on_unload()
        assert "send_private_msg" not in qq.events


class SubscriptionForwardingTests(IsolatedAsyncioTestCase):
    async def test_media_size_batches_preserve_order_and_fill_exact_limit(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([
            post(i, [MediaAttachment(f"https://media/{i}.jpg", "image")]) for i in range(1, 5)
        ])
        sizes = [1, 3, 8, 2]

        async def download(url, max_bytes):
            size = sizes[int(url.rsplit("/", 1)[1].split(".")[0]) - 1] * 1024 * 1024
            self.assertLessEqual(size, max_bytes)
            return b"x" * size, "image/jpeg"

        client.download_media = download
        async with running_plugin(qq, client) as plugin:
            await plugin._collect_updates()
            await plugin._forward_once()
        self.assertEqual([len(nodes) for _, nodes in qq.forward_attempts], [2, 2])
        for (_, nodes), ids in zip(qq.forward_attempts, ([1, 2], [3, 4]), strict=True):
            self.assertEqual(
                [int(n["data"]["content"][0]["data"]["text"].rsplit("/status/", 1)[1]) for n in nodes], ids,
            )

    async def test_media_size_counts_video_and_files_and_keeps_large_post_whole(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([
            post(1, [MediaAttachment("https://media/1.jpg", "image")]),
            post(2, [
                MediaAttachment("https://media/2.mp4", "video"),
                MediaAttachment("https://media/2.bin", "file"),
            ]),
            post(3, [MediaAttachment("https://media/3.jpg", "image")]),
        ])
        client.image_size = 600 * 1024
        async with running_plugin(qq, client, delivery={"max_media_size_mb": 1}) as plugin:
            summary = await plugin._scan_once()
        self.assertEqual(summary.forwarded_posts, 3)
        self.assertEqual([len(nodes) for _, nodes in qq.forward_attempts], [1, 3, 1])
        self.assertEqual(
            [s["type"] for n in qq.forward_attempts[1][1] for s in n["data"]["content"]],
            ["text", "video", "file"],
        )

    async def test_single_post_always_forwarded_once_and_no_private_sends(self) -> None:
        qq = FakeQQ()
        async with running_plugin(qq, TimelineClient([post(1)])) as plugin:
            first = await plugin._scan_once()
            second = await plugin._scan_once()
            self.assertTrue(plugin._require_state_store().is_seen("example", "1"))
        self.assertEqual((first.forwarded_posts, second.forwarded_posts), (1, 0))
        self.assertEqual(len(qq.forward_attempts), 1)
        self.assertIn("原文：https://x.com/example/status/1", qq.text(qq.forward_attempts[0][1]))

    async def test_scan_sends_all_unseen_posts_in_order_despite_old_limit(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(i) for i in range(16, 0, -1)])
        async with running_plugin(qq, client, nitter={"max_posts_per_scan": 1}) as plugin:
            plugin._require_state_store().mark_seen("example", "1")
            summary = await plugin._scan_once()
            repeated = await plugin._scan_once()
        self.assertEqual((summary.forwarded_posts, repeated.forwarded_posts), (15, 0))
        self.assertEqual(len(qq.forward_attempts), 1)
        self.assertEqual(
            qq.text(qq.forward_attempts[0][1]),
            "\n".join(plugin._format_post_text(post(i)) for i in range(2, 17)),
        )

    async def test_images_video_and_text_stay_in_one_record_with_real_local_files(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([
            post(1, [MediaAttachment("https://media/1.jpg", "image"), MediaAttachment("https://media/2.jpg", "image")]),
            post(2, [MediaAttachment("https://media/video.mp4", "video")]), post(3),
        ])
        async with running_plugin(qq, client) as plugin:
            await plugin._collect_updates()
            self.assertFalse(qq.events)
            self.assertEqual(len(client.downloads), 3)
            cached_paths = [
                plugin._require_media_cache().path(media)
                for item in plugin._require_state_store().pending_deliveries() for media in item.staged.media
            ]
            self.assertTrue(all(path.is_file() for path in cached_paths))
            summary = await plugin._forward_once()
            self.assertTrue(all(not path.exists() for path in cached_paths))
        self.assertEqual(summary.forwarded_posts, 3)
        self.assertEqual(len(qq.forward_attempts), 1)
        self.assertEqual(
            [s["type"] for n in qq.forward_attempts[0][1] for s in n["data"]["content"]],
            ["text", "image", "image", "text", "video", "text"],
        )

    async def test_image_total_does_not_split_group_record(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1, [
            MediaAttachment("https://media/1.jpg", "image"), MediaAttachment("https://media/2.jpg", "image"),
        ])])
        client.image_size = 600 * 1024
        async with running_plugin(qq, client, delivery={"max_media_size_mb": 1}) as plugin:
            await plugin._scan_once()
        self.assertEqual(len(qq.forward_attempts), 1)
        self.assertEqual(len(qq.forward_attempts[0][1]), 1)

    async def test_two_groups_share_cache_and_download_only_once(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1), post(2, [MediaAttachment("https://media/2.jpg", "image")])])
        async with running_plugin(qq, client, delivery={"qq_groups": []}) as plugin:
            subscriptions = plugin._require_subscription_store()
            subscriptions.subscribe("10001", "example")
            subscriptions.subscribe("10002", "example", media_only=True)
            summary = await plugin._scan_once()
        self.assertEqual(summary.forwarded_posts, 2)
        self.assertEqual(client.downloads, ["https://media/2.jpg"])
        self.assertEqual([group for group, nodes in qq.forward_attempts], ["10001", "10002"])
        self.assertEqual([len(nodes) for group, nodes in qq.forward_attempts], [2, 1])
        self.assertEqual(qq.media_paths[0], qq.media_paths[1])

    async def test_partial_media_failure_sends_body_then_one_links_notice(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1, [MediaAttachment("https://media/bad.mp4", "video")]), post(2)])
        async with running_plugin(qq, client) as plugin:
            await plugin._scan_once()
            await plugin._scan_once()
        self.assertEqual(len(qq.forward_attempts), 1)
        self.assertEqual(len(qq.notices), 1)
        self.assertEqual(qq.events[-1], "failed_links_notice")
        self.assertEqual(qq.notices[0][-1]["segments"][0]["content"], "https://x.com/example/status/1")

    async def test_disabled_failed_links_notice_still_completes_delivery_and_cleanup(self) -> None:
        for failure in ("media", "group", "prepare"):
            with self.subTest(failure=failure):
                qq = FakeQQ()
                client = TimelineClient([post(1, [MediaAttachment("https://media/1.jpg", "image")])])
                if failure == "media":
                    client.posts = [post(1, [MediaAttachment("https://media/bad.jpg", "image")])]
                elif failure == "group":
                    qq.fail_group = lambda group, nodes: True
                else:
                    client.posts = [replace(post(1), has_video=True)]
                    client.fail_enrich = True
                async with running_plugin(qq, client, nitter={"send_failed_links_forward": False}) as plugin:
                    await plugin._scan_once()
                    await plugin._scan_once()
                    self.assertEqual(plugin._require_state_store().pending_post_count(), 0)
                    self.assertFalse(list(plugin._require_media_cache().root.glob("*/*")))
                self.assertFalse(qq.notices)
                self.assertNotIn("failed_links_notice", qq.events)

    async def test_failed_image_does_not_stop_later_images_or_posts(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1, [
            MediaAttachment("https://media/bad.jpg", "image"), MediaAttachment("https://media/ok.jpg", "image"),
        ]), post(2)])
        async with running_plugin(qq, client) as plugin:
            await plugin._scan_once()
        self.assertEqual(len(set(qq.media_paths)), 1)
        self.assertEqual(len(qq.notices), 1)

    async def test_bisection_reuses_cache_and_syncs_only_successful_posts(self) -> None:
        qq = FakeQQ()
        qq.fail_group = lambda group, nodes: "/status/2" in qq.text(nodes)
        client = TimelineClient([
            post(i, [MediaAttachment(f"https://media/{i}.jpg", "image")]) for i in range(1, 4)
        ])
        async with running_plugin(qq, client, context_sync={"enabled": True}) as plugin:
            summary = await plugin._scan_once()
            await plugin._scan_once()
        self.assertEqual((summary.forwarded_posts, summary.dropped_posts), (2, 1))
        self.assertEqual(len(client.downloads), 3)
        self.assertEqual(len(set(qq.media_paths)), 3)
        self.assertEqual(len(qq.forward_attempts), 5)
        self.assertEqual(len(qq.notices), 1)
        self.assertNotIn("正文 2", "\n".join(qq.contexts))
        self.assertNotIn("x.com", "\n".join(qq.contexts))

    async def test_split_depth_zero_or_one_is_respected(self) -> None:
        for depth, attempts in ((0, 1), (1, 3)):
            qq = FakeQQ()
            qq.fail_group = lambda group, nodes: True
            qq.fail_notice = True
            async with running_plugin(
                qq, TimelineClient([post(i) for i in range(1, 5)]),
                delivery={"forward_split_attempts": depth},
            ) as plugin:
                summary = await plugin._scan_once()
            self.assertEqual(summary.dropped_posts, 4)
            self.assertEqual(len(qq.forward_attempts), attempts)
            self.assertEqual(len(qq.notices), 1)

    async def test_recovered_split_has_no_failed_links_notice(self) -> None:
        qq = FakeQQ()
        qq.fail_group = lambda group, nodes: len(nodes) > 1
        async with running_plugin(qq, TimelineClient([post(1), post(2)])) as plugin:
            summary = await plugin._scan_once()
        self.assertEqual(summary.forwarded_posts, 2)
        self.assertFalse(qq.notices)

    async def test_node_limit_splits_between_posts(self) -> None:
        qq = FakeQQ()
        async with running_plugin(qq, TimelineClient([post(i) for i in range(1, 8)])) as plugin:
            with patch("plugins.NitterToMaiBot.plugin.MAX_SUBSCRIPTION_FORWARD_NODES", 3):
                await plugin._scan_once()
        self.assertEqual([len(nodes) for group, nodes in qq.forward_attempts], [3, 3, 1])

    async def test_quiet_queue_survives_load_and_flushes_without_qq_calls_during_checks(self) -> None:
        qq = FakeQQ()
        async with running_plugin(qq, TimelineClient([post(1), post(2)])) as plugin:
            with patch.object(plugin, "_is_quiet_period", return_value=True):
                quiet = await plugin._scan_once()
            self.assertEqual(quiet.deferred_posts, 2)
            self.assertFalse(qq.events)
            plugin._require_state_store().load()
            with patch.object(plugin, "_is_quiet_period", return_value=False):
                result = await plugin._scan_once()
        self.assertEqual(result.forwarded_posts, 2)

    async def test_hls_cache_is_preserved_until_forwarding_and_runtime_file_is_removed(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1, [
            MediaAttachment("https://media/master.m3u8", "video", "application/vnd.apple.mpegurl"),
        ])])
        async with running_plugin(qq, client) as plugin:
            path = plugin.ctx.paths.runtime_dir / "test.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test video")
            with patch.object(plugin, "_materialize_hls_video", new_callable=AsyncMock, return_value=path):
                await plugin._collect_updates()
            cached = plugin._require_state_store().pending_deliveries()[0].staged.media[0]
            cached_path = plugin._require_media_cache().path(cached)
            self.assertFalse(path.exists())
            self.assertEqual(cached_path.read_bytes(), b"test video")
            self.assertFalse(qq.events)
            await plugin._forward_once()
            self.assertFalse(cached_path.exists())
        self.assertTrue(any(
            s["type"] == "video" for n in qq.forward_attempts[0][1] for s in n["data"]["content"]
        ))

    async def test_video_preparation_failure_still_has_a_final_links_notice(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([replace(post(1), has_video=True)])
        client.fail_enrich = True
        async with running_plugin(qq, client) as plugin:
            await plugin._scan_once()
        self.assertFalse(qq.forward_attempts)
        self.assertEqual(len(qq.notices), 1)

    async def test_unresolved_group_retains_cache_until_other_groups_complete(self) -> None:
        qq = FakeQQ()
        client = TimelineClient([post(1, [MediaAttachment("https://media/1.jpg", "image")])])
        async with running_plugin(qq, client, delivery={"qq_groups": ["10001", "10002"]}) as plugin:
            await plugin._collect_updates()
            item = plugin._require_state_store().pending_deliveries()[0]
            cached_path = plugin._require_media_cache().path(item.staged.media[0])
            original = plugin._resolve_group_stream

            async def resolve(group, cache):
                if group == "10002":
                    raise RuntimeError("测试群暂不可用")
                return await original(group, cache)

            with patch.object(plugin, "_resolve_group_stream", side_effect=resolve):
                await plugin._forward_once()
            self.assertTrue(cached_path.exists())
            await plugin._forward_once()
            self.assertFalse(cached_path.exists())
            self.assertEqual([group for group, nodes in qq.forward_attempts], ["10001", "10002"])
            self.assertEqual(client.downloads, ["https://media/1.jpg"])
