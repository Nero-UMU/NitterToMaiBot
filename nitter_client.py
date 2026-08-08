"""访问 Nitter RSS、状态页和媒体资源。"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import asyncio
import re
import time

from .models import MediaAttachment, NitterPost


MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")
NITTER_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class NitterClientError(RuntimeError):
    """Nitter 请求或响应处理失败。"""


class MediaTooLargeError(NitterClientError):
    """媒体文件超过配置的大小上限。"""


class _DescriptionParser(HTMLParser):
    """将 RSS description 中的 HTML 转成文本并提取媒体。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self.media: List[Tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attributes = dict(attrs)
        normalized_tag = tag.lower()
        if normalized_tag in {"br", "p", "blockquote", "hr"}:
            self._parts.append("\n")
        if normalized_tag == "img" and attributes.get("src"):
            self.media.append((str(attributes["src"]), "image", ""))
        if normalized_tag == "source" and attributes.get("src"):
            mime_type = str(attributes.get("type") or "")
            media_type = "video" if mime_type.startswith("video/") else "file"
            self.media.append((str(attributes["src"]), media_type, mime_type))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "blockquote"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        """返回压缩空白后的纯文本。"""

        lines = []
        for line in "".join(self._parts).splitlines():
            normalized_line = re.sub(r"\s+", " ", line).strip()
            if normalized_line:
                lines.append(normalized_line)
        return "\n".join(lines)


class _MainTweetMediaParser(HTMLParser):
    """解析状态页主推文，排除回复区和引用推文的内容。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_main_tweet = False
        self._main_tweet_div_depth = 0
        self._quote_div_depth = 0
        self._content_div_depth = 0
        self._content_parts: List[str] = []
        self._author_parts: List[str] = []
        self._capture_author = False
        self._content_captured = False
        self.author = ""
        self.has_video = False
        self.media: List[Tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attributes = dict(attrs)
        normalized_tag = tag.lower()

        if not self._inside_main_tweet:
            classes = str(attributes.get("class") or "").split()
            if normalized_tag == "div" and "main-tweet" in classes:
                self._inside_main_tweet = True
                self._main_tweet_div_depth = 1
            return

        classes = str(attributes.get("class") or "").split()
        if normalized_tag == "div":
            self._main_tweet_div_depth += 1
            if self._quote_div_depth > 0:
                self._quote_div_depth += 1
            elif "quote" in classes:
                self._quote_div_depth = 1
            elif not self._content_captured and "tweet-content" in classes:
                self._content_div_depth = self._main_tweet_div_depth

        if self._quote_div_depth > 0:
            return
        if normalized_tag == "a" and "username" in classes and not self.author:
            self._capture_author = True
        if normalized_tag == "br" and self._content_div_depth > 0:
            self._content_parts.append("\n")
        if normalized_tag == "a" and "still-image" in classes and attributes.get("href"):
            self.media.append((str(attributes["href"]), "image", ""))
        if normalized_tag in {"video", "source"}:
            self.has_video = True
        if normalized_tag == "div" and "video-overlay" in classes:
            self.has_video = True
        if normalized_tag == "source" and attributes.get("src"):
            mime_type = str(attributes.get("type") or "")
            media_type = "video" if mime_type.startswith("video/") else "file"
            self.media.append((str(attributes["src"]), media_type, mime_type))

    def handle_data(self, data: str) -> None:
        if self._quote_div_depth > 0:
            return
        if self._capture_author:
            self._author_parts.append(data)
        if self._content_div_depth > 0:
            self._content_parts.append(data)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if not self._inside_main_tweet:
            return
        normalized_tag = tag.lower()
        if normalized_tag == "a" and self._capture_author:
            self._capture_author = False
            self.author = "".join(self._author_parts).strip()
        if normalized_tag != "div":
            return
        if self._content_div_depth == self._main_tweet_div_depth:
            self._content_div_depth = 0
            self._content_captured = True
        if self._quote_div_depth > 0:
            self._quote_div_depth -= 1
        self._main_tweet_div_depth -= 1
        if self._main_tweet_div_depth == 0:
            self._inside_main_tweet = False

    def text(self) -> str:
        """返回状态页主推文的纯文本。"""

        lines = []
        for line in "".join(self._content_parts).splitlines():
            normalized_line = re.sub(r"\s+", " ", line).strip()
            if normalized_line:
                lines.append(normalized_line)
        return "\n".join(lines)


class NitterClient:
    """通过标准库异步封装 Nitter 的 HTTP 接口。"""

    def __init__(self, base_url: str, timeout_seconds: int, request_attempts: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.request_attempts = request_attempts
        self._profile_names: Dict[str, str] = {}

    async def fetch_timeline(self, account: str) -> List[NitterPost]:
        """获取一个账号的 RSS 时间线。"""

        normalized_account = account.lstrip("@").strip()
        if not USERNAME_PATTERN.fullmatch(normalized_account):
            raise ValueError(f"无效的推特账号 ID: {account}")
        rss_url = f"{self.base_url}/{quote(normalized_account)}/rss"
        raw_data, _content_type = await asyncio.to_thread(
            self._request_bytes,
            rss_url,
            MAX_DOCUMENT_BYTES,
        )
        profile_name = self.parse_profile_name(raw_data, normalized_account)
        if profile_name:
            self._profile_names[normalized_account.lower()] = profile_name
        return self.parse_rss(raw_data, normalized_account)

    def profile_name(self, account: str) -> str:
        """返回本客户端最近一次 RSS 请求解析到的账号显示名。"""

        return self._profile_names.get(account.lstrip("@").strip().lower(), "")

    @staticmethod
    def parse_profile_name(raw_data: bytes, account: str) -> str:
        """从 Nitter RSS 频道标题提取推特显示名。"""

        try:
            root = ElementTree.fromstring(raw_data)
        except ElementTree.ParseError as exc:
            raise NitterClientError(f"Nitter RSS XML 解析失败: {exc}") from exc
        channel_title = root.findtext("./channel/title", default="").strip()
        if not channel_title:
            return ""

        escaped_account = re.escape(account.lstrip("@").strip())
        display_name = re.sub(
            rf"\s*(?:/\s*@{escaped_account}|\(@{escaped_account}\))\s*$",
            "",
            channel_title,
            flags=re.IGNORECASE,
        ).strip()
        if not display_name or display_name.lstrip("@").lower() == account.lstrip("@").lower():
            return ""
        return display_name

    async def enrich_status_media(self, post: NitterPost) -> NitterPost:
        """从状态页补充 RSS 没有给出的真实视频地址。"""

        raw_data, _content_type = await asyncio.to_thread(
            self._request_bytes,
            post.url,
            MAX_DOCUMENT_BYTES,
        )
        parser = _MainTweetMediaParser()
        parser.feed(raw_data.decode("utf-8"))
        combined_media = list(post.media)
        known_urls = {media.url for media in combined_media}
        for raw_url, media_type, mime_type in parser.media:
            if media_type == "image":
                continue
            media_url = self._absolute_url(raw_url)
            if media_url not in known_urls:
                combined_media.append(
                    MediaAttachment(url=media_url, media_type=media_type, mime_type=mime_type)
                )
                known_urls.add(media_url)
        return NitterPost(
            account=post.account,
            post_id=post.post_id,
            author=post.author,
            text=post.text,
            published_at=post.published_at,
            url=post.url,
            is_retweet=post.is_retweet,
            has_video=post.has_video,
            media=combined_media,
            translated_text=post.translated_text,
        )

    async def fetch_status(self, account: str, post_id: str) -> NitterPost:
        """通过配置的 Nitter 实例解析一条指定推文。"""

        normalized_account = account.lstrip("@").strip()
        if not USERNAME_PATTERN.fullmatch(normalized_account):
            raise ValueError(f"无效的推特账号 ID: {account}")
        if not post_id.isdigit():
            raise ValueError(f"无效的推文 ID: {post_id}")

        status_url = f"{self.base_url}/{quote(normalized_account)}/status/{post_id}"
        raw_data, _content_type = await asyncio.to_thread(
            self._request_bytes,
            status_url,
            MAX_DOCUMENT_BYTES,
        )
        parser = _MainTweetMediaParser()
        parser.feed(raw_data.decode("utf-8"))
        text = parser.text()
        if not text:
            raise NitterClientError(f"Nitter 状态页中未找到推文正文: {status_url}")

        media: List[MediaAttachment] = []
        known_urls = set()
        for raw_url, media_type, mime_type in parser.media:
            media_url = self._localize_media_url(raw_url)
            if media_url not in known_urls:
                media.append(MediaAttachment(media_url, media_type, mime_type))
                known_urls.add(media_url)

        author = parser.author if parser.author else f"@{normalized_account}"
        return NitterPost(
            account=normalized_account,
            post_id=post_id,
            author=author,
            text=text,
            published_at=self._published_at_from_post_id(post_id),
            url=status_url,
            has_video=parser.has_video,
            media=media,
        )

    async def download_media(self, media_url: str, max_bytes: int) -> Tuple[bytes, str]:
        """下载一个媒体文件，并在读取过程中执行严格的大小限制。"""

        return await asyncio.to_thread(self._request_bytes, media_url, max_bytes)

    def parse_rss(self, raw_data: bytes, account: str) -> List[NitterPost]:
        """解析 Nitter RSS 文档。"""

        try:
            root = ElementTree.fromstring(raw_data)
        except ElementTree.ParseError as exc:
            raise NitterClientError(f"Nitter RSS XML 解析失败: {exc}") from exc

        posts: List[NitterPost] = []
        for item in root.findall("./channel/item"):
            post_id = self._required_text(item, "guid")
            title = self._required_text(item, "title")
            raw_description = item.findtext("description", default="")
            raw_link = self._required_text(item, "link")
            raw_published_at = self._required_text(item, "pubDate")
            author = item.findtext("{http://purl.org/dc/elements/1.1/}creator", default=f"@{account}").strip()

            description_parser = _DescriptionParser()
            description_parser.feed(raw_description)
            description_text = description_parser.text()
            text = description_text if description_text else title.strip()
            published_at = self._parse_published_at(raw_published_at)
            has_video = bool(
                re.search(r"<br\s*/?>\s*(?:Video|GIF)\s*<br\s*/?>", raw_description, re.IGNORECASE)
                or re.search(r"<(?:video|source)\b", raw_description, re.IGNORECASE)
            )

            media: List[MediaAttachment] = []
            known_urls = set()
            for raw_url, media_type, mime_type in description_parser.media:
                media_url = self._localize_media_url(raw_url)
                if media_url not in known_urls:
                    media.append(MediaAttachment(media_url, media_type, mime_type))
                    known_urls.add(media_url)

            for enclosure in item.findall("enclosure"):
                raw_url = str(enclosure.attrib.get("url") or "").strip()
                if not raw_url:
                    continue
                mime_type = str(enclosure.attrib.get("type") or "").strip()
                media_type = self._media_type_from_mime(mime_type)
                media_url = self._localize_media_url(raw_url)
                if media_url not in known_urls:
                    media.append(MediaAttachment(media_url, media_type, mime_type))
                    known_urls.add(media_url)

            posts.append(
                NitterPost(
                    account=account,
                    post_id=post_id,
                    author=author,
                    text=text,
                    published_at=published_at,
                    url=self._localize_nitter_url(raw_link),
                    is_retweet=author.lstrip("@").lower() != account.lower(),
                    has_video=has_video,
                    media=media,
                )
            )
        return posts

    def _request_bytes(self, url: str, max_bytes: int) -> Tuple[bytes, str]:
        request_url = self._normalize_request_url(url)
        parsed_url = urlsplit(request_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise NitterClientError(f"不支持的 URL: {url}")

        request = Request(request_url, headers=NITTER_REQUEST_HEADERS)
        last_error: Optional[Exception] = None
        for attempt in range(1, self.request_attempts + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    raw_length = response.headers.get("Content-Length")
                    if raw_length and int(raw_length) > max_bytes:
                        raise MediaTooLargeError(
                            f"资源大小 {int(raw_length)} 字节超过限制 {max_bytes} 字节: {url}"
                        )
                    content = response.read(max_bytes + 1)
                    if len(content) > max_bytes:
                        raise MediaTooLargeError(f"资源超过限制 {max_bytes} 字节: {url}")
                    content_type = response.headers.get_content_type()
                    return content, content_type
            except MediaTooLargeError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.request_attempts or not self._is_retriable_error(exc):
                    break
                time.sleep(0.25 * attempt)

        raise NitterClientError(
            f"请求失败（已尝试 {self.request_attempts} 次）: {url}: {last_error}"
        ) from last_error

    def _localize_nitter_url(self, raw_url: str) -> str:
        absolute_url = self._absolute_url(raw_url)
        parsed_url = urlsplit(absolute_url)
        base = urlsplit(self.base_url)
        return urlunsplit((base.scheme, base.netloc, parsed_url.path, parsed_url.query, ""))

    def _localize_media_url(self, raw_url: str) -> str:
        """将 Nitter 代理资源改写到配置实例，保留 Twitter CDN 直链。"""

        absolute_url = self._absolute_url(raw_url)
        parsed_url = urlsplit(absolute_url)
        if raw_url.startswith("/") or parsed_url.path.startswith(("/pic/", "/i/videos/")):
            return self._localize_nitter_url(absolute_url)
        return absolute_url

    def _absolute_url(self, raw_url: str) -> str:
        return urljoin(f"{self.base_url}/", raw_url)

    @staticmethod
    def _normalize_request_url(url: str) -> str:
        """移除只供浏览器页面定位使用、不得进入 HTTP 请求的 fragment。"""

        parsed_url = urlsplit(url)
        return urlunsplit(
            (
                parsed_url.scheme,
                parsed_url.netloc,
                parsed_url.path,
                parsed_url.query,
                "",
            )
        )

    @staticmethod
    def _required_text(item: ElementTree.Element, tag: str) -> str:
        value = item.findtext(tag, default="").strip()
        if not value:
            raise NitterClientError(f"RSS item 缺少必要字段: {tag}")
        return value

    @staticmethod
    def _parse_published_at(value: str) -> datetime:
        try:
            published_at = parsedate_to_datetime(value)
        except (TypeError, ValueError) as exc:
            raise NitterClientError(f"无法解析推文发布时间: {value}") from exc
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        return published_at

    @staticmethod
    def _media_type_from_mime(mime_type: str) -> str:
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("video/"):
            return "video"
        return "file"

    @staticmethod
    def _published_at_from_post_id(post_id: str) -> datetime:
        """从 Twitter Snowflake ID 还原精确的 UTC 发布时间。"""

        twitter_epoch_ms = 1288834974657
        timestamp_ms = (int(post_id) >> 22) + twitter_epoch_ms
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)

    @staticmethod
    def _is_retriable_error(error: Exception) -> bool:
        """判断是否属于 Nitter/CDN 常见的瞬时响应错误。"""

        if isinstance(error, HTTPError):
            return error.code in {404, 408, 429, 500, 502, 503, 504}
        return isinstance(error, (OSError, TimeoutError))
