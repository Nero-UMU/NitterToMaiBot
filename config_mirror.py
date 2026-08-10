"""把统一订阅数据同步到插件配置页的只读展示区。"""

from pathlib import Path
from typing import Dict, List

import os

import tomlkit


class _ConfigMirrorConflictError(RuntimeError):
    """配置在镜像写入期间被其他来源修改。"""


class SubscriptionConfigMirror:
    """只更新配置文件中的订阅镜像，并清空已经迁移的旧配置字段。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def sync(self, snapshot: Dict[str, object]) -> bool:
        """同步只读镜像；检测到并发保存时重新读取最新配置后重试。"""

        groups = self._snapshot_groups(snapshot)
        accounts = self._snapshot_accounts(snapshot)
        for attempt in range(3):
            try:
                return self._sync_once(groups, accounts)
            except _ConfigMirrorConflictError as exc:
                if attempt == 2:
                    raise RuntimeError("后台配置持续发生并发修改，订阅只读镜像同步失败") from exc
        raise AssertionError("订阅只读镜像重试流程异常")

    def _sync_once(
        self,
        groups: List[Dict[str, object]],
        accounts: List[Dict[str, object]],
    ) -> bool:
        """基于同一次配置快照完成一次同步尝试。"""

        original_content = self.config_path.read_bytes()
        document = tomlkit.parse(original_content.decode("utf-8"))

        raw_document = document.unwrap()
        raw_subscriptions = raw_document.get("subscriptions", {})
        nitter = raw_document.get("nitter", {})
        delivery = raw_document.get("delivery", {})
        if (
            isinstance(raw_subscriptions, dict)
            and raw_subscriptions.get("groups", []) == groups
            and raw_subscriptions.get("accounts", []) == accounts
            and isinstance(nitter, dict)
            and nitter.get("accounts", []) == []
            and isinstance(delivery, dict)
            and delivery.get("qq_groups", []) == []
        ):
            return False

        if "nitter" not in document or "delivery" not in document:
            raise ValueError(f"插件配置缺少 nitter 或 delivery 配置节: {self.config_path}")
        document["nitter"]["accounts"] = []
        document["delivery"]["qq_groups"] = []
        document["subscriptions"] = self._build_subscription_table(groups, accounts)
        self._atomic_write(document, original_content)
        return True

    @staticmethod
    def _snapshot_groups(snapshot: Dict[str, object]) -> List[Dict[str, object]]:
        raw_groups = snapshot.get("groups")
        if not isinstance(raw_groups, list):
            raise TypeError("订阅快照的 groups 必须是列表")
        groups: List[Dict[str, object]] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                raise TypeError("订阅快照的群记录必须是对象")
            group_id = raw_group.get("group_id")
            enabled = raw_group.get("enabled")
            if not isinstance(group_id, str) or not isinstance(enabled, bool):
                raise TypeError("订阅快照的群记录字段无效")
            groups.append({"group_id": group_id, "enabled": enabled})
        return groups

    @staticmethod
    def _snapshot_accounts(snapshot: Dict[str, object]) -> List[Dict[str, object]]:
        raw_accounts = snapshot.get("accounts")
        if not isinstance(raw_accounts, list):
            raise TypeError("订阅快照的 accounts 必须是列表")
        accounts: List[Dict[str, object]] = []
        for raw_account in raw_accounts:
            if not isinstance(raw_account, dict):
                raise TypeError("订阅快照的账号记录必须是对象")
            account = raw_account.get("account")
            display_name = raw_account.get("display_name", "")
            qq_groups = raw_account.get("qq_groups")
            media_only_qq_groups = raw_account.get("media_only_qq_groups")
            if (
                not isinstance(account, str)
                or not isinstance(display_name, str)
                or not isinstance(qq_groups, list)
                or not isinstance(media_only_qq_groups, list)
            ):
                raise TypeError("订阅快照的账号记录字段无效")
            if not all(isinstance(group_id, str) for group_id in qq_groups):
                raise TypeError("订阅快照的账号群列表必须只包含字符串")
            if not all(
                isinstance(group_id, str) and group_id in qq_groups
                for group_id in media_only_qq_groups
            ):
                raise TypeError("订阅快照的仅媒体群必须属于账号订阅群")
            account_snapshot: Dict[str, object] = {
                "account": account,
                "qq_groups": list(qq_groups),
                "media_only_qq_groups": list(media_only_qq_groups),
            }
            if display_name:
                account_snapshot["display_name"] = display_name
            accounts.append(account_snapshot)
        return accounts

    @staticmethod
    def _build_subscription_table(
        groups: List[Dict[str, object]],
        accounts: List[Dict[str, object]],
    ) -> tomlkit.items.Table:
        subscriptions = tomlkit.table()
        group_tables = tomlkit.aot()
        for group in groups:
            group_table = tomlkit.table()
            group_table.add("group_id", group["group_id"])
            group_table.add("enabled", group["enabled"])
            group_tables.append(group_table)
        subscriptions.add("groups", group_tables)

        account_tables = tomlkit.aot()
        for account in accounts:
            account_table = tomlkit.table()
            account_table.add("account", account["account"])
            if "display_name" in account:
                account_table.add("display_name", account["display_name"])
            account_table.add("qq_groups", account["qq_groups"])
            account_table.add("media_only_qq_groups", account["media_only_qq_groups"])
            account_tables.append(account_table)
        subscriptions.add("accounts", account_tables)
        return subscriptions

    def _atomic_write(self, document: tomlkit.TOMLDocument, original_content: bytes) -> None:
        """在同一目录写入临时文件后替换原配置，避免产生半截 TOML。"""

        file_mode = self.config_path.stat().st_mode
        temp_path = self.config_path.with_suffix(f"{self.config_path.suffix}.subscription.tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as config_file:
                tomlkit.dump(document, config_file)
            os.chmod(temp_path, file_mode)
            if self.config_path.read_bytes() != original_content:
                raise _ConfigMirrorConflictError("插件配置已被其他来源修改")
            os.replace(temp_path, self.config_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
