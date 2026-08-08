"""群内订阅命令测试。"""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from maibot_sdk.context import PluginContext, PluginPaths

from plugins.NitterToMaiBot.media_cache import CachedMedia
from plugins.NitterToMaiBot.models import MediaAttachment, NitterPost
from plugins.NitterToMaiBot.plugin import create_plugin
from plugins.NitterToMaiBot.tests.helpers import use_temporary_config_mirror


class _CommandNitterClient:
    """为关注命令返回固定时间线。"""

    def __init__(self, base_url: str, timeout_seconds: int, request_attempts: int) -> None:
        del base_url
        del request_attempts
        del timeout_seconds

    async def fetch_timeline(self, account: str) -> List[NitterPost]:
        return [
            NitterPost(
                account=account,
                post_id="123456",
                author=f"@{account}",
                text="测试推文",
                published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
                url=f"http://127.0.0.1:8080/{account}/status/123456",
            )
        ]

    def profile_name(self, account: str) -> str:
        return f"{account} 显示名"


class PluginCommandTests(IsolatedAsyncioTestCase):
    """验证关注命令会写入当前群的独立订阅。"""

    async def test_auto_parse_supports_private_qq_stream(self) -> None:
        plugin = create_plugin()
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": True, "config_version": "1.4.1"},
                "nitter": {"base_url": "http://127.0.0.1:8080"},
            }
        )
        parse_status = AsyncMock()

        with patch.object(plugin, "_parse_and_send_status", parse_status):
            hook_result = await plugin.handle_auto_parse_status(
                {
                    "platform": "qq",
                    "processed_plain_text": "https://x.com/OpenAI/status/123456?s=20",
                    "session_id": "qq-private-stream",
                    "message_info": {
                        "user_info": {"user_id": "123456"},
                        "group_info": None,
                    },
                }
            )
            continue_result = await plugin.handle_auto_parse_status(
                {
                    "platform": "qq",
                    "processed_plain_text": "普通私聊消息",
                    "session_id": "qq-private-stream",
                }
            )

        parse_status.assert_awaited_once_with("OpenAI", "123456", "qq-private-stream")
        self.assertEqual(hook_result, {"action": "abort"})
        self.assertEqual(continue_result, {"action": "continue"})

    async def test_large_image_uses_url_file_instead_of_inline_rpc(self) -> None:
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
            return {"success": True}

        plugin = create_plugin()
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": False, "config_version": "1.4.1"},
                "nitter": {"base_url": "http://127.0.0.1:8080"},
                "delivery": {"max_media_size_mb": 10},
            }
        )
        plugin._set_context(PluginContext("third-party.nitter-to-maibot", rpc_call=rpc_call))
        post = NitterPost(
            account="OpenAI",
            post_id="123456",
            author="@OpenAI",
            text="测试推文",
            published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
            url="http://127.0.0.1:8080/OpenAI/status/123456",
        )
        media = MediaAttachment("https://example.com/large.jpg", "image", "image/jpeg")

        with TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "large.jpg"
            media_path.write_bytes(b"1234")
            cached_media = CachedMedia(
                path=media_path,
                public_url="http://127.0.0.1:18080/nitter-media/token/large.jpg",
                content_type="image/jpeg",
                size=4,
            )
            with (
                patch("plugins.NitterToMaiBot.plugin.MAX_INLINE_MEDIA_BYTES", 3),
                patch.object(plugin, "_cache_media", AsyncMock(return_value=cached_media)),
            ):
                sent = await plugin._send_media_attachment(
                    _CommandNitterClient("", 1, 1),
                    post,
                    media,
                    1,
                    "qq-private-stream",
                    {},
                )

        self.assertTrue(sent)
        self.assertEqual([capability for capability, _payload in calls], ["send.custom"])
        self.assertEqual(
            calls[0][1]["args"]["content"]["url"],
            "http://127.0.0.1:18080/nitter-media/token/large.jpg",
        )

    async def test_follow_command_persists_group_subscription(self) -> None:
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
            if payload["capability"] == "send.text":
                return {"success": True}
            raise AssertionError(f"收到未预期的能力调用: {payload['capability']}")

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.1.0"},
                    "nitter": {"base_url": "http://127.0.0.1:8080"},
                    "delivery": {"qq_groups": []},
                    "interaction": {"allow_group_commands": True},
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

            with patch("plugins.NitterToMaiBot.plugin.NitterClient", _CommandNitterClient):
                success, _response, intercept = await plugin.handle_follow(
                    stream_id="qq-group-stream",
                    group_id="10001",
                    platform="qq",
                    matched_groups={"accounts": "@OpenAI"},
                )

            targets = plugin._build_scan_targets()
            await plugin.on_unload()

        self.assertTrue(success)
        self.assertEqual(intercept, 2)
        self.assertEqual(targets, {"OpenAI": ["10001"]})
        self.assertEqual([payload["capability"] for _method, payload in calls], ["send.text"])

    async def test_list_follows_uses_forward_above_twenty_accounts(self) -> None:
        """超过 20 个订阅时按每节点 20 个发送带显示名的合并转发。"""

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
            if payload["capability"] in {"send.text", "send.forward"}:
                return {"success": True}
            raise AssertionError(f"收到未预期的能力调用: {payload['capability']}")

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.4.4"},
                    "nitter": {"base_url": "http://127.0.0.1:8080"},
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
            subscription_store = plugin._require_subscription_store()
            for index in range(1, 21):
                account = f"user{index:02d}"
                subscription_store.subscribe("10001", account)
                subscription_store.set_display_name(account, f"推特名 {index}")
            await plugin.handle_list_follows(
                stream_id="qq-group-stream",
                group_id="10001",
                platform="qq",
            )
            subscription_store.subscribe("10001", "user21")
            subscription_store.set_display_name("user21", "推特名 21")
            await plugin.handle_list_follows(
                stream_id="qq-group-stream",
                group_id="10001",
                platform="qq",
            )
            await plugin.on_unload()

        self.assertEqual(
            [payload["capability"] for _method, payload in calls],
            ["send.text", "send.forward"],
        )
        self.assertTrue(
            calls[0][1]["args"]["text"].startswith(
                "当前群推送：开启\n订阅账号（20 个）：\n1.@user01 推特名 1"
            )
        )
        nodes = calls[1][1]["args"]["messages"]
        self.assertEqual(len(nodes), 2)
        first_content = nodes[0]["segments"][0]["content"]
        second_content = nodes[1]["segments"][0]["content"]
        self.assertTrue(first_content.startswith("当前群推送：开启\n订阅账号（21 个）：\n1.@user01 推特名 1"))
        self.assertIn("20.@user20 推特名 20", first_content)
        self.assertEqual(second_content, "21.@user21 推特名 21")

    async def test_posts_command_uses_default_and_requested_count(self) -> None:
        """推文命令默认发送一条，并支持显式指定发送数量。"""

        async def rpc_call(
            method: str,
            plugin_id: str,
            payload: Dict[str, Any],
            timeout_ms: int | None = None,
        ) -> Dict[str, Any]:
            del method
            del plugin_id
            del timeout_ms
            raise AssertionError(f"收到未预期的能力调用: {payload['capability']}")

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.1.0"},
                    "nitter": {
                        "base_url": "http://127.0.0.1:8080",
                        "max_posts_per_scan": 10,
                    },
                    "delivery": {"qq_groups": []},
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

            timeline = [
                NitterPost(
                    account="OpenAI",
                    post_id=str(post_id),
                    author="@OpenAI",
                    text=f"测试推文 {post_id}",
                    published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
                    url=f"http://127.0.0.1:8080/OpenAI/status/{post_id}",
                )
                for post_id in (3, 2, 1)
            ]
            client = _CommandNitterClient("", 1, 1)
            client.fetch_timeline = AsyncMock(return_value=timeline)
            send_previews = AsyncMock()

            with (
                patch.object(plugin, "_create_client", return_value=client),
                patch.object(plugin, "_send_posts_preview", send_previews),
            ):
                default_result = await plugin.handle_posts(
                    stream_id="qq-group-stream",
                    matched_groups={"account": "@OpenAI", "count": ""},
                )
                default_post_ids = [post.post_id for post in send_previews.await_args.args[1]]
                send_previews.reset_mock()
                requested_result = await plugin.handle_posts(
                    stream_id="qq-group-stream",
                    matched_groups={"account": "OpenAI", "count": "2"},
                )
                requested_post_ids = [post.post_id for post in send_previews.await_args.args[1]]

            media_post = NitterPost(
                account="OpenAI",
                post_id="4",
                author="@OpenAI",
                text="附件容错测试",
                published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
                url="http://127.0.0.1:8080/OpenAI/status/4",
                media=[
                    MediaAttachment("https://example.com/failed.jpg", "image", "image/jpeg"),
                    MediaAttachment("https://example.com/success.jpg", "image", "image/jpeg"),
                ],
            )
            send_media = AsyncMock(side_effect=[False, True])
            with (
                patch.object(plugin, "_send_post_message", AsyncMock(return_value=True)),
                patch.object(plugin, "_send_media_attachment", send_media),
            ):
                await plugin._send_post_preview(client, media_post, "qq-group-stream")

            await plugin.on_unload()

        self.assertEqual(default_result, (True, "已发送 @OpenAI 的最新 1 条推文。", 2))
        self.assertEqual(default_post_ids, ["3"])
        self.assertEqual(requested_result, (True, "已发送 @OpenAI 的最新 2 条推文。", 2))
        self.assertEqual(requested_post_ids, ["3", "2"])
        self.assertEqual(send_media.await_count, 2)

    async def test_posts_preview_batches_only_above_configured_threshold(self) -> None:
        """自定义阈值为 2：两条逐条发送，三条使用一次合并转发。"""

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
            if payload["capability"] in {"send.text", "send.forward"}:
                return {"success": True}
            raise AssertionError(f"收到未预期的能力调用: {payload['capability']}")

        with TemporaryDirectory() as temp_dir:
            plugin = create_plugin()
            use_temporary_config_mirror(plugin, temp_dir)
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": False, "config_version": "1.2.0"},
                    "nitter": {"base_url": "http://127.0.0.1:8080"},
                    "delivery": {"qq_groups": [], "forward_batch_threshold": 2},
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

            posts = [
                NitterPost(
                    account="OpenAI",
                    post_id=str(post_id),
                    author="@OpenAI",
                    text=f"测试推文 {post_id}",
                    published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
                    url=f"http://127.0.0.1:8080/OpenAI/status/{post_id}",
                )
                for post_id in (3, 2, 1)
            ]
            client = _CommandNitterClient("", 1, 1)
            await plugin._send_posts_preview(client, posts[:2], "qq-group-stream")
            await plugin._send_posts_preview(client, posts, "qq-group-stream")
            await plugin.on_unload()

        self.assertEqual(
            [payload["capability"] for _method, payload in calls],
            ["send.text", "send.text", "send.forward"],
        )
        forward_payload = calls[2][1]["args"]
        self.assertEqual(len(forward_payload["messages"]), 3)
        self.assertEqual(
            [node["message_id"] for node in forward_payload["messages"]],
            ["3", "2", "1"],
        )
