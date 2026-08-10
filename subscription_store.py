"""QQ 群订阅关系的持久化存储。"""

from pathlib import Path
from typing import Dict, List, Tuple

import json
import os
import re


SUBSCRIPTION_VERSION = 3
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class SubscriptionStore:
    """维护 QQ 群、推送开关和推特账号之间的多对多订阅关系。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._groups: Dict[str, bool] = {}
        self._accounts: Dict[str, Dict[str, object]] = {}
        self._needs_save = False

    @property
    def needs_save(self) -> bool:
        """返回加载后是否需要按当前版本重新保存。"""

        return self._needs_save

    def load(self) -> None:
        """从磁盘读取订阅；旧版结构会在内存中迁移为当前结构。"""

        self._groups = {}
        self._accounts = {}
        self._needs_save = False
        if not self.path.exists():
            return

        with self.path.open("r", encoding="utf-8") as subscription_file:
            raw_state = json.load(subscription_file)
        if not isinstance(raw_state, dict):
            raise ValueError(f"NitterToMaiBot 订阅文件结构无效: {self.path}")

        version = raw_state.get("version")
        if version == 1:
            self._load_version_one(raw_state)
            self._needs_save = True
            return
        if version == 2:
            self._load_account_mapping(raw_state, include_media_filters=False)
            self._needs_save = True
            return
        if version != SUBSCRIPTION_VERSION:
            raise ValueError(f"不支持的 NitterToMaiBot 订阅文件版本: {self.path}")
        self._load_account_mapping(raw_state, include_media_filters=True)

    def save(self) -> None:
        """以当前版本原子保存订阅关系。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as subscription_file:
            json.dump(self.snapshot(), subscription_file, ensure_ascii=False, indent=2)
            subscription_file.write("\n")
        os.replace(temp_path, self.path)
        self._needs_save = False

    def subscribe(self, group_id: str, account: str, media_only: bool = False) -> bool:
        """为群添加账号订阅，返回是否实际新增。"""

        self._validate_group_id(group_id)
        self._validate_account(account)
        self._groups.setdefault(group_id, True)
        account_key = account.lower()
        account_entry = self._accounts.setdefault(
            account_key,
            {
                "account": account,
                "display_name": "",
                "qq_groups": [],
                "media_only_qq_groups": [],
            },
        )
        qq_groups = self._groups_from_account(account_entry)
        if group_id in qq_groups:
            return False
        qq_groups.append(group_id)
        if media_only:
            self._media_only_groups_from_account(account_entry).append(group_id)
        return True

    def set_media_only(self, group_id: str, account: str, media_only: bool) -> bool:
        """修改一条群账号订阅的仅媒体标记，返回标记是否发生变化。"""

        account_entry = self._accounts.get(account.lower())
        if account_entry is None or group_id not in self._groups_from_account(account_entry):
            return False
        media_only_groups = self._media_only_groups_from_account(account_entry)
        currently_media_only = group_id in media_only_groups
        if currently_media_only == media_only:
            return False
        if media_only:
            media_only_groups.append(group_id)
        else:
            media_only_groups.remove(group_id)
        return True

    def is_media_only(self, group_id: str, account: str) -> bool:
        """返回指定群对账号的订阅是否仅接收带媒体的推文。"""

        account_entry = self._accounts.get(account.lower())
        if account_entry is None or group_id not in self._groups_from_account(account_entry):
            return False
        return group_id in self._media_only_groups_from_account(account_entry)

    def unsubscribe(self, group_id: str, account: str) -> bool:
        """从群移除账号订阅，并清理没有订阅关系的空记录。"""

        account_key = account.lower()
        account_entry = self._accounts.get(account_key)
        if account_entry is None:
            return False
        qq_groups = self._groups_from_account(account_entry)
        if group_id not in qq_groups:
            return False
        qq_groups.remove(group_id)
        media_only_groups = self._media_only_groups_from_account(account_entry)
        if group_id in media_only_groups:
            media_only_groups.remove(group_id)
        if not qq_groups:
            del self._accounts[account_key]
        if not any(group_id in self._groups_from_account(entry) for entry in self._accounts.values()):
            self._groups.pop(group_id, None)
        return True

    def set_push_enabled(self, group_id: str, enabled: bool) -> bool:
        """设置已有订阅群的推送开关，返回该群是否存在。"""

        if group_id not in self._groups:
            return False
        self._groups[group_id] = enabled
        return True

    def is_push_enabled(self, group_id: str) -> bool:
        """读取群推送开关；没有订阅记录的群视为未启用。"""

        return self._groups.get(group_id, False)

    def accounts_for_group(self, group_id: str) -> List[str]:
        """返回一个群订阅的账号副本。"""

        accounts: List[str] = []
        for account_entry in self._accounts.values():
            if group_id in self._groups_from_account(account_entry):
                accounts.append(self._account_name(account_entry))
        return accounts

    def subscriptions_for_group(self, group_id: str) -> List[Tuple[str, bool]]:
        """返回群内账号及其仅媒体标记的副本。"""

        subscriptions: List[Tuple[str, bool]] = []
        for account_entry in self._accounts.values():
            if group_id not in self._groups_from_account(account_entry):
                continue
            account = self._account_name(account_entry)
            subscriptions.append((account, self.is_media_only(group_id, account)))
        return subscriptions

    def set_group_media_only(self, group_id: str, media_only: bool) -> int:
        """批量修改一个群的全部账号订阅模式，返回实际变化数量。"""

        changed_count = 0
        for account, current_media_only in self.subscriptions_for_group(group_id):
            if current_media_only == media_only:
                continue
            if self.set_media_only(group_id, account, media_only):
                changed_count += 1
        return changed_count

    def display_name(self, account: str) -> str:
        """返回账号的推特显示名；尚未获取时返回空字符串。"""

        account_entry = self._accounts.get(account.lower())
        if account_entry is None:
            return ""
        display_name = account_entry.get("display_name", "")
        if not isinstance(display_name, str):
            raise TypeError("推特显示名必须是字符串")
        return display_name

    def set_display_name(self, account: str, display_name: str) -> bool:
        """更新已订阅账号的推特显示名，返回数据是否发生变化。"""

        account_entry = self._accounts.get(account.lower())
        if account_entry is None:
            return False
        normalized_name = re.sub(r"\s+", " ", display_name).strip()
        if not normalized_name or self.display_name(account) == normalized_name:
            return False
        account_entry["display_name"] = normalized_name
        return True

    def target_groups_by_account(self) -> Dict[str, List[str]]:
        """按账号汇总所有已启用的目标群。"""

        targets: Dict[str, List[str]] = {}
        for account_entry in self._accounts.values():
            enabled_groups = [
                group_id
                for group_id in self._groups_from_account(account_entry)
                if self._groups[group_id]
            ]
            if enabled_groups:
                targets[self._account_name(account_entry)] = enabled_groups
        return targets

    def target_subscriptions_by_account(self) -> Dict[str, Dict[str, bool]]:
        """按账号汇总已启用目标群及对应的仅媒体标记。"""

        targets: Dict[str, Dict[str, bool]] = {}
        for account_entry in self._accounts.values():
            account = self._account_name(account_entry)
            enabled_groups = {
                group_id: self.is_media_only(group_id, account)
                for group_id in self._groups_from_account(account_entry)
                if self._groups[group_id]
            }
            if enabled_groups:
                targets[account] = enabled_groups
        return targets

    def merge_legacy_global_subscriptions(self, accounts: List[str], group_ids: List[str]) -> bool:
        """把旧配置中的账号与群笛卡尔积合并进统一订阅结构。"""

        if not accounts or not group_ids:
            return False
        changed = False
        for group_id in group_ids:
            self._validate_group_id(group_id)
            if self._groups.get(group_id) is not True:
                self._groups[group_id] = True
                changed = True
            for account in accounts:
                if self.subscribe(group_id, account):
                    changed = True
        return changed

    def snapshot(self) -> Dict[str, object]:
        """返回适合持久化和后台只读展示的稳定结构。"""

        groups = [
            {"group_id": group_id, "enabled": enabled}
            for group_id, enabled in self._groups.items()
        ]
        accounts = []
        for account_entry in self._accounts.values():
            account_snapshot = {
                "account": self._account_name(account_entry),
                "qq_groups": list(self._groups_from_account(account_entry)),
                "media_only_qq_groups": list(
                    self._media_only_groups_from_account(account_entry)
                ),
            }
            display_name = self.display_name(self._account_name(account_entry))
            if display_name:
                account_snapshot["display_name"] = display_name
            accounts.append(account_snapshot)
        return {
            "version": SUBSCRIPTION_VERSION,
            "groups": groups,
            "accounts": accounts,
        }

    def group_count(self) -> int:
        """返回有账号订阅关系的 QQ 群数量。"""

        return len(self._groups)

    def account_count(self) -> int:
        """返回被至少一个 QQ 群订阅的账号数量。"""

        return len(self._accounts)

    def _load_version_one(self, raw_state: Dict[str, object]) -> None:
        """读取旧版按群存储的结构。"""

        raw_groups = raw_state.get("groups")
        if not isinstance(raw_groups, dict):
            raise ValueError(f"NitterToMaiBot 订阅文件结构无效: {self.path}")
        for group_id, raw_group in raw_groups.items():
            if not isinstance(group_id, str) or not group_id.isdigit() or not isinstance(raw_group, dict):
                raise ValueError(f"NitterToMaiBot 群订阅结构无效: {self.path}")
            enabled = raw_group.get("enabled")
            accounts = raw_group.get("accounts")
            if not isinstance(enabled, bool) or not isinstance(accounts, list):
                raise ValueError(f"NitterToMaiBot 群订阅字段无效: {self.path}")
            self._groups[group_id] = enabled
            for account in accounts:
                if not isinstance(account, str):
                    raise ValueError(f"NitterToMaiBot 群订阅账号无效: {self.path}")
                self.subscribe(group_id, account)
            self._groups[group_id] = enabled
        self._remove_empty_groups()

    def _load_account_mapping(
        self,
        raw_state: Dict[str, object],
        *,
        include_media_filters: bool,
    ) -> None:
        """读取第二、三版的群列表与账号列表结构。"""

        raw_groups = raw_state.get("groups")
        raw_accounts = raw_state.get("accounts")
        if not isinstance(raw_groups, list) or not isinstance(raw_accounts, list):
            raise ValueError(f"NitterToMaiBot 订阅文件结构无效: {self.path}")

        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                raise ValueError(f"NitterToMaiBot 群订阅结构无效: {self.path}")
            group_id = raw_group.get("group_id")
            enabled = raw_group.get("enabled")
            if not isinstance(group_id, str) or not group_id.isdigit() or not isinstance(enabled, bool):
                raise ValueError(f"NitterToMaiBot 群订阅字段无效: {self.path}")
            if group_id in self._groups:
                raise ValueError(f"NitterToMaiBot 群订阅存在重复群号: {group_id}")
            self._groups[group_id] = enabled

        for raw_account in raw_accounts:
            if not isinstance(raw_account, dict):
                raise ValueError(f"NitterToMaiBot 账号订阅结构无效: {self.path}")
            account = raw_account.get("account")
            display_name = raw_account.get("display_name", "")
            qq_groups = raw_account.get("qq_groups")
            media_only_qq_groups = (
                raw_account.get("media_only_qq_groups") if include_media_filters else []
            )
            if (
                not isinstance(account, str)
                or not isinstance(display_name, str)
                or not isinstance(qq_groups, list)
                or not isinstance(media_only_qq_groups, list)
            ):
                raise ValueError(f"NitterToMaiBot 账号订阅字段无效: {self.path}")
            self._validate_account(account)
            if account.lower() in self._accounts:
                raise ValueError(f"NitterToMaiBot 账号订阅存在重复账号: @{account}")
            if not all(isinstance(group_id, str) and group_id in self._groups for group_id in qq_groups):
                raise ValueError(f"NitterToMaiBot 账号订阅引用了不存在的群: @{account}")
            if len(set(qq_groups)) != len(qq_groups):
                raise ValueError(f"NitterToMaiBot 账号订阅存在重复群号: @{account}")
            if not all(
                isinstance(group_id, str) and group_id in qq_groups
                for group_id in media_only_qq_groups
            ):
                raise ValueError(f"NitterToMaiBot 仅媒体订阅引用了未订阅的群: @{account}")
            if len(set(media_only_qq_groups)) != len(media_only_qq_groups):
                raise ValueError(f"NitterToMaiBot 仅媒体订阅存在重复群号: @{account}")
            self._accounts[account.lower()] = {
                "account": account,
                "display_name": re.sub(r"\s+", " ", display_name).strip(),
                "qq_groups": list(qq_groups),
                "media_only_qq_groups": list(media_only_qq_groups),
            }
        self._remove_empty_groups()

    def _remove_empty_groups(self) -> None:
        referenced_groups = {
            group_id
            for account_entry in self._accounts.values()
            for group_id in self._groups_from_account(account_entry)
        }
        removed = [group_id for group_id in self._groups if group_id not in referenced_groups]
        for group_id in removed:
            del self._groups[group_id]
        if removed:
            self._needs_save = True

    @staticmethod
    def _account_name(account_entry: Dict[str, object]) -> str:
        account = account_entry["account"]
        if not isinstance(account, str):
            raise TypeError("订阅账号必须是字符串")
        return account

    @staticmethod
    def _groups_from_account(account_entry: Dict[str, object]) -> List[str]:
        qq_groups = account_entry["qq_groups"]
        if not isinstance(qq_groups, list) or not all(isinstance(group_id, str) for group_id in qq_groups):
            raise TypeError("账号订阅群必须是字符串列表")
        return qq_groups

    @staticmethod
    def _media_only_groups_from_account(account_entry: Dict[str, object]) -> List[str]:
        media_only_qq_groups = account_entry["media_only_qq_groups"]
        if not isinstance(media_only_qq_groups, list) or not all(
            isinstance(group_id, str) for group_id in media_only_qq_groups
        ):
            raise TypeError("仅媒体订阅群必须是字符串列表")
        return media_only_qq_groups

    @staticmethod
    def _validate_group_id(group_id: str) -> None:
        if not group_id.isdigit():
            raise ValueError(f"无效的 QQ 群号: {group_id}")

    @staticmethod
    def _validate_account(account: str) -> None:
        if not USERNAME_PATTERN.fullmatch(account):
            raise ValueError(f"无效的推特账号 ID: {account}")
