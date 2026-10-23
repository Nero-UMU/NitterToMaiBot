"""NitterToMaiBot 的内部数据模型。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlsplit

import re


def normalize_post_id(raw_id: str) -> str:
    """将数字 ID 或 Nitter 推文 URL 统一为数字 ID，与实例域名无关。"""

    if re.fullmatch(r"[0-9]+", raw_id):
        return raw_id
    parsed_url = urlsplit(raw_id)
    match = re.search(r"/status/([0-9]+)/?$", parsed_url.path)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or not match:
        raise ValueError(f"无效的推文 ID: {raw_id}")
    return match.group(1)


@dataclass(frozen=True)
class HlsStreamSelection:
    """从 HLS 主播放清单中选出的音视频流。"""

    video_url: str
    audio_url: Optional[str]
    estimated_size_bytes: int


@dataclass(frozen=True)
class MediaAttachment:
    """推文中的一个媒体附件。"""

    url: str
    media_type: str
    mime_type: str = ""


@dataclass(frozen=True)
class NitterPost:
    """从 Nitter RSS 解析得到的一条推文。"""

    account: str
    post_id: str
    author: str
    text: str
    published_at: datetime
    url: str
    is_retweet: bool = False
    has_video: bool = False
    media: List[MediaAttachment] = field(default_factory=list)
    translated_text: str = ""


@dataclass
class ScanSummary:
    """一次扫描的统计结果。"""

    scanned_accounts: int = 0
    fetched_posts: int = 0
    forwarded_posts: int = 0
    dropped_posts: int = 0
    deferred_posts: int = 0
    failed_accounts: int = 0


@dataclass(frozen=True)
class CachedMedia:
    """插件媒体缓存目录中的一个文件，不存放任意外部绝对路径。"""

    relative_path: str
    media_type: str


@dataclass(frozen=True)
class StagedPost:
    """一条推文已下载的媒体，以及未成功缓存的附件。"""

    media: List[CachedMedia] = field(default_factory=list)
    failed_media_tokens: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PendingDelivery:
    """跨检查周期保留推文、原目标群及本地缓存，直到群转发周期结束。"""

    post: NitterPost
    group_ids: List[str]
    staged: Optional[StagedPost] = None
    staging_failed: bool = False
