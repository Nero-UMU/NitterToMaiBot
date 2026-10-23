"""SDK 公开 API 直接组装群聊天记录，不调用私聊发送。"""

from typing import Any, Dict, List
from unittest import IsolatedAsyncioTestCase

from maibot_sdk.context import PluginContext

from plugins.NitterToMaiBot.qq_forwarder import QQForwarder


API_NAME = "adapter.napcat.action.call"


class QQForwarderTests(IsolatedAsyncioTestCase):
    async def test_forward_sends_content_nodes_without_private_send_or_message_id_references(self) -> None:
        actions: List[Dict[str, Any]] = []

        async def rpc_call(method, plugin_id, payload, timeout_ms=None):
            del method, plugin_id, timeout_ms
            if payload["capability"] == "api.list":
                return {
                    "success": True,
                    "apis": [{"name": API_NAME, "version": "1", "full_name": "test.adapter." + API_NAME}],
                }
            self.assertEqual(payload["capability"], "api.call")
            self.assertEqual(payload["args"]["api_name"], "test.adapter." + API_NAME)
            request = payload["args"]["args"]
            actions.append(request)
            if request["action_name"] == "get_login_info":
                data = {"user_id": 123456}
            else:
                self.assertEqual(request["action_name"], "send_group_forward_msg")
                data = {"message_id": 300}
            return {"success": True, "result": {"status": "ok", "retcode": 0, "data": data}}

        forwarder = await QQForwarder.connect(PluginContext("test.plugin", rpc_call=rpc_call), "")
        nodes = [{"type": "node", "data": {"user_id": "123456", "content": [{"type": "text", "data": {"text": "推文"}}]}}]
        await forwarder.forward_to_group("10001", nodes)
        self.assertEqual(
            actions[1]["params"],
            {
                "group_id": "10001",
                "messages": nodes,
            },
        )

    async def test_missing_message_id_is_not_a_success(self) -> None:
        async def rpc_call(method, plugin_id, payload, timeout_ms=None):
            del method, plugin_id, timeout_ms
            return {"success": True, "result": {"status": "ok", "retcode": 0, "data": {}}}

        forwarder = QQForwarder(PluginContext("test.plugin", rpc_call=rpc_call), "test.api", "123456")
        with self.assertRaisesRegex(RuntimeError, "消息 ID"):
            await forwarder.forward_to_group("10001", [{"type": "node", "data": {"content": []}}])

    async def test_account_mismatch_does_not_send(self) -> None:
        actions = []

        async def rpc_call(method, plugin_id, payload, timeout_ms=None):
            del method, plugin_id, timeout_ms
            if payload["capability"] == "api.list":
                return {"success": True, "apis": [{"name": API_NAME, "version": "1", "full_name": "test.api"}]}
            actions.append(payload["args"]["args"]["action_name"])
            return {"success": True, "result": {"status": "ok", "retcode": 0, "data": {"user_id": 123456}}}

        with self.assertRaisesRegex(RuntimeError, "机器人 QQ"):
            await QQForwarder.connect(PluginContext("test.plugin", rpc_call=rpc_call), "654321")
        self.assertEqual(actions, ["get_login_info"])

    async def test_ambiguous_adapters_require_explicit_account(self) -> None:
        async def rpc_call(method, plugin_id, payload, timeout_ms=None):
            del method, plugin_id, timeout_ms
            return {
                "success": True,
                "apis": [{"name": API_NAME, "version": "1", "full_name": name} for name in ("first.api", "second.api")],
            }

        with self.assertRaisesRegex(RuntimeError, "机器人 QQ"):
            await QQForwarder.connect(PluginContext("test.plugin", rpc_call=rpc_call), "")

    async def test_failed_onebot_response_exposes_reason(self) -> None:
        async def rpc_call(method, plugin_id, payload, timeout_ms=None):
            del method, plugin_id, payload, timeout_ms
            return {"success": True, "result": {"status": "failed", "retcode": 1200, "message": "上传超时"}}

        forwarder = QQForwarder(PluginContext("test.plugin", rpc_call=rpc_call), "test.api", "123456")
        with self.assertRaisesRegex(RuntimeError, "上传超时"):
            await forwarder.forward_to_group("10001", [{"type": "node", "data": {"content": []}}])
