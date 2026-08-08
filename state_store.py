"""插件去重与投递进度的持久化。"""

from pathlib import Path
from typing import Dict, List, Set

import json
import os


STATE_VERSION = 1


class StateStore:
    """维护已见推文和分群投递进度。"""

    def __init__(self, path: Path, max_seen_per_account: int) -> None:
        self.path = path
        self.max_seen_per_account = max_seen_per_account
        self._seen: Dict[str, List[str]] = {}
        self._progress: Dict[str, Dict[str, List[str]]] = {}

    def load(self) -> None:
        """从磁盘读取状态；损坏的状态文件会明确报错。"""

        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as state_file:
            raw_state = json.load(state_file)
        if not isinstance(raw_state, dict) or raw_state.get("version") != STATE_VERSION:
            raise ValueError(f"不支持的 NitterToMaiBot 状态文件: {self.path}")

        raw_seen = raw_state.get("seen")
        raw_progress = raw_state.get("progress")
        if not isinstance(raw_seen, dict) or not isinstance(raw_progress, dict):
            raise ValueError(f"NitterToMaiBot 状态文件结构无效: {self.path}")

        seen: Dict[str, List[str]] = {}
        for account, post_ids in raw_seen.items():
            if not isinstance(account, str) or not isinstance(post_ids, list) or not all(
                isinstance(post_id, str) for post_id in post_ids
            ):
                raise ValueError(f"NitterToMaiBot seen 状态结构无效: {self.path}")
            seen[account] = post_ids[-self.max_seen_per_account :]

        progress: Dict[str, Dict[str, List[str]]] = {}
        for post_key, group_progress in raw_progress.items():
            if not isinstance(post_key, str) or not isinstance(group_progress, dict):
                raise ValueError(f"NitterToMaiBot progress 状态结构无效: {self.path}")
            normalized_group_progress: Dict[str, List[str]] = {}
            for group_id, tokens in group_progress.items():
                if not isinstance(group_id, str) or not isinstance(tokens, list) or not all(
                    isinstance(token, str) for token in tokens
                ):
                    raise ValueError(f"NitterToMaiBot 群投递状态结构无效: {self.path}")
                normalized_group_progress[group_id] = tokens
            progress[post_key] = normalized_group_progress

        self._seen = seen
        self._progress = progress

    def save(self) -> None:
        """原子写入状态，避免进程中断留下半个 JSON。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        state = {
            "version": STATE_VERSION,
            "seen": self._seen,
            "progress": self._progress,
        }
        with temp_path.open("w", encoding="utf-8", newline="\n") as state_file:
            json.dump(state, state_file, ensure_ascii=False, indent=2)
            state_file.write("\n")
        os.replace(temp_path, self.path)

    def has_account(self, account: str) -> bool:
        """判断账号是否已经建立过首轮基线。"""

        return account in self._seen

    def is_seen(self, account: str, post_id: str) -> bool:
        """判断推文是否已经完成全部目标群投递。"""

        return post_id in self._seen.get(account, [])

    def mark_seen(self, account: str, post_id: str) -> None:
        """把推文标记为已完成，并清理临时投递进度。"""

        account_seen = self._seen.setdefault(account, [])
        if post_id not in account_seen:
            account_seen.append(post_id)
        self._seen[account] = account_seen[-self.max_seen_per_account :]
        self._progress.pop(self._post_key(account, post_id), None)

    def mark_baseline(self, account: str, post_ids: List[str]) -> None:
        """首次运行时记录当前 RSS 内容但不发送。"""

        unique_post_ids = list(dict.fromkeys(post_ids))
        self._seen[account] = unique_post_ids[-self.max_seen_per_account :]

    def completed_tokens(self, account: str, post_id: str, group_id: str) -> Set[str]:
        """读取一条推文在一个群中的已完成投递步骤。"""

        post_progress = self._progress.get(self._post_key(account, post_id), {})
        return set(post_progress.get(group_id, []))

    def mark_token_completed(self, account: str, post_id: str, group_id: str, token: str) -> None:
        """记录一个投递步骤，供失败后的精确续传使用。"""

        post_progress = self._progress.setdefault(self._post_key(account, post_id), {})
        completed = post_progress.setdefault(group_id, [])
        if token not in completed:
            completed.append(token)

    @staticmethod
    def _post_key(account: str, post_id: str) -> str:
        return f"{account}:{post_id}"

