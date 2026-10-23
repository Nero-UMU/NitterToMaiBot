"""单条推文正文与多张图片必须作为一条图文消息发送。"""

from dataclasses import replace
from unittest import IsolatedAsyncioTestCase

import base64

from plugins.NitterToMaiBot.models import MediaAttachment
from plugins.NitterToMaiBot.tests.test_subscription_forwarding import FakeQQ, TimelineClient, post, running_plugin


class SinglePostQQ(FakeQQ):
    def __init__(self) -> None:
        super().__init__()
        self.messages = []
        self.send_success = True

    async def rpc_call(self, method, plugin_id, payload, timeout_ms=None):
        capability = payload["capability"]
        if capability in {"send.hybrid", "send.text", "send.custom"}:
            self.messages.append((capability, payload["args"]))
            return {"success": self.send_success}
        if capability == "send.image":
            raise AssertionError("单条推文的图片不能逐张独立发送")
        return await super().rpc_call(method, plugin_id, payload, timeout_ms)


class SinglePostMessageTests(IsolatedAsyncioTestCase):
    async def test_default_posts_command_sends_text_and_all_images_in_one_message(self) -> None:
        for stream in ("qq-group-stream", "qq-private-stream"):
            with self.subTest(stream=stream):
                qq = SinglePostQQ()
                media = [MediaAttachment(f"https://media/{i}.jpg", "image") for i in range(3)]
                client = TimelineClient([replace(post(1, media), account="CatNIP_Catgrass")])
                async with running_plugin(qq, client, delivery={"forward_batch_threshold": 1}) as plugin:
                    result = await plugin.handle_posts(
                        stream_id=stream, matched_groups={"account": "@CatNIP_Catgrass"},
                    )
                    self.assertTrue(result[0], result[1])
                    self.assertEqual(plugin._require_state_store().pending_post_count(), 0)
                self.assertEqual([kind for kind, _ in qq.messages], ["send.hybrid"])
                args = qq.messages[0][1]
                self.assertEqual(args["stream_id"], stream)
                self.assertEqual([s["type"] for s in args["segments"]], ["text", "image", "image", "image"])
                self.assertIn("正文 1", args["segments"][0]["content"])
                self.assertTrue(all(base64.b64decode(s["content"]) == b"x" * 10 for s in args["segments"][1:]))

    async def test_failed_image_does_not_split_remaining_images_from_text(self) -> None:
        qq = SinglePostQQ()
        client = TimelineClient([post(1, [
            MediaAttachment("https://media/bad.jpg", "image"),
            MediaAttachment("https://media/2.jpg", "image"),
            MediaAttachment("https://media/3.jpg", "image"),
        ])])
        async with running_plugin(qq, client) as plugin:
            await plugin._send_post_preview(client, client.posts[0], "qq-group-stream")
        self.assertEqual(len(qq.messages), 1)
        self.assertEqual(qq.messages[0][0], "send.hybrid")
        self.assertEqual([s["type"] for s in qq.messages[0][1]["segments"]], ["text", "image", "image"])
        self.assertIn("图片获取失败", qq.messages[0][1]["segments"][0]["content"])

    async def test_plain_text_or_disabled_images_still_send_one_text_message(self) -> None:
        for send_images in (True, False):
            with self.subTest(send_images=send_images):
                qq = SinglePostQQ()
                client = TimelineClient([post(1, [] if send_images else [MediaAttachment("https://media/1.jpg", "image")])])
                async with running_plugin(qq, client, delivery={"send_images": send_images}) as plugin:
                    await plugin._send_post_preview(client, client.posts[0], "qq-private-stream")
                self.assertEqual([kind for kind, _ in qq.messages], ["send.text"])
                self.assertFalse(client.downloads)

    async def test_failed_hybrid_send_is_not_retried_as_separate_images(self) -> None:
        qq = SinglePostQQ()
        qq.send_success = False
        client = TimelineClient([post(1, [MediaAttachment("https://media/1.jpg", "image")])])
        async with running_plugin(qq, client) as plugin:
            with self.assertRaisesRegex(RuntimeError, "图文"):
                await plugin._send_post_preview(client, client.posts[0], "qq-group-stream")
        self.assertEqual([kind for kind, _ in qq.messages], ["send.hybrid"])

    async def test_video_is_sent_once_after_combined_text_and_images(self) -> None:
        qq = SinglePostQQ()
        client = TimelineClient([post(1, [
            MediaAttachment("https://media/1.jpg", "image"),
            MediaAttachment("https://media/1.mp4", "video"),
            MediaAttachment("https://media/2.jpg", "image"),
        ])])
        async with running_plugin(qq, client) as plugin:
            await plugin._send_post_preview(client, client.posts[0], "qq-private-stream")
        self.assertEqual([kind for kind, _ in qq.messages], ["send.hybrid", "send.custom"])
        self.assertEqual([s["type"] for s in qq.messages[0][1]["segments"]], ["text", "image", "image"])
