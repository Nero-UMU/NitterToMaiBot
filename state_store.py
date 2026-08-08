"""插件去重、静默积压与投递进度的持久化。"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set

import json
import os

from .models import MediaAttachment, NitterPost


STATE_VERSION = 2
LEGACY_STATE_VERSION = 1


class StateStore:
    """维护已见推文和分群投递进度。"""

    def __init__(self, path: Path, max_seen_per_account: int) -> None:
        self.path = path
        self.max_seen_per_account = max_seen_per_account
        self._seen: Dict[str, List[str]] = {}
        self._progress: Dict[str, Dict[str, List[str]]] = {}
        self._quiet_queue: Dict[str, NitterPost] = {}

    def load(self) -> None:
        """从磁盘读取状态；损坏的状态文件会明确报错。"""

        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as state_file:
            raw_state = json.load(state_file)
        if not isinstance(raw_state, dict) or raw_state.get("version") not in {
            LEGACY_STATE_VERSION,
            STATE_VERSION,
        }:
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
        raw_quiet_queue = raw_state.get("quiet_queue", [])
        if not isinstance(raw_quiet_queue, list):
            raise ValueError(f"NitterToMaiBot 静默积压状态结构无效: {self.path}")
        quiet_queue: Dict[str, NitterPost] = {}
        for raw_post in raw_quiet_queue:
            post = self._deserialize_post(raw_post)
            quiet_queue[self._post_key(post.account, post.post_id)] = post
        self._quiet_queue = quiet_queue

    def save(self) -> None:
        """原子写入状态，避免进程中断留下半个 JSON。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        state = {
            "version": STATE_VERSION,
            "seen": self._seen,
            "progress": self._progress,
            "quiet_queue": [self._serialize_post(post) for post in self._quiet_queue.values()],
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
        self._quiet_queue.pop(self._post_key(account, post_id), None)

    def mark_baseline(self, account: str, post_ids: List[str]) -> None:
        """首次运行时记录当前 RSS 内容但不发送。"""

        unique_post_ids = list(dict.fromkeys(post_ids))
        self._seen[account] = unique_post_ids[-self.max_seen_per_account :]
        for post_id in unique_post_ids:
            self._quiet_queue.pop(self._post_key(account, post_id), None)

    def queue_quiet_post(self, post: NitterPost) -> bool:
        """把静默时段发现的推文加入持久化积压，返回是否新增。"""

        post_key = self._post_key(post.account, post.post_id)
        if post_key in self._quiet_queue:
            return False
        self._quiet_queue[post_key] = post
        return True

    def is_quiet_queued(self, account: str, post_id: str) -> bool:
        """判断推文是否已经进入静默积压。"""

        return self._post_key(account, post_id) in self._quiet_queue

    def quiet_posts(self) -> List[NitterPost]:
        """返回静默积压推文的快照。"""

        return list(self._quiet_queue.values())

    def quiet_post_count(self) -> int:
        """返回当前静默积压数量。"""

        return len(self._quiet_queue)

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

    @staticmethod
    def _serialize_post(post: NitterPost) -> Dict[str, Any]:
        """把推文转换为可写入 JSON 的静默积压结构。"""

        return {
            "account": post.account,
            "post_id": post.post_id,
            "author": post.author,
            "text": post.text,
            "published_at": post.published_at.isoformat(),
            "url": post.url,
            "is_retweet": post.is_retweet,
            "has_video": post.has_video,
            "media": [
                {
                    "url": media.url,
                    "media_type": media.media_type,
                    "mime_type": media.mime_type,
                }
                for media in post.media
            ],
            "translated_text": post.translated_text,
        }

    def _deserialize_post(self, raw_post: object) -> NitterPost:
        """严格校验并恢复一条静默积压推文。"""

        if not isinstance(raw_post, dict):
            raise ValueError(f"NitterToMaiBot 静默积压推文结构无效: {self.path}")

        string_fields = ("account", "post_id", "author", "text", "published_at", "url")
        if not all(isinstance(raw_post.get(field), str) for field in string_fields):
            raise ValueError(f"NitterToMaiBot 静默积压推文字段无效: {self.path}")
        if not isinstance(raw_post.get("is_retweet", False), bool) or not isinstance(
            raw_post.get("has_video", False),
            bool,
        ):
            raise ValueError(f"NitterToMaiBot 静默积压推文标记无效: {self.path}")
        translated_text = raw_post.get("translated_text", "")
        if not isinstance(translated_text, str):
            raise ValueError(f"NitterToMaiBot 静默积压翻译字段无效: {self.path}")

        raw_media = raw_post.get("media", [])
        if not isinstance(raw_media, list):
            raise ValueError(f"NitterToMaiBot 静默积压媒体结构无效: {self.path}")
        media_items: List[MediaAttachment] = []
        for raw_item in raw_media:
            if not isinstance(raw_item, dict) or not all(
                isinstance(raw_item.get(field), str)
                for field in ("url", "media_type", "mime_type")
            ):
                raise ValueError(f"NitterToMaiBot 静默积压媒体字段无效: {self.path}")
            media_items.append(
                MediaAttachment(
                    url=raw_item["url"],
                    media_type=raw_item["media_type"],
                    mime_type=raw_item["mime_type"],
                )
            )

        published_at = datetime.fromisoformat(raw_post["published_at"])
        if published_at.tzinfo is None:
            raise ValueError(f"NitterToMaiBot 静默积压时间缺少时区: {self.path}")
        return NitterPost(
            account=raw_post["account"],
            post_id=raw_post["post_id"],
            author=raw_post["author"],
            text=raw_post["text"],
            published_at=published_at,
            url=raw_post["url"],
            is_retweet=raw_post.get("is_retweet", False),
            has_video=raw_post.get("has_video", False),
            media=media_items,
            translated_text=translated_text,
        )
