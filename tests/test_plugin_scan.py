"""插件扫描和 SDK 能力调用测试。"""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from maibot_sdk.context import PluginContext, PluginPaths

from plugins.NitterToMaiBot.models import MediaAttachment, NitterPost
from plugins.NitterToMaiBot.plugin import create_plugin
from plugins.NitterToMaiBot.tests.helpers import use_temporary_config_mirror


class _FakeNitterClient:
    """返回一条固定推文的 Nitter 客户端。"""

    post = NitterPost(
        account="example",
        post_id="123456",
        author="@example",
        text="测试推文",
        published_at=datetime(2026, 8, 8, 4, 4, 25, tzinfo=timezone.utc),
        url="http://127.0.0.1:8080/example/status/123456#m",
        has_video=True,
        media=[
            MediaAttachment("http://127.0.0.1:8080/pic/photo.jpg", "image", "image/jpeg"),
            MediaAttachment("https://video.twimg.com/video.mp4", "video", "video/mp4"),
        ],
    )

    def __init__(self, base_url: str, timeout_seconds: int, request_attempts: int) -> None:
        del base_url
        del request_attempts
        del timeout_seconds

    async def fetch_timeline(self, account: str) -> List[NitterPost]:
        self.post = NitterPost(
            account=account,
            post_id=self.post.post_id,
            author=self.post.author,
            text=self.post.text,
            published_at=self.post.published_at,
            url=self.post.url,
            has_video=self.post.has_video,
            media=self.post.media,
        )
        return [self.post]

    def profile_name(self, account: str) -> str:
        return f"{account} 显示名"

    async def enrich_status_media(self, post: NitterPost) -> NitterPost:
        return post

    async def download_media(self, media_url: str, max_bytes: int) -> Tuple[bytes, str]:
        del max_bytes
        if media_url.endswith(".jpg"):
            return b"fake-image", "image/jpeg"
        return b"fake-video", "video/mp4"

class _MultiAccountNitterClient:
    """为两个订阅账号各返回一条新推文。"""

    def __init__(self, base_url: str, timeout_seconds: int, request_attempts: int) -> None:
        del base_url
        del request_attempts
        del timeout_seconds

    async def fetch_timeline(self, account: str) -> List[NitterPost]:
        post_id = "1001" if account == "first" else "1002"
        return [
            NitterPost(
                account=account,
                post_id=post_id,
                author=f"@{account}",
                text=f"{account} 的测试推文",
                published_at=datetime(2026, 8, 8, int(post_id[-1]), tzinfo=timezone.utc),
                url=f"http://127.0.0.1:8080/{account}/status/{post_id}",
            )
        ]

    def profile_name(self, account: str) -> str:
        return f"{account} 显示名"


class PluginScanTests(IsolatedAsyncioTestCase):
    """验证扫描会通过真实 SDK 能力名打开群聊并发送。"""

    def test_post_text_uses_original_x_url(self) -> None:
        """群消息中的查看原文链接应指向 x.com，而不是 Nitter。"""

        message_text = create_plugin()._format_post_text(_FakeNitterClient.post)

        self.assertTrue(message_text.startswith("@example · 2026-08-08 12:04（北京时间）\n\n"))
        self.assertNotIn("【Nitter 推文更新】", message_text)
        self.assertIn("原文：https://x.com/example/status/123456", message_text)
        self.assertNotIn("twitter.com", message_text)
        self.assertNotIn("原文：http://127.0.0.1:8080", message_text)

    def test_retweet_text_identifies_source_account_and_author(self) -> None:
        post = NitterPost(
            account="elonmusk",
            post_id="123456",
            author="@OpenAI",
            text="测试转推",
            published_at=datetime(2026, 8, 8, 4, 4, 25, tzinfo=timezone.utc),
            url="http://127.0.0.1:8080/elonmusk/status/123456",
            is_retweet=True,
        )

        message_text = create_plugin()._format_post_text(post)

        self.assertTrue(message_text.startswith("@elonmusk 转推了 @OpenAI · 2026-08-08 12:04（北京时间）"))

    def test_quiet_period_uses_beijing_time_and_supports_cross_midnight(self) -> None:
        plugin = create_plugin()
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": False, "config_version": "1.5.3"},
                "quiet_hours": {
                    "enabled": True,
                    "start_time": "23:30",
                    "end_time": "06:00",
                }
            }
        )

        self.assertTrue(
            plugin._is_quiet_period(datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc))
        )
        self.assertTrue(
            plugin._is_quiet_period(datetime(2026, 8, 8, 21, 59, tzinfo=timezone.utc))
        )
        self.assertFalse(
            plugin._is_quiet_period(datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc))
        )

    async def test_translation_uses_selected_maibot_model_task(self) -> None:
        """开启翻译后应调用 SDK LLM 能力，并把中文结果附到原文下方。"""

        calls: List[Tuple[str, Dict[str, Any]]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del plugin_id
            del timeout_ms
            calls.append((method, payload))
            self.assertEqual(payload["capability"], "llm.generate")
            return {
                "success": True,
                "response": "这是一条测试推文。",
                "model_name": "configured-model",
            }

        plugin = create_plugin()
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": False, "config_version": "1.5.1"},
                "translation": {
                    "enabled": True,
                    "model": "utils",
                    "prompt": "请仅把推文翻译成简体中文。",
                },
            }
        )
        plugin._set_context(PluginContext("third-party.nitter-to-maibot", rpc_call=rpc_call))
        post = NitterPost(
            account="example",
            post_id="123456",
            author="@example",
            text="This is a test post.",
            published_at=datetime(2026, 8, 8, 4, 4, 25, tzinfo=timezone.utc),
            url="http://127.0.0.1:8080/example/status/123456",
        )

        translated_post = await plugin._prepare_post_translation(post)
        message_text = plugin._format_post_text(translated_post)

        self.assertEqual(post.translated_text, "")
        self.assertEqual(translated_post.translated_text, "这是一条测试推文。")
        self.assertIn("This is a test post.\n\n中文翻译：\n这是一条测试推文。", message_text)
        self.assertEqual(len(calls), 1)
        args = calls[0][1]["args"]
        self.assertEqual(args["model"], "utils")
        self.assertEqual(args["temperature"], 0.1)
        self.assertEqual(args["max_tokens"], 2048)
        self.assertEqual(
            args["prompt"][0],
            {"role": "system", "content": "请仅把推文翻译成简体中文。"},
        )
        self.assertEqual(args["prompt"][1], {"role": "user", "content": "This is a test post."})

    async def test_translation_failure_is_exposed(self) -> None:
        """模型调用失败时不能把未翻译正文当成成功翻译。"""

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            self.assertEqual(payload["capability"], "llm.generate")
            return {"success": False, "error": "测试模型不可用"}

        plugin = create_plugin()
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": False, "config_version": "1.5.0"},
                "translation": {"enabled": True, "model": "planner"},
            }
        )
        plugin._set_context(PluginContext("third-party.nitter-to-maibot", rpc_call=rpc_call))

        with self.assertRaisesRegex(RuntimeError, "测试模型不可用"):
            await plugin._prepare_post_translation(_FakeNitterClient.post)

    async def test_first_scan_can_forward_existing_post(self) -> None:
        calls: List[Tuple[str, Dict[str, Any]]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del plugin_id
            del timeout_ms
            calls.append((method, payload))
            capability = str(payload["capability"])
            if capability == "chat.open_session":
                return {"success": True, "stream_id": "qq-group-stream"}
            if capability in {"send.text", "send.image", "send.custom"}:
                return {"success": True}
            raise AssertionError(f"收到未预期的能力调用: {capability}")

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.0.0"},
                    "nitter": {
                        "base_url": "http://127.0.0.1:8080",
                        "accounts": ["example"],
                        "send_existing_on_first_run": True,
                    },
                    "delivery": {
                        "qq_groups": ["10001"],
                        "send_images": True,
                        "send_videos": True,
                        "send_other_files": True,
                    },
                }
            )
            context = PluginContext(
                "third-party.nitter-to-maibot",
                rpc_call=rpc_call,
                paths=PluginPaths(
                    data_dir=Path(temp_dir) / "data",
                    runtime_dir=Path(temp_dir) / "runtime",
                ),
            )
            plugin._set_context(context)
            await plugin.on_load()

            with patch("plugins.NitterToMaiBot.plugin.NitterClient", _FakeNitterClient):
                summary = await plugin._scan_once()

            await plugin.on_unload()

        self.assertEqual(summary.forwarded_posts, 1)
        capabilities = [str(payload["capability"]) for _method, payload in calls]
        self.assertEqual(
            capabilities,
            ["chat.open_session", "send.text", "send.image", "send.custom"],
        )
        file_payload = next(
            payload["args"]["content"]
            for _method, payload in calls
            if payload["capability"] == "send.custom"
        )
        self.assertEqual(file_payload["url"], "https://video.twimg.com/video.mp4")
        self.assertNotIn("base64", file_payload)

    async def test_scan_batches_multiple_accounts_for_same_group(self) -> None:
        """同一轮、同一目标群的多账号更新应合并为一条聊天记录。"""

        calls: List[Tuple[str, Dict[str, Any]]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del plugin_id
            del timeout_ms
            calls.append((method, payload))
            capability = str(payload["capability"])
            if capability == "chat.open_session":
                return {"success": True, "stream_id": "qq-group-stream"}
            if capability == "send.forward":
                return {"success": True}
            raise AssertionError(f"收到未预期的能力调用: {capability}")

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.2.0"},
                    "nitter": {
                        "base_url": "http://127.0.0.1:8080",
                        "accounts": ["first", "second"],
                        "send_existing_on_first_run": True,
                    },
                    "delivery": {
                        "qq_groups": ["10001"],
                        "forward_batch_threshold": 1,
                    },
                }
            )
            context = PluginContext(
                "third-party.nitter-to-maibot",
                rpc_call=rpc_call,
                paths=PluginPaths(
                    data_dir=Path(temp_dir) / "data",
                    runtime_dir=Path(temp_dir) / "runtime",
                ),
            )
            plugin._set_context(context)
            await plugin.on_load()

            with patch("plugins.NitterToMaiBot.plugin.NitterClient", _MultiAccountNitterClient):
                summary = await plugin._scan_once()

            await plugin.on_unload()

        self.assertEqual(summary.forwarded_posts, 2)
        self.assertEqual(
            [payload["capability"] for _method, payload in calls],
            ["chat.open_session", "send.forward"],
        )
        forward_messages = calls[1][1]["args"]["messages"]
        self.assertEqual([node["nickname"] for node in forward_messages], ["@first", "@second"])

    async def test_quiet_posts_are_persisted_then_sent_as_one_forward(self) -> None:
        """静默期间不发送，结束后的下一轮把多条积压合并为一条聊天记录。"""

        calls: List[Tuple[str, Dict[str, Any]]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del plugin_id
            del timeout_ms
            calls.append((method, payload))
            capability = str(payload["capability"])
            if capability == "chat.open_session":
                return {"success": True, "stream_id": "qq-group-stream"}
            if capability == "send.forward":
                return {"success": True}
            raise AssertionError(f"收到未预期的能力调用: {capability}")

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.5.3"},
                    "nitter": {
                        "base_url": "http://127.0.0.1:8080",
                        "accounts": ["first", "second"],
                        "send_existing_on_first_run": True,
                    },
                    "delivery": {
                        "qq_groups": ["10001"],
                        "forward_batch_threshold": 50,
                    },
                    "quiet_hours": {
                        "enabled": True,
                        "start_time": "00:00",
                        "end_time": "06:00",
                    },
                }
            )
            context = PluginContext(
                "third-party.nitter-to-maibot",
                rpc_call=rpc_call,
                paths=PluginPaths(
                    data_dir=Path(temp_dir) / "data",
                    runtime_dir=Path(temp_dir) / "runtime",
                ),
            )
            plugin._set_context(context)
            await plugin.on_load()

            with (
                patch("plugins.NitterToMaiBot.plugin.NitterClient", _MultiAccountNitterClient),
                patch.object(plugin, "_is_quiet_period", side_effect=[True, False]),
            ):
                quiet_summary = await plugin._scan_once()
                self.assertEqual(calls, [])
                self.assertEqual(quiet_summary.forwarded_posts, 0)
                self.assertEqual(quiet_summary.deferred_posts, 2)
                self.assertEqual(plugin._require_state_store().quiet_post_count(), 2)

                sent_summary = await plugin._scan_once()

            await plugin.on_unload()

        self.assertEqual(sent_summary.forwarded_posts, 2)
        self.assertEqual(sent_summary.deferred_posts, 0)
        self.assertEqual(
            [payload["capability"] for _method, payload in calls],
            ["chat.open_session", "send.forward"],
        )
        forward_messages = calls[1][1]["args"]["messages"]
        self.assertEqual([node["nickname"] for node in forward_messages], ["@first", "@second"])
