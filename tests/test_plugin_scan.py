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
from plugins.NitterToMaiBot.plugin import PLUGIN_ID, create_plugin
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
        self.assertEqual(args["task_name"], "utils")
        self.assertEqual(args["model"], "")
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
            return {"success": True, "sent": True, "message_id": "5001"}

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
