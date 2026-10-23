"""订阅媒体的本地缓存，群发完成前保留，重试和多群共用同一份文件。"""

from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional

import os
import re
import shutil

from .models import CachedMedia, NitterPost, StagedPost
from .nitter_client import MediaTooLargeError


class MediaCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def post_key(post: NitterPost) -> str:
        return sha256(f"{post.account.lower()}:{post.post_id}".encode("utf-8")).hexdigest()

    def file(self, post: NitterPost, index: int, media_type: str) -> CachedMedia:
        suffix = {"image": ".jpg", "video": ".mp4", "file": ".bin"}[media_type]
        return CachedMedia(f"{self.post_key(post)}/{index}{suffix}", media_type)

    def path(self, media: CachedMedia) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}/[1-9][0-9]*\.(?:jpg|mp4|bin)", media.relative_path):
            raise ValueError("媒体缓存路径无效")
        path = (self.root / media.relative_path).resolve()
        path.relative_to(self.root.resolve())
        return path

    def existing(self, media: CachedMedia, max_bytes: int) -> bool:
        path = self.path(media)
        if not path.exists():
            return False
        size = path.stat().st_size
        if not path.is_file() or size <= 0:
            raise ValueError(f"媒体缓存不是有效文件: {path}")
        if size > max_bytes:
            raise MediaTooLargeError(f"媒体缓存超过大小上限: {path}")
        return True

    def size_bytes(self, staged: StagedPost) -> int:
        """统计实际缓存的原始附件体积，包含图片、视频和文件。"""

        return sum(self.path(media).stat().st_size for media in staged.media)

    def write(self, media: CachedMedia, data: bytes, source: Optional[Path] = None) -> None:
        path = self.path(media)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        try:
            if source is None:
                temporary.write_bytes(data)
            else:
                shutil.copyfile(source, temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self, post: NitterPost) -> None:
        directory = self.root / self.post_key(post)
        if directory.is_symlink():
            raise ValueError("拒绝清理符号链接媒体缓存目录")
        directory.resolve().relative_to(self.root.resolve())
        if directory.exists():
            shutil.rmtree(directory)

    def nodes(self, post: NitterPost, staged: StagedPost, user_id: str, text: str) -> List[Dict[str, Any]]:
        """正文和图片同节点，视频/文件独立节点，但均位于同一个聊天记录中。"""

        body: List[Dict[str, Any]] = [{"type": "text", "data": {"text": text}}]
        attachments: List[List[Dict[str, Any]]] = []
        for media in staged.media:
            path = self.path(media)
            if path.parent.name != self.post_key(post) or not path.is_file() or path.stat().st_size <= 0:
                raise ValueError(f"推文媒体缓存缺失或归属错误: {path}")
            segment = {"type": media.media_type, "data": {"file": str(path)}}
            if media.media_type == "image":
                body.append(segment)
            else:
                attachments.append([segment])
        return [
            {"type": "node", "data": {"user_id": user_id, "nickname": post.author, "content": content}}
            for content in [body, *attachments]
        ]

    def sdk_nodes(self, post: NitterPost, staged: StagedPost, text: str) -> List[Dict[str, Any]]:
        """相同节点布局转换为 SDK 转发格式；媒体用 dict 段保留本地文件引用。"""

        result: List[Dict[str, Any]] = []
        for index, node in enumerate(self.nodes(post, staged, "0", text)):
            result.append({
                "user_id": node["data"]["user_id"],
                "nickname": node["data"]["nickname"],
                "message_id": post.post_id if index == 0 else f"{post.post_id}-media-{index}",
                "segments": [
                    {"type": "text", "content": segment["data"]["text"]}
                    if segment["type"] == "text" else {"type": "dict", "data": segment}
                    for segment in node["data"]["content"]
                ],
            })
        return result
