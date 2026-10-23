"""发送回执、图文上下文和用户引用之间必须保持可恢复的关联。"""

from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from PIL import Image

import asyncio
import base64

from plugins.NitterToMaiBot.context_history import ContextHistory, SentContext, encode_image_preview
from plugins.NitterToMaiBot.models import MediaAttachment
from plugins.NitterToMaiBot.tests.test_subscription_forwarding import FakeQQ, TimelineClient, post, running_plugin


def image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return output.getvalue()


class SyncClient(TimelineClient):
    async def download_media(self, url, max_bytes):
        if "bad" in url:
            raise RuntimeError("测试图片下载失败")
        self.downloads.append(url)
        return image_bytes(), "image/png"


class SyncQQ(FakeQQ):
    def __init__(self) -> None:
        super().__init__()
        self.context_args = []
        self.sends = []
        self.fail_send = False
        self.fail_context = False
        self.fail_vlm = False

    async def rpc_call(self, method, plugin_id, payload, timeout_ms=None):
        capability, args = payload["capability"], payload["args"]
        if capability in {"send.text", "send.hybrid", "send.forward", "send.custom"}:
            self.sends.append((capability, args))
            return {"success": not self.fail_send, "sent": not self.fail_send, "message_id": "1414286799"}
        if capability == "maisaka.context.append":
            self.context_args.append(args)
            return {"success": not self.fail_context, "error": "测试注入失败" if self.fail_context else ""}
        if capability == "llm.generate":
            assert args["task_name"] == "vlm"
            if self.fail_vlm:
                return {"success": False, "error": "测试 VLM 未配置"}
            return {"success": True, "response": "一张红色的小图片。"}
        return await super().rpc_call(method, plugin_id, payload, timeout_ms)


def quoted_message(stream_id="qq-private-stream", message_id="1929947748", target_id="1414286799"):
    return {
        "platform": "qq",
        "session_id": stream_id,
        "message_id": message_id,
        "raw_message": [
            {"type": "reply", "data": {"target_message_id": target_id}},
            {"type": "text", "data": "还有完整版"},
        ],
    }


class ContextSyncTests(IsolatedAsyncioTestCase):
    async def test_link_preview_syncs_real_id_and_image_then_restores_quote_after_reload(self) -> None:
        qq = SyncQQ()
        client = SyncClient([post(1, [MediaAttachment("https://media/1.png", "image")])])
        async with running_plugin(qq, client, context_sync={"enabled": True}) as plugin:
            await plugin._send_post_preview(client, client.posts[0], "qq-private-stream")
            self.assertTrue(qq.sends[0][1]["return_details"])
            synced = qq.context_args[0]
            self.assertEqual(synced["message_id"], "1414286799")
            self.assertIn('msg_id="1414286799"', synced["segments"][0]["content"])
            self.assertIn('is_self_message="true"', synced["segments"][0]["content"])
            images = [s for s in synced["segments"] if s["type"] == "image"]
            self.assertEqual(len(images), 1)
            with Image.open(BytesIO(base64.b64decode(images[0]["binary_data_base64"]))) as image:
                self.assertEqual(image.size, (8, 8))

            # 模拟插件重载：重新读取磁盘，不能依赖上一个实例的聊天历史。
            await plugin.on_unload()
            await plugin.on_load()
            qq.context_args.clear()
            with patch.object(plugin, "_parse_and_send_status", AsyncMock()) as parse:
                result = await plugin.handle_auto_parse_status(quoted_message())
            self.assertEqual(result, {"action": "continue"})
            parse.assert_not_awaited()
            self.assertTrue(qq.context_args)
            self.assertIn("1929947748", qq.context_args[-1]["visible_text"])
            self.assertIn("正文 1", qq.context_args[-1]["visible_text"])
            self.assertEqual(len(qq.sends), 1)

    async def test_subscription_partial_media_still_syncs_delivered_body_and_good_image(self) -> None:
        qq = SyncQQ()
        client = SyncClient(
            [
                post(
                    1,
                    [
                        MediaAttachment("https://media/bad.png", "image"),
                        MediaAttachment("https://media/good.png", "image"),
                    ],
                )
            ]
        )
        async with running_plugin(qq, client, context_sync={"enabled": True}) as plugin:
            await plugin._scan_once()
            contexts = [a for a in qq.context_args if a["message_id"] == "500"]
            self.assertTrue(contexts)
            self.assertIn("正文 1", contexts[0]["visible_text"])
            self.assertIn("失败", contexts[0]["visible_text"])
            self.assertEqual(sum(s["type"] == "image" for s in contexts[0]["segments"]), 1)

    async def test_manual_forward_retains_images_after_temporary_cache_is_cleaned(self) -> None:
        qq = SyncQQ()
        client = SyncClient([post(1, [MediaAttachment("https://media/1.png", "image")]), post(2)])
        async with running_plugin(
            qq, client, context_sync={"enabled": True}, delivery={"forward_batch_threshold": 1}
        ) as plugin:
            await plugin._send_posts_preview(client, client.posts, "qq-private-stream")
            qq.context_args.clear()
            await plugin.handle_auto_parse_status(quoted_message())
            self.assertIn("正文 1", qq.context_args[0]["visible_text"])
            self.assertIn("正文 2", qq.context_args[0]["visible_text"])
            self.assertTrue(any(s["type"] == "image" for a in qq.context_args for s in a["segments"]))

    async def test_failed_send_and_disabled_sync_do_not_create_quote_context(self) -> None:
        for enabled, failed in ((True, True), (False, False)):
            with self.subTest(enabled=enabled, failed=failed):
                qq = SyncQQ()
                qq.fail_send = failed
                client = SyncClient([post(1)])
                async with running_plugin(qq, client, context_sync={"enabled": enabled}) as plugin:
                    if failed:
                        with self.assertRaises(RuntimeError):
                            await plugin._send_post_preview(client, client.posts[0], "qq-private-stream")
                    else:
                        await plugin._send_post_preview(client, client.posts[0], "qq-private-stream")
                    await plugin.handle_auto_parse_status(quoted_message())
                    self.assertEqual(qq.context_args, [])

    async def test_quote_lookup_is_scoped_to_stream_and_same_event_is_not_injected_twice(self) -> None:
        qq = SyncQQ()
        async with running_plugin(qq, SyncClient([post(1)]), context_sync={"enabled": True}) as plugin:
            await plugin._send_post_preview(SyncClient([]), post(1), "qq-private-stream")
            qq.context_args.clear()
            await plugin.handle_auto_parse_status(quoted_message(stream_id="other-stream"))
            await plugin.handle_auto_parse_status(quoted_message(target_id="unknown"))
            self.assertEqual(qq.context_args, [])
            await plugin.handle_auto_parse_status(quoted_message())
            count = len(qq.context_args)
            self.assertGreater(count, 0)
            await plugin.handle_auto_parse_status(quoted_message())
            self.assertEqual(len(qq.context_args), count)

    async def test_context_failure_can_be_recovered_by_quote_without_resending(self) -> None:
        qq = SyncQQ()
        qq.fail_context = True
        async with running_plugin(qq, SyncClient([post(1)]), context_sync={"enabled": True}) as plugin:
            await plugin._send_post_preview(SyncClient([]), post(1), "qq-private-stream")
            qq.fail_context = False
            qq.context_args.clear()
            await plugin.handle_auto_parse_status(quoted_message())
            self.assertTrue(qq.context_args)
            self.assertEqual(len(qq.sends), 1)

    async def test_text_planner_gets_persisted_vlm_description_on_quote(self) -> None:
        qq = SyncQQ()
        client = SyncClient([post(1, [MediaAttachment("https://media/1.png", "image")])])
        async with running_plugin(qq, client, context_sync={"enabled": True}) as plugin:
            await plugin._send_post_preview(client, client.posts[0], "qq-private-stream")
            await asyncio.gather(*plugin._context_image_tasks.values())
            await plugin.on_unload()
            await plugin.on_load()
            qq.context_args.clear()
            await plugin.handle_auto_parse_status(quoted_message())
            self.assertIn("一张红色的小图片", qq.context_args[-1]["visible_text"])
            images = [s for s in qq.context_args[-1]["segments"] if s["type"] == "image"]
            self.assertIn("红色", images[0]["description"])

    async def test_vlm_failure_keeps_explicit_unknown_image_and_does_not_resend(self) -> None:
        qq = SyncQQ()
        qq.fail_vlm = True
        client = SyncClient([post(1, [MediaAttachment("https://media/1.png", "image")])])
        async with running_plugin(qq, client, context_sync={"enabled": True}) as plugin:
            await plugin._send_post_preview(client, client.posts[0], "qq-private-stream")
            await asyncio.gather(*plugin._context_image_tasks.values())
            qq.context_args.clear()
            await plugin.handle_auto_parse_status(quoted_message())
            self.assertIn("画面尚未识别", qq.context_args[-1]["visible_text"])
            self.assertNotIn("红色", qq.context_args[-1]["visible_text"])
            self.assertEqual(len(qq.sends), 1)

    async def test_history_prunes_old_records_and_image_batches_keep_real_id(self) -> None:
        with TemporaryDirectory() as directory:
            history = ContextHistory(Path(directory) / "context.db")
            with patch("plugins.NitterToMaiBot.context_history.MAX_CONTEXT_RECORDS", 2):
                for index in range(3):
                    history.save(SentContext("stream", str(index), "12:00", "summary", "full", []))
            self.assertIsNone(history.get("stream", "0"))
            self.assertIsNotNone(ContextHistory(history.path).get("stream", "2"))
            self.assertIsNone(history.get("other", "2"))
        record = SentContext(
            "stream",
            "1414286799",
            "12:00",
            "summary",
            "full",
            [{"label": str(i), "content": "A" * 200} for i in range(5)],
        )
        with patch("plugins.NitterToMaiBot.context_history.MAX_CONTEXT_BATCH_CHARS", 600):
            batches = record.batches(full=True)
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(s["type"] == "image" for b in batches for s in b), 5)
        self.assertTrue(all('msg_id="1414286799"' in b[0]["content"] for b in batches))

    async def test_preview_is_resized_and_source_is_unchanged(self) -> None:
        output = BytesIO()
        Image.new("RGB", (2048, 1024), "red").save(output, format="PNG")
        source = output.getvalue()
        preview = encode_image_preview(source)
        with Image.open(BytesIO(base64.b64decode(preview))) as image:
            self.assertEqual(image.size, (1024, 512))
        self.assertEqual(output.getvalue(), source)

    async def test_summary_and_full_information_respect_existing_text_setting(self) -> None:
        for mode in ("摘要", "完整文本"):
            qq = SyncQQ()
            p = replace(post(1), text="x" * 150)
            async with running_plugin(
                qq,
                SyncClient([p]),
                context_sync={
                    "enabled": True,
                    "detail_level": mode,
                    "summary_text_limit": 100,
                },
            ) as plugin:
                await plugin._send_post_preview(SyncClient([]), p, "qq-private-stream")
                visible = qq.context_args[0]["visible_text"]
                self.assertIn("x" * (100 if mode == "摘要" else 150), visible)
                if mode == "摘要":
                    self.assertNotIn("x" * 101, visible)
