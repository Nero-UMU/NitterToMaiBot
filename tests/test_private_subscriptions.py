"""订阅私聊按来源群发送，并遵守好友关系、群发送回执和启用时间。"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import tomllib

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF

from plugins.NitterToMaiBot.models import MediaAttachment
from plugins.NitterToMaiBot.private_subscriptions import PrivateSubscriptions
from plugins.NitterToMaiBot.tests.test_subscription_forwarding import FakeQQ, TimelineClient, post, running_plugin


class PrivateQQ(FakeQQ):
    def __init__(self) -> None:
        super().__init__()
        self.friend_ids = ["90001"]
        self.friend_error = False
        self.private_error = False
        self.deliveries = []
        self.private_notices = []

    async def rpc_call(
        self, method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None,
    ) -> Any:
        capability, args = payload["capability"], payload["args"]
        if capability == "api.call":
            action = args["args"]["action_name"]
            params = args["args"]["params"]
            if action == "get_friend_list":
                assert params == {"no_cache": True}
                self.events.append(action)
                if self.friend_error:
                    return {"success": False, "error": "好友列表暂时不可用"}
                data = [{"user_id": int(user_id)} for user_id in self.friend_ids]
                return {"success": True, "result": {"status": "ok", "retcode": 0, "data": data}}
            if action == "get_group_info":
                data = {"group_name": "测试" + params["group_id"]}
                return {"success": True, "result": {"status": "ok", "retcode": 0, "data": data}}
        if capability == "chat.open_session" and args["chat_type"] == "private":
            assert args["account_id"] == "123456"
            return {"success": True, "stream_id": "private-" + args["user_id"]}
        if capability == "send.text" and args["stream_id"].startswith("private-"):
            self.events.append("private_header")
            self.deliveries.append((args["stream_id"], args["text"]))
            return {"success": True}
        if capability == "send.forward" and args["stream_id"].startswith("private-"):
            if args["messages"][0]["message_id"] == "failed-links-notice":
                self.events.append("private_failed_links")
                self.private_notices.append((
                    args["stream_id"], [n["segments"][0]["content"] for n in args["messages"][1:]],
                ))
                return {"success": not self.fail_notice}
            self.events.append("private_forward")
            self.deliveries.append((args["stream_id"], [node["message_id"] for node in args["messages"]]))
            if self.private_error:
                raise TimeoutError("等待私聊回执超时")
            return {"success": True}
        return await super().rpc_call(method, plugin_id, payload, timeout_ms)


class PrivateSubscriptionTests(IsolatedAsyncioTestCase):
    async def test_admin_and_command_modes_share_persisted_records_with_stale_edit_protection(self) -> None:
        qq = PrivateQQ()
        qq.friend_ids.append("90002")
        async with running_plugin(qq, TimelineClient([]), private_subscriptions={"enabled": True}) as p:
            store = p._require_private_subscriptions()
            path = p._config_mirror.config_path
            args = {"group_id": "10001", "platform": "qq", "matched_groups": {"action": "订阅"}}
            first = {"group_id": "10001", "user_id": "90001"}
            second = {"group_id": "10001", "user_id": "90002"}

            async def save(mode, records, revision):
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                data["plugin"] = {"enabled": False, "config_version": "1.9.1"}
                data["private_subscriptions"].update(
                    enabled=True, management_mode=mode, records=records, revision=revision,
                )
                p.set_plugin_config(data)
                await p.on_config_update(CONFIG_RELOAD_SCOPE_SELF, {}, "test-admin-save")

            await save("仅群命令", [first], store.revision())
            self.assertFalse(store.memberships())
            self.assertTrue((await p.handle_private_subscription(user_id="90001", **args))[0])
            displayed = tomllib.loads(path.read_text(encoding="utf-8"))["private_subscriptions"]
            self.assertEqual(displayed["records"], [first])
            started = store.users("10001")["90001"]

            await save("仅后台", [first, second], store.revision())
            self.assertEqual(store.users("10001")["90001"], started)
            self.assertEqual(set(store.users("10001")), {"90001", "90002"})
            self.assertFalse((await p.handle_private_subscription(
                user_id="90002", **{**args, "matched_groups": {"action": "退订"}},
            ))[0])
            self.assertTrue((await p.handle_private_subscription(
                user_id="90002", **{**args, "matched_groups": {"action": "查询"}},
            ))[0])

            revision = store.revision()
            await save("群命令和后台", [second], revision)
            self.assertEqual(set(store.users("10001")), {"90002"})
            self.assertTrue((await p.handle_private_subscription(user_id="90001", **args))[0])
            await save("群命令和后台", [], revision)
            self.assertEqual(set(store.users("10001")), {"90001", "90002"})
            self.assertEqual(
                tomllib.loads(path.read_text(encoding="utf-8"))["private_subscriptions"]["records"], [first, second],
            )
            with self.assertRaisesRegex(ValueError, "QQ 好友"):
                await save("群命令和后台", [first, second, {"group_id": "10001", "user_id": "90003"}], store.revision())
            self.assertEqual(store.memberships(), {("10001", "90001"), ("10001", "90002")})

    async def test_friend_only_commands_persist_and_default_is_off(self) -> None:
        qq = PrivateQQ()
        async with running_plugin(qq, TimelineClient([post(1)])) as plugin:
            store = plugin._require_subscription_store()
            store.subscribe("10001", "example")
            args = {"group_id": "10001", "platform": "qq", "matched_groups": {"action": "订阅"}}
            self.assertFalse((await plugin.handle_private_subscription(user_id="90001", **args))[0])
            self.assertNotIn("get_friend_list", qq.events)

        async with running_plugin(qq, TimelineClient([post(1)]), private_subscriptions={"enabled": True}) as plugin:
            plugin._require_subscription_store().subscribe("10001", "example")
            self.assertFalse((await plugin.handle_private_subscription(user_id="90002", **args))[0])
            self.assertTrue((await plugin.handle_private_subscription(user_id="90001", **args))[0])
            store = PrivateSubscriptions(Path(plugin.ctx.paths.data_dir) / "private_subscriptions.json")
            store.load()
            self.assertEqual(set(store.users("10001")), {"90001"})
            args["matched_groups"] = {"action": "退订"}
            self.assertTrue((await plugin.handle_private_subscription(user_id="90001", **args))[0])
            store.load()
            self.assertEqual(store.users("10001"), {})

    async def test_private_follows_only_acknowledged_group_packages(self) -> None:
        qq = PrivateQQ()
        qq.fail_group = lambda _group, nodes: "/status/2" in qq.text(nodes)
        async with running_plugin(qq, TimelineClient([post(1), post(2)]), private_subscriptions={"enabled": True}) as p:
            p._require_private_subscriptions().set_subscribed("10001", "90001", True)
            p._require_private_subscriptions().set_subscribed("10001", "90002", True)
            await p._scan_once()
        self.assertEqual(qq.deliveries, [
            ("private-90001", "来自测试10001群的推特订阅推送"), ("private-90001", ["1"]),
        ])
        self.assertLess(qq.events.index("send_group_forward_msg"), qq.events.index("private_header"))
        self.assertEqual(qq.events.count("get_friend_list"), 1)
        self.assertEqual(qq.private_notices, [("private-90001", ["https://x.com/example/status/2"])])
        self.assertEqual(len(qq.notices), 1)

    async def test_failed_link_switch_applies_to_group_and_private_with_media_failure(self) -> None:
        for group_push, enabled in ((True, False), (False, True), (False, False)):
            with self.subTest(group_push=group_push, failed_link_switch=enabled):
                qq = PrivateQQ()
                client = TimelineClient([post(1, [MediaAttachment("https://media/bad.jpg", "image")])])
                async with running_plugin(
                    qq, client,
                    private_subscriptions={"enabled": True},
                    nitter={"send_failed_links_forward": enabled},
                ) as p:
                    p._require_subscription_store().set_push_enabled("10001", group_push)
                    p._require_private_subscriptions().set_subscribed("10001", "90001", True)
                    await p._scan_once()
                self.assertEqual(qq.events.count("private_forward"), 1)
                self.assertEqual(qq.private_notices, (
                    [("private-90001", ["https://x.com/example/status/1"])] if enabled else []
                ))
                self.assertFalse(qq.notices)

    async def test_only_private_failed_source_still_sends_link_and_source_header(self) -> None:
        qq = PrivateQQ()
        client = TimelineClient([replace(post(1), has_video=True)])
        client.fail_enrich = True
        async with running_plugin(qq, client, private_subscriptions={"enabled": True}) as p:
            p._require_subscription_store().set_push_enabled("10001", False)
            p._require_private_subscriptions().set_subscribed("10001", "90001", True)
            await p._scan_once()
        self.assertEqual(qq.deliveries, [("private-90001", "来自测试10001群的推特订阅推送")])
        self.assertEqual(qq.private_notices, [("private-90001", ["https://x.com/example/status/1"])])

    async def test_closed_group_uses_direct_private_delivery_without_cross_group_deduplication(self) -> None:
        qq = PrivateQQ()
        client = TimelineClient([post(1), post(2)])
        async with running_plugin(qq, client, private_subscriptions={"enabled": True}) as p:
            groups = p._require_subscription_store()
            groups.subscribe("10002", "example")
            for group in ("10001", "10002"):
                groups.set_push_enabled(group, False)
                p._require_private_subscriptions().set_subscribed(group, "90001", True)
            with patch("plugins.NitterToMaiBot.plugin.MAX_SUBSCRIPTION_FORWARD_NODES", 1):
                await p._scan_once()
            self.assertEqual(p._require_state_store().pending_post_count(), 0)
        self.assertEqual(qq.forward_attempts, [])
        self.assertEqual(qq.events.count("get_friend_list"), 1)
        self.assertEqual(qq.deliveries, [
            ("private-90001", "来自测试10001群的推特订阅推送"), ("private-90001", ["1"]), ("private-90001", ["2"]),
            ("private-90001", "来自测试10002群的推特订阅推送"), ("private-90001", ["1"]), ("private-90001", ["2"]),
        ])

    async def test_friend_lookup_retry_does_not_resend_confirmed_group_package(self) -> None:
        qq = PrivateQQ()
        qq.friend_error = True
        async with running_plugin(qq, TimelineClient([post(1)]), private_subscriptions={"enabled": True}) as p:
            p._require_private_subscriptions().set_subscribed("10001", "90001", True)
            await p._scan_once()
            self.assertEqual(qq.deliveries, [])
            p._require_state_store().load()
            qq.friend_error = False
            qq.private_error = True
            qq.fail_notice = True
            await p._forward_once()
            p._require_state_store().load()
            await p._forward_once()
        self.assertEqual(len(qq.forward_attempts), 1)
        self.assertEqual(qq.events.count("private_forward"), 1)
        self.assertEqual(qq.private_notices, [("private-90001", ["https://x.com/example/status/1"])])

    async def test_activation_cutoff_marks_old_feed_items_as_processed(self) -> None:
        qq = FakeQQ()
        async with running_plugin(qq, TimelineClient([post(1), post(2), post(3)])) as p:
            state = p._require_state_store()
            state.ignore_posts_before = post(2).published_at.timestamp()
            await p._save_state()
            state.load()
            await p._scan_once()
            self.assertTrue(all(state.is_seen("example", str(i)) for i in (1, 2, 3)))
        self.assertEqual(len(qq.forward_attempts), 1)
        self.assertIn("/status/3", qq.text(qq.forward_attempts[0][1]))
        self.assertNotIn("/status/2", qq.text(qq.forward_attempts[0][1]))
