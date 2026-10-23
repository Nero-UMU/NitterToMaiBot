"""通过 SDK 公开 API 直接发送本地缓存组成的群合并转发，不自发消息。"""

from typing import Any, Dict, List

from maibot_sdk.context import PluginContext


ACTION_API_NAME = "adapter.napcat.action.call"


class QQForwarder:
    """绑定一个已登录的 QQ 适配器，不读取其他插件配置或直接连接协议端。"""

    def __init__(self, ctx: PluginContext, api_name: str, self_id: str) -> None:
        self.ctx = ctx
        self.api_name = api_name
        self.self_id = self_id

    @classmethod
    async def connect(cls, ctx: PluginContext, account_id: str) -> "QQForwarder":
        apis = await ctx.api.list()
        if not isinstance(apis, list):
            raise RuntimeError("无法读取 QQ 适配器的公开 API 列表")
        candidates = [entry for entry in apis if entry.get("name") == ACTION_API_NAME and entry.get("version") == "1"]
        if not candidates:
            raise RuntimeError("未找到 QQ 动作 API，请启用支持公开 OneBot API 的 NapCat/Snowluma 适配器")
        if len(candidates) > 1 and not account_id:
            raise RuntimeError("存在多个 QQ 适配器，请在转发目标中指定发送机器人 QQ 号")

        for entry in candidates:
            forwarder = cls(ctx, entry["full_name"], "")
            data = await forwarder.call_action("get_login_info", {})
            self_id = str(data.get("user_id") or "").strip()
            if not self_id.isdecimal():
                raise RuntimeError("QQ 适配器未返回有效的登录机器人 QQ 号")
            if account_id and self_id != account_id:
                continue
            forwarder.self_id = self_id
            return forwarder
        raise RuntimeError("没有已登录的适配器与设置的发送机器人 QQ 号匹配")

    async def call_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        response = await self.ctx.api.call(
            self.api_name,
            version="1",
            action_name=action,
            params=params,
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"QQ 动作 {action} 未返回有效响应")
        if response.get("retcode") != 0 or response.get("status") != "ok":
            reason = response.get("message") or response.get("wording") or "未返回错误详情"
            raise RuntimeError(f"QQ 动作 {action} 失败（{response.get('retcode')}）：{reason}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"QQ 动作 {action} 未返回有效的 data")
        return data

    async def forward_to_group(self, group_id: str, nodes: List[Dict[str, Any]]) -> str:
        if not nodes:
            raise ValueError("不能转发空聊天记录")
        data = await self.call_action(
            "send_group_forward_msg",
            {
                "group_id": group_id,
                "messages": nodes,
            },
        )
        return self._require_message_id(data)

    @staticmethod
    def _require_message_id(data: Dict[str, Any]) -> str:
        raw_id = data.get("message_id")
        message_id = str(raw_id).strip() if raw_id is not None else ""
        if not message_id or message_id == "0":
            raise RuntimeError("QQ 发送响应缺少有效消息 ID，不能标记发送成功")
        return message_id
