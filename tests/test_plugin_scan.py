"""插件扫描和 SDK 能力调用测试。"""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import asyncio

from maibot_sdk.context import PluginContext, PluginPaths

from plugins.NitterToMaiBot.config_mirror import subscription_revision
from plugins.NitterToMaiBot.models import MediaAttachment, NitterPost
from plugins.NitterToMaiBot.plugin import (
    DROPPED_FORWARD_TOKEN,
    FORWARD_TOKEN,
    PLUGIN_ID,
    create_plugin,
)
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
        plugin._set_context(PluginContext(PLUGIN_ID, rpc_call=rpc_call))
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

    async def test_translation_failure_marks_error(self) -> None:
        """模型返回失败时应标记翻译错误，不阻断后续投递。"""

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
        plugin._set_context(PluginContext(PLUGIN_ID, rpc_call=rpc_call))

        prepared_post = await plugin._prepare_post_translation(_FakeNitterClient.post)

        message_text = plugin._format_post_text(prepared_post)
        self.assertIsNot(prepared_post, _FakeNitterClient.post)
        self.assertEqual(prepared_post.translated_text, "翻译错误")
        self.assertIn("中文翻译：\n翻译错误", message_text)

    async def test_translation_exception_marks_error(self) -> None:
        """模型 RPC 超时时应标记翻译错误，不阻断后续投递。"""

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del payload
            del timeout_ms
            raise TimeoutError("测试翻译超时")

        plugin = create_plugin()
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": False, "config_version": "1.5.3"},
                "translation": {"enabled": True, "model": "utils"},
            }
        )
        plugin._set_context(PluginContext(PLUGIN_ID, rpc_call=rpc_call))

        prepared_post = await plugin._prepare_post_translation(_FakeNitterClient.post)

        message_text = plugin._format_post_text(prepared_post)
        self.assertIsNot(prepared_post, _FakeNitterClient.post)
        self.assertEqual(prepared_post.translated_text, "翻译错误")
        self.assertIn("中文翻译：\n翻译错误", message_text)

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
                PLUGIN_ID,
                rpc_call=rpc_call,
                paths=PluginPaths(
                    data_dir=Path(temp_dir) / "data",
                    runtime_dir=Path(temp_dir) / "runtime",
                ),
            )
            plugin._set_context(context)
            await plugin.on_load()
            self.assertTrue(
                plugin._require_subscription_store().set_media_only(
                    "10001",
                    "example",
                    True,
                )
            )

            with patch("plugins.NitterToMaiBot.plugin.NitterClient", _FakeNitterClient):
                summary = await plugin._scan_once()

            await plugin.on_unload()

        self.assertEqual(summary.forwarded_posts, 1)
        capabilities = [str(payload["capability"]) for _method, payload in calls]
        self.assertEqual(
            capabilities,
            ["chat.open_session", "send.text", "send.image", "send.custom"],
        )
        video_call = next(
            payload["args"]
            for _method, payload in calls
            if payload["capability"] == "send.custom"
        )
        self.assertEqual(video_call["custom_type"], "videourl")
        self.assertEqual(
            video_call["content"]["url"],
            "https://video.twimg.com/video.mp4",
        )
        self.assertNotIn("base64", video_call["content"])

    async def test_successful_preview_can_sync_summary_to_maisaka_context(self) -> None:
        """开启上下文同步后，只在推文发送成功后写入文本摘要。"""

        calls: List[Dict[str, Any]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            calls.append(payload)
            return {"success": True}

        post = NitterPost(
            account="OpenAI",
            post_id="5001",
            author="@OpenAI",
            text="x" * 150,
            published_at=datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc),
            url="https://nitter.net/OpenAI/status/5001",
        )

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.6.0"},
                    "context_sync": {
                        "enabled": True,
                        "detail_level": "summary",
                        "summary_text_limit": 100,
                    },
                }
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    rpc_call=rpc_call,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()
            await plugin._send_post_preview(object(), post, "qq-group-stream")  # type: ignore[arg-type]
            await plugin.on_unload()

        self.assertEqual(
            [payload["capability"] for payload in calls],
            ["send.text", "maisaka.context.append"],
        )
        context_args = calls[1]["args"]
        self.assertEqual(context_args["source_kind"], f"plugin:{PLUGIN_ID}:tweet_forward")
        self.assertIn("NitterToMaiBot 已成功转发 1 条推文", context_args["visible_text"])
        self.assertIn("x" * 100 + "…", context_args["visible_text"])
        self.assertNotIn("x" * 101, context_args["visible_text"])
        self.assertNotIn("https://x.com/", context_args["visible_text"])

    def test_full_context_contains_translation_and_only_media_counts(self) -> None:
        """完整上下文保留文本和翻译，但不写入媒体地址或二进制。"""

        plugin = create_plugin()
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": False, "config_version": "1.6.1"},
                "context_sync": {"enabled": True, "detail_level": "full"},
            }
        )
        post = NitterPost(
            account="OpenAI",
            post_id="7001",
            author="@OpenAI",
            text="original text",
            translated_text="中文翻译内容",
            published_at=datetime(2026, 8, 26, 2, 0, tzinfo=timezone.utc),
            url="https://nitter.net/OpenAI/status/7001",
            media=[
                MediaAttachment("https://example.com/a.jpg", "image", "image/jpeg"),
                MediaAttachment("https://example.com/b.mp4", "video", "video/mp4"),
            ],
        )

        context_text = plugin._format_context_post(post, 1)

        self.assertIn("original text", context_text)
        self.assertIn("中文翻译内容", context_text)
        self.assertIn("媒体：图片 1 个、视频 1 个", context_text)
        self.assertNotIn("https://x.com/", context_text)
        self.assertNotIn("https://example.com/a.jpg", context_text)
        self.assertNotIn("https://example.com/b.mp4", context_text)

    async def test_webui_subscription_mode_applies_current_revision_and_rejects_stale_save(
        self,
    ) -> None:
        """后台管理应写回真实存储，并阻止旧页面覆盖较新的订阅。"""

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {"plugin": {"enabled": False, "config_version": "1.6.0"}}
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()

            empty_revision = subscription_revision(plugin._require_subscription_store().snapshot())
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.6.0"},
                    "interaction": {"subscription_management_mode": "webui"},
                    "subscriptions": {
                        "revision": empty_revision,
                        "groups": [{"group_id": "10001", "enabled": True}],
                        "accounts": [
                            {
                                "account": "@OpenAI",
                                "qq_groups": ["10001"],
                                "media_only_qq_groups": ["10001"],
                            }
                        ],
                    },
                }
            )
            await plugin.on_config_update("self", {}, "test-current")
            store = plugin._require_subscription_store()
            self.assertEqual(store.subscriptions_for_group("10001"), [("OpenAI", True)])

            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.6.0"},
                    "interaction": {"subscription_management_mode": "webui"},
                    "subscriptions": {
                        "revision": empty_revision,
                        "groups": [{"group_id": "20002", "enabled": True}],
                        "accounts": [
                            {
                                "account": "Other",
                                "qq_groups": ["20002"],
                                "media_only_qq_groups": [],
                            }
                        ],
                    },
                }
            )
            await plugin.on_config_update("self", {}, "test-stale")
            self.assertEqual(store.subscriptions_for_group("10001"), [("OpenAI", True)])
            self.assertEqual(store.subscriptions_for_group("20002"), [])
            await plugin.on_unload()

    async def test_webui_subscription_save_failure_restores_persisted_snapshot(self) -> None:
        """后台订阅落盘失败时，内存和后台镜像都应恢复为原快照。"""

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {"plugin": {"enabled": False, "config_version": "1.6.1"}}
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()
            store = plugin._require_subscription_store()
            store.subscribe("10001", "OpenAI")
            await asyncio.to_thread(store.save)
            await plugin._sync_subscription_mirror()
            original_snapshot = store.snapshot()
            original_revision = subscription_revision(original_snapshot)

            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.6.1"},
                    "interaction": {"subscription_management_mode": "webui"},
                    "subscriptions": {
                        "revision": original_revision,
                        "groups": [{"group_id": "20002", "enabled": True}],
                        "accounts": [
                            {"account": "Other", "qq_groups": ["20002"]}
                        ],
                    },
                }
            )
            with patch.object(store, "save", side_effect=OSError("测试写入失败")):
                with self.assertRaises(OSError):
                    await plugin._apply_subscription_config_update()

            self.assertEqual(store.snapshot(), original_snapshot)
            await plugin.on_unload()

    async def test_media_only_subscription_excludes_plain_text_for_its_group(self) -> None:
        """纯文本推文只投递到全部推文群，不进入仅媒体群。"""

        calls: List[Tuple[str, Dict[str, Any]]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            calls.append((str(payload["capability"]), payload))
            capability = str(payload["capability"])
            if capability == "chat.open_session":
                return {"success": True, "stream_id": "qq-group-stream"}
            if capability == "send.text":
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
                        "send_existing_on_first_run": True,
                    },
                }
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    rpc_call=rpc_call,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()
            subscription_store = plugin._require_subscription_store()
            subscription_store.subscribe("10001", "first")
            subscription_store.subscribe("10002", "first", media_only=True)

            with patch("plugins.NitterToMaiBot.plugin.NitterClient", _MultiAccountNitterClient):
                summary = await plugin._scan_once()

            state_store = plugin._require_state_store()
            await plugin.on_unload()

        self.assertEqual(summary.forwarded_posts, 1)
        self.assertTrue(state_store.is_seen("first", "1001"))
        self.assertEqual(
            [capability for capability, _payload in calls],
            ["chat.open_session", "send.text"],
        )
        self.assertEqual(calls[0][1]["args"]["group_id"], "10001")

    async def test_media_only_subscription_marks_plain_text_seen_when_no_group_accepts_it(self) -> None:
        """只有仅媒体目标时，纯文本推文直接记为已处理且不发送。"""

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.5.3"},
                    "nitter": {
                        "base_url": "http://127.0.0.1:8080",
                        "send_existing_on_first_run": True,
                    },
                }
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()
            plugin._require_subscription_store().subscribe(
                "10001",
                "first",
                media_only=True,
            )

            with patch("plugins.NitterToMaiBot.plugin.NitterClient", _MultiAccountNitterClient):
                summary = await plugin._scan_once()

            state_store = plugin._require_state_store()
            await plugin.on_unload()

        self.assertEqual(summary.forwarded_posts, 0)
        self.assertTrue(state_store.is_seen("first", "1001"))

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
                PLUGIN_ID,
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
                PLUGIN_ID,
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

    async def test_forward_batches_bisect_failure_and_drop_only_failed_leaf(self) -> None:
        """失败分包应最多二分三层，并只放弃最终仍失败的叶子。"""

        forward_calls: List[Dict[str, Any]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            self.assertEqual(payload["capability"], "send.forward")
            forward_calls.append(payload)
            post_ids = {
                str(message["message_id"])
                for message in payload["args"]["messages"]
            }
            return {"success": "3007" not in post_ids}

        posts = [
            NitterPost(
                account=f"account{index:02d}",
                post_id=str(3000 + index),
                author=f"@account{index:02d}",
                text=f"第 {index} 条测试推文",
                published_at=datetime(2026, 8, 11, 0, index, tzinfo=timezone.utc),
                url=f"http://127.0.0.1:8080/account{index:02d}/status/{3000 + index}",
            )
            for index in range(8)
        ]

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {"plugin": {"enabled": False, "config_version": "1.5.3"}}
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    rpc_call=rpc_call,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()

            await plugin._send_posts_to_group_in_batches(
                _MultiAccountNitterClient("", 0, 0),
                posts,
                "10001",
                "qq-group-stream",
            )

            state_store = plugin._require_state_store()
            completed_tokens = [
                state_store.completed_tokens(post.account, post.post_id, "10001")
                for post in posts
            ]
            await plugin.on_unload()

        self.assertEqual(
            [len(call["args"]["messages"]) for call in forward_calls],
            [8, 4, 4, 2, 2, 1, 1],
        )
        self.assertTrue(all(FORWARD_TOKEN in tokens for tokens in completed_tokens[:7]))
        self.assertNotIn(FORWARD_TOKEN, completed_tokens[7])
        self.assertIn(DROPPED_FORWARD_TOKEN, completed_tokens[7])

    async def test_forward_batch_failure_drops_whole_batch_when_split_attempts_is_zero(
        self,
    ) -> None:
        """二分次数为零时，整包首次失败后应直接全部放弃。"""

        forward_calls: List[Dict[str, Any]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            forward_calls.append(payload)
            return {"success": False}

        posts = [
            NitterPost(
                account=f"account{index}",
                post_id=str(5000 + index),
                author=f"@account{index}",
                text=f"第 {index} 条失败测试推文",
                published_at=datetime(2026, 8, 26, 0, index, tzinfo=timezone.utc),
                url=f"https://nitter.net/account{index}/status/{5000 + index}",
            )
            for index in range(4)
        ]

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.6.1"},
                    "delivery": {"forward_split_attempts": 0},
                    "context_sync": {"enabled": True},
                }
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    rpc_call=rpc_call,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()
            await plugin._send_posts_to_group_in_batches(
                _MultiAccountNitterClient("", 0, 0),
                posts,
                "10001",
                "qq-group-stream",
            )
            state_store = plugin._require_state_store()
            completed_tokens = [
                state_store.completed_tokens(post.account, post.post_id, "10001")
                for post in posts
            ]
            await plugin.on_unload()

        self.assertEqual(len(forward_calls), 1)
        self.assertEqual(len(forward_calls[0]["args"]["messages"]), 4)
        self.assertEqual(forward_calls[0]["capability"], "send.forward")
        self.assertTrue(
            all(DROPPED_FORWARD_TOKEN in tokens for tokens in completed_tokens)
        )
        self.assertTrue(all(FORWARD_TOKEN not in tokens for tokens in completed_tokens))

    async def test_forward_batch_failure_splits_only_one_level_when_configured_once(
        self,
    ) -> None:
        """二分次数为一时，只重试两个一级子包，不继续拆成单条。"""

        forward_sizes: List[int] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            self.assertEqual(payload["capability"], "send.forward")
            forward_sizes.append(len(payload["args"]["messages"]))
            return {"success": False}

        posts = [
            NitterPost(
                account=f"single{index}",
                post_id=str(6000 + index),
                author=f"@single{index}",
                text=f"第 {index} 条一次二分测试推文",
                published_at=datetime(2026, 8, 26, 1, index, tzinfo=timezone.utc),
                url=f"https://nitter.net/single{index}/status/{6000 + index}",
            )
            for index in range(4)
        ]

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.6.1"},
                    "delivery": {"forward_split_attempts": 1},
                }
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    rpc_call=rpc_call,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()
            await plugin._send_posts_to_group_in_batches(
                _MultiAccountNitterClient("", 0, 0),
                posts,
                "10001",
                "qq-group-stream",
            )
            state_store = plugin._require_state_store()
            completed_tokens = [
                state_store.completed_tokens(post.account, post.post_id, "10001")
                for post in posts
            ]
            await plugin.on_unload()

        self.assertEqual(forward_sizes, [4, 2, 2])
        self.assertTrue(
            all(DROPPED_FORWARD_TOKEN in tokens for tokens in completed_tokens)
        )

    async def test_bisected_forward_syncs_context_only_for_successful_child(self) -> None:
        """二分后只有成功并保存进度的子包可以进入聊天上下文。"""

        calls: List[Dict[str, Any]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            calls.append(payload)
            if payload["capability"] == "maisaka.context.append":
                return {"success": True}
            message_ids = [
                message["message_id"] for message in payload["args"]["messages"]
            ]
            return {"success": message_ids == ["8001"]}

        posts = [
            NitterPost(
                account=account,
                post_id=post_id,
                author=f"@{account}",
                text=f"{account} context test",
                published_at=datetime(2026, 8, 26, 3, index, tzinfo=timezone.utc),
                url=f"https://nitter.net/{account}/status/{post_id}",
            )
            for index, (account, post_id) in enumerate(
                (("success_account", "8001"), ("failed_account", "8002"))
            )
        ]

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.6.2"},
                    "delivery": {"forward_split_attempts": 1},
                    "context_sync": {"enabled": True},
                }
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    rpc_call=rpc_call,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()
            await plugin._send_posts_to_group_in_batches(
                _MultiAccountNitterClient("", 0, 0),
                posts,
                "10001",
                "qq-group-stream",
            )
            state_store = plugin._require_state_store()
            success_tokens = state_store.completed_tokens(
                "success_account", "8001", "10001"
            )
            failed_tokens = state_store.completed_tokens(
                "failed_account", "8002", "10001"
            )
            await plugin.on_unload()

        self.assertEqual(
            [payload["capability"] for payload in calls],
            [
                "send.forward",
                "send.forward",
                "maisaka.context.append",
                "send.forward",
            ],
        )
        context_text = calls[2]["args"]["visible_text"]
        self.assertIn("success_account", context_text)
        self.assertNotIn("failed_account", context_text)
        self.assertIn(FORWARD_TOKEN, success_tokens)
        self.assertIn(DROPPED_FORWARD_TOKEN, failed_tokens)

    async def test_forward_batches_split_on_inline_image_budget_without_file_nodes(self) -> None:
        """累计图片超过单包上限时拆包，且只构造内嵌图片节点。"""

        forward_calls: List[Dict[str, Any]] = []

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            self.assertEqual(payload["capability"], "send.forward")
            forward_calls.append(payload)
            return {"success": True}

        test_case = self

        class SizedImageClient:
            async def download_media(self, media_url: str, max_bytes: int) -> Tuple[bytes, str]:
                del media_url
                test_case.assertEqual(max_bytes, 1024 * 1024)
                return b"x" * 700_000, "image/jpeg"

        sized_client = SizedImageClient()
        posts = [
            NitterPost(
                account=f"image{index}",
                post_id=str(4000 + index),
                author=f"@image{index}",
                text=f"图片推文 {index}",
                published_at=datetime(2026, 8, 11, 1, index, tzinfo=timezone.utc),
                url=f"http://127.0.0.1:8080/image{index}/status/{4000 + index}",
                media=[
                    MediaAttachment(
                        f"http://127.0.0.1:8080/pic/image{index}.jpg",
                        "image",
                        "image/jpeg",
                    )
                ],
            )
            for index in range(3)
        ]

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.5.3"},
                    "delivery": {"max_media_size_mb": 1},
                }
            )
            plugin._set_context(
                PluginContext(
                    PLUGIN_ID,
                    rpc_call=rpc_call,
                    paths=PluginPaths(
                        data_dir=Path(temp_dir) / "data",
                        runtime_dir=Path(temp_dir) / "runtime",
                    ),
                )
            )
            await plugin.on_load()
            await plugin._send_posts_to_group_in_batches(
                sized_client,
                posts,
                "10001",
                "qq-group-stream",
            )
            await plugin.on_unload()

        self.assertEqual([len(call["args"]["messages"]) for call in forward_calls], [1, 1, 1])
        segment_types = [
            segment["type"]
            for call in forward_calls
            for node in call["args"]["messages"]
            for segment in node["segments"]
        ]
        self.assertIn("image", segment_types)
        self.assertNotIn("file", segment_types)
