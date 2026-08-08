"""NitterToMaiBot 的内部数据模型。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List


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
    failed_accounts: int = 0
