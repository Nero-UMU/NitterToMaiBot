"""保存用户选择的私聊推送来源及其订阅时间。"""

from pathlib import Path
from typing import Dict, Set, Tuple

import hashlib
import json
import math
import os
import time


class PrivateSubscriptions:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._groups: Dict[str, Dict[str, float]] = {}

    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("groups"), dict):
            raise ValueError(f"私聊订阅文件结构无效: {self.path}")
        for group_id, users in data["groups"].items():
            if not group_id.isdecimal() or not isinstance(users, dict):
                raise ValueError(f"私聊订阅来源无效: {self.path}")
            for user_id, started_at in users.items():
                if (
                    not user_id.isdecimal() or isinstance(started_at, bool)
                    or not isinstance(started_at, (int, float)) or not math.isfinite(started_at) or started_at <= 0
                ):
                    raise ValueError(f"私聊订阅用户或时间无效: {self.path}")
        self._groups = data["groups"]

    def groups(self) -> Set[str]:
        return {group_id for group_id, users in self._groups.items() if users}

    def users(self, group_id: str) -> Dict[str, float]:
        return dict(self._groups.get(group_id, {}))

    def memberships(self) -> Set[Tuple[str, str]]:
        return {(group_id, user_id) for group_id, users in self._groups.items() for user_id in users}

    def revision(self) -> str:
        # 时间也参与校验，避免退订后重新订阅时旧页面误删新的关系。
        snapshot = json.dumps(self._groups, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(snapshot).hexdigest()

    def set_subscribed(self, group_id: str, user_id: str, enabled: bool) -> bool:
        membership = {(group_id, user_id)}
        current = self.memberships()
        return self.replace_memberships(current | membership if enabled else current - membership)

    def replace_memberships(self, memberships: Set[Tuple[str, str]]) -> bool:
        """整体替换订阅关系，保留未修改记录的订阅时间。"""

        if memberships == self.memberships():
            return False
        now = time.time()
        snapshot: Dict[str, Dict[str, float]] = {}
        for group_id, user_id in sorted(memberships):
            snapshot.setdefault(group_id, {})[user_id] = self._groups.get(group_id, {}).get(user_id, now)
        # 保存成功后才让扫描和投递看到新关系。
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "groups": snapshot}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        self._groups = snapshot
        return True
