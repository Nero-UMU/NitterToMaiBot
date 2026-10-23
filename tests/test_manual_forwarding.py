"""手动多推文合并转发：图片和视频同包，缓存与订阅隔离。"""

from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from plugins.NitterToMaiBot.models import MediaAttachment
from plugins.NitterToMaiBot.tests.test_subscription_forwarding import FakeQQ, TimelineClient, post, running_plugin


class PreviewQQ(FakeQQ):
    def __init__(self) -> None:
        super().__init__()
        self.records = []
        self.preview_paths = []
        self.send_success = True

    async def rpc_call(self, method, plugin_id, payload, timeout_ms=None):
        if payload["capability"] == "send.forward":
            self.records.append(payload["args"])
            for node in payload["args"]["messages"]:
                for segment in node["segments"]:
                    if segment["type"] == "dict":
                        path = Path(segment["data"]["data"]["file"])
                        assert path.is_absolute() and path.is_file()
                        self.preview_paths.append(path)
            return {"success": self.send_success}
        if payload["capability"] in {"send.text", "send.image", "send.custom"}:
            raise AssertionError("手动合并转发不得把正文或附件单独发送")
        return await super().rpc_call(method, plugin_id, payload, timeout_ms)


class ManualForwardingTests(IsolatedAsyncioTestCase):
    async def test_five_requested_posts_include_images_and_video_in_one_record(self) -> None:
        for stream in ("qq-group-stream", "qq-private-stream"):
            with self.subTest(stream=stream):
                qq = PreviewQQ()
                posts = [replace(post(i), account="CatNIP_Catgrass") for i in range(5, 0, -1)]
                posts[0] = replace(posts[0], media=[MediaAttachment("https://media/5.jpg", "image")])
                posts[2] = replace(posts[2], has_video=True, media=[MediaAttachment("https://media/3.mp4", "video")])
                async with running_plugin(qq, TimelineClient(posts), delivery={"forward_batch_threshold": 1}) as plugin:
                    result = await plugin.handle_posts(
                        stream_id=stream, matched_groups={"account": "@CatNIP_Catgrass", "count": "5"},
                    )
                    self.assertTrue(result[0], result[1])
                    self.assertEqual(plugin._require_state_store().pending_post_count(), 0)
                self.assertEqual(len(qq.records), 1)
                self.assertEqual(qq.records[0]["stream_id"], stream)
                nodes = qq.records[0]["messages"]
                self.assertEqual(len(nodes), 6)
                self.assertEqual(
                    [s["data"]["type"] for n in nodes for s in n["segments"] if s["type"] == "dict"],
                    ["image", "video"],
                )
                texts = [s["content"] for n in nodes for s in n["segments"] if s["type"] == "text"]
                self.assertEqual(len(texts), 5)
                self.assertIn("正文 5", texts[0])
                self.assertIn("正文 1", texts[-1])
                self.assertFalse(any(p.exists() for p in qq.preview_paths))

    async def test_total_image_size_splits_manual_forward_in_order(self) -> None:
        qq = PreviewQQ()
        client = TimelineClient([post(i, [MediaAttachment(f"https://media/{i}.jpg", "image")]) for i in range(1, 6)])
        client.image_size = 600 * 1024
        async with running_plugin(qq, client, delivery={"max_media_size_mb": 1, "forward_batch_threshold": 1}) as plugin:
            await plugin._send_posts_preview(client, client.posts, "qq-group-stream")
        self.assertEqual(len(qq.records), 5)
        self.assertEqual([r["messages"][0]["message_id"] for r in qq.records], ["1", "2", "3", "4", "5"])

    async def test_manual_size_limit_preserves_oversized_post_and_video(self) -> None:
        qq = PreviewQQ()
        client = TimelineClient([
            post(1, [MediaAttachment("https://media/1.jpg", "image")]),
            post(2, [
                MediaAttachment("https://media/2.jpg", "image"),
                MediaAttachment("https://media/2.mp4", "video"),
            ]),
            post(3, [MediaAttachment("https://media/3.jpg", "image")]),
        ])
        client.image_size = 600 * 1024
        async with running_plugin(qq, client, delivery={"max_media_size_mb": 1, "forward_batch_threshold": 1}) as plugin:
            await plugin._send_posts_preview(client, client.posts, "qq-group-stream")
        self.assertEqual([len(r["messages"]) for r in qq.records], [1, 2, 1])
        self.assertEqual([n["message_id"] for n in qq.records[1]["messages"]], ["2", "2-media-1"])

    async def test_manual_hls_cache_does_not_remove_pending_subscription_media(self) -> None:
        qq = PreviewQQ()
        queued_post = post(1, [MediaAttachment("https://media/1.jpg", "image")])
        client = TimelineClient([queued_post])
        async with running_plugin(qq, client, delivery={"forward_batch_threshold": 1}) as plugin:
            await plugin._collect_updates()
            item = plugin._require_state_store().pending_deliveries()[0]
            subscription_path = plugin._require_media_cache().path(item.staged.media[0])
            video = replace(queued_post, media=[MediaAttachment("https://media/master.m3u8", "video")], has_video=True)
            source = plugin.ctx.paths.runtime_dir / "source.mp4"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"video")
            with patch.object(plugin, "_materialize_hls_video", AsyncMock(return_value=source)):
                await plugin._send_posts_preview(client, [video, post(2)], "qq-private-stream")
            self.assertTrue(subscription_path.is_file())
            self.assertFalse(source.exists())
            self.assertFalse(any(p.exists() for p in qq.preview_paths))
            self.assertEqual(plugin._require_state_store().pending_post_count(), 1)

    async def test_failed_forward_cleans_preview_files_without_sending_separately(self) -> None:
        qq = PreviewQQ()
        qq.send_success = False
        client = TimelineClient([post(1, [MediaAttachment("https://media/1.jpg", "image")]), post(2)])
        async with running_plugin(qq, client, delivery={"forward_batch_threshold": 1}) as plugin:
            with self.assertRaisesRegex(RuntimeError, "合并转发"):
                await plugin._send_posts_preview(client, client.posts, "qq-group-stream")
            self.assertFalse(any(p.exists() for p in qq.preview_paths))
        self.assertEqual(len(qq.records), 1)

    async def test_node_limit_does_not_separate_a_video_from_its_post(self) -> None:
        qq = PreviewQQ()
        client = TimelineClient([
            post(1), post(2, [MediaAttachment("https://media/2.mp4", "video")]), post(3), post(4),
        ])
        async with running_plugin(qq, client, delivery={"forward_batch_threshold": 1}) as plugin:
            with patch("plugins.NitterToMaiBot.plugin.MAX_SUBSCRIPTION_FORWARD_NODES", 3):
                await plugin._send_posts_preview(client, client.posts, "qq-group-stream")
        self.assertEqual([len(r["messages"]) for r in qq.records], [3, 2])
        self.assertEqual([n["message_id"] for n in qq.records[0]["messages"]], ["1", "2", "2-media-1"])

    async def test_failed_media_is_reported_inside_record_and_other_media_remain(self) -> None:
        qq = PreviewQQ()
        client = TimelineClient([post(1, [
            MediaAttachment("https://media/bad.jpg", "image"), MediaAttachment("https://media/1.mp4", "video"),
        ]), post(2)])
        async with running_plugin(qq, client, delivery={"forward_batch_threshold": 1}) as plugin:
            await plugin._send_posts_preview(client, client.posts, "qq-group-stream")
        self.assertEqual(len(qq.records), 1)
        nodes = qq.records[0]["messages"]
        self.assertIn("部分媒体获取失败", nodes[0]["segments"][0]["content"])
        self.assertEqual(nodes[1]["segments"][0]["data"]["type"], "video")
