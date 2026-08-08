"""为推文媒体提供落盘缓存、临时 HTTP 访问和定时清理基础能力。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import formatdate
from hashlib import sha256
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import asyncio
import mimetypes
import os
import re
import secrets


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
MEDIA_FILE_PATTERN = re.compile(r"^[0-9a-f]{64}\.[A-Za-z0-9]{1,8}$")
RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
DownloadToFile = Callable[[str, Path, int], Awaitable[Tuple[int, str]]]


class MediaCacheLimitError(RuntimeError):
    """现有缓存文件超过当前配置的媒体大小上限。"""


@dataclass(frozen=True)
class CachedMedia:
    """一份已经落盘且可通过临时 HTTP 地址访问的媒体。"""

    path: Path
    public_url: str
    content_type: str
    size: int


class MediaCacheService:
    """管理媒体缓存文件及只读 HTTP 文件服务。"""

    def __init__(
        self,
        data_dir: Path,
        bind_host: str,
        port: int,
        public_base_url: str,
        logger: Any,
    ) -> None:
        self.data_dir = data_dir
        self.cache_dir = data_dir / "media_cache"
        self.token_path = data_dir / "media_url_token.txt"
        self.bind_host = bind_host
        self.port = port
        self.public_base_url = public_base_url.rstrip("/")
        self.logger = logger
        self._server: Optional[asyncio.AbstractServer] = None
        self._token = ""
        self._download_locks: Dict[str, asyncio.Lock] = {}

    @property
    def is_running(self) -> bool:
        """返回临时 HTTP 文件服务是否正在监听。"""

        return self._server is not None

    def initialize(self) -> None:
        """创建缓存目录并加载稳定的随机访问令牌。"""

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.token_path.is_file():
            token = self.token_path.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{24,}", token):
                self._token = token
                return

        token = secrets.token_urlsafe(32)
        temp_path = self.token_path.with_suffix(".tmp")
        try:
            temp_path.write_text(token, encoding="utf-8")
            os.replace(temp_path, self.token_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        self._token = token

    def has_cached_files(self) -> bool:
        """判断缓存目录中是否存在已经完成下载的媒体。"""

        if not self.cache_dir.is_dir():
            return False
        return any(path.is_file() and MEDIA_FILE_PATTERN.fullmatch(path.name) for path in self.cache_dir.iterdir())

    async def start(self) -> None:
        """启动只允许通过随机令牌访问的本地 HTTP 文件服务。"""

        if self._server is not None:
            return
        if not self._token:
            await asyncio.to_thread(self.initialize)
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.bind_host,
            port=self.port,
        )
        sockets = self._server.sockets or []
        bound_port = int(sockets[0].getsockname()[1]) if sockets else self.port
        self.logger.info(
            "推文媒体临时文件服务已启动：http://%s:%d",
            self.bind_host,
            bound_port,
        )

    async def stop(self) -> None:
        """停止临时 HTTP 文件服务。"""

        if self._server is None:
            return
        server = self._server
        self._server = None
        server.close()
        await server.wait_closed()

    async def cache_media(
        self,
        source_url: str,
        file_name: str,
        max_bytes: int,
        downloader: DownloadToFile,
    ) -> CachedMedia:
        """下载并缓存媒体；同一来源的并发请求只执行一次下载。"""

        if not self._token:
            await asyncio.to_thread(self.initialize)
        cache_name = self._build_cache_name(source_url, file_name)
        target_path = self.cache_dir / cache_name
        download_lock = self._download_locks.setdefault(source_url, asyncio.Lock())
        async with download_lock:
            if target_path.is_file() and target_path.stat().st_size > 0:
                size = target_path.stat().st_size
                if size > max_bytes:
                    raise MediaCacheLimitError(
                        f"缓存文件大小 {size} 字节超过当前限制 {max_bytes} 字节: {source_url}"
                    )
                content_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"
            else:
                temp_path = self.cache_dir / f".{cache_name}.{secrets.token_hex(6)}.part"
                try:
                    size, content_type = await downloader(source_url, temp_path, max_bytes)
                    if not temp_path.is_file():
                        raise RuntimeError(f"媒体下载完成后没有生成临时文件: {source_url}")
                    actual_size = temp_path.stat().st_size
                    if actual_size != size:
                        raise RuntimeError(
                            f"媒体下载大小校验失败，返回 {size} 字节，实际 {actual_size} 字节: {source_url}"
                        )
                    await asyncio.to_thread(os.replace, temp_path, target_path)
                finally:
                    if temp_path.exists():
                        temp_path.unlink()

        await self.start()
        return CachedMedia(
            path=target_path,
            public_url=self._build_public_url(cache_name),
            content_type=content_type or mimetypes.guess_type(target_path.name)[0] or "application/octet-stream",
            size=size,
        )

    async def clear(self) -> Tuple[int, int]:
        """删除全部已完成的媒体缓存，返回文件数和总字节数。"""

        return await asyncio.to_thread(self._clear_sync)

    def _clear_sync(self) -> Tuple[int, int]:
        removed_files = 0
        removed_bytes = 0
        if not self.cache_dir.is_dir():
            return removed_files, removed_bytes
        for path in self.cache_dir.iterdir():
            if not path.is_file() or not MEDIA_FILE_PATTERN.fullmatch(path.name):
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            removed_files += 1
            removed_bytes += size
        return removed_files, removed_bytes

    def _build_cache_name(self, source_url: str, file_name: str) -> str:
        suffix = Path(file_name).suffix.lower()
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
            suffix = Path(urlsplit(source_url).path).suffix.lower()
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
            suffix = ".bin"
        return f"{sha256(source_url.encode('utf-8')).hexdigest()}{suffix}"

    def _build_public_url(self, cache_name: str) -> str:
        base_url = self._effective_public_base_url()
        return f"{base_url}/nitter-media/{self._token}/{quote(cache_name)}"

    def _effective_public_base_url(self) -> str:
        if self.port != 0 or self._server is None:
            return self.public_base_url
        sockets = self._server.sockets or []
        if not sockets:
            return self.public_base_url
        actual_port = int(sockets[0].getsockname()[1])
        parsed_url = urlsplit(self.public_base_url)
        host = parsed_url.hostname or self.bind_host
        netloc = f"{host}:{actual_port}"
        return urlunsplit((parsed_url.scheme, netloc, "", "", "")).rstrip("/")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理单个只读 HTTP 请求，并支持视频播放器常用的 Range。"""

        try:
            raw_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            request_lines = raw_headers.decode("iso-8859-1").split("\r\n")
            method, raw_target, _version = request_lines[0].split(" ", maxsplit=2)
            headers = self._parse_headers(request_lines[1:])
            if method not in {"GET", "HEAD"}:
                await self._send_error(writer, 405, "Method Not Allowed")
                return

            file_path = self._resolve_request_path(raw_target)
            if file_path is None or not file_path.is_file():
                await self._send_error(writer, 404, "Not Found")
                return
            await self._send_file(writer, method, file_path, headers.get("range", ""))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError):
            await self._send_error(writer, 400, "Bad Request")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.logger.warning("推文媒体临时文件服务处理请求失败", exc_info=True)
            try:
                await self._send_error(writer, 500, "Internal Server Error")
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    @staticmethod
    def _parse_headers(header_lines: list[str]) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        for line in header_lines:
            if not line or ":" not in line:
                continue
            name, value = line.split(":", maxsplit=1)
            headers[name.strip().lower()] = value.strip()
        return headers

    def _resolve_request_path(self, raw_target: str) -> Optional[Path]:
        request_path = unquote(urlsplit(raw_target).path)
        prefix = f"/nitter-media/{self._token}/"
        if not request_path.startswith(prefix):
            return None
        file_name = request_path.removeprefix(prefix)
        if not MEDIA_FILE_PATTERN.fullmatch(file_name):
            return None
        return self.cache_dir / file_name

    async def _send_file(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        file_path: Path,
        raw_range: str,
    ) -> None:
        file_size = file_path.stat().st_size
        byte_range = self._parse_range(raw_range, file_size)
        if raw_range and byte_range is None:
            headers = {"Content-Range": f"bytes */{file_size}"}
            await self._write_headers(writer, 416, "Range Not Satisfiable", headers, 0)
            return

        start, end = byte_range if byte_range is not None else (0, max(0, file_size - 1))
        content_length = 0 if file_size == 0 else end - start + 1
        status_code = 206 if byte_range is not None else 200
        reason = "Partial Content" if status_code == 206 else "OK"
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=86400",
            "Content-Type": mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
            "Last-Modified": formatdate(file_path.stat().st_mtime, usegmt=True),
        }
        if byte_range is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        await self._write_headers(writer, status_code, reason, headers, content_length)
        if method == "HEAD" or content_length == 0:
            return

        with file_path.open("rb") as media_file:
            media_file.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = await asyncio.to_thread(media_file.read, min(1024 * 1024, remaining))
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                remaining -= len(chunk)

    @staticmethod
    def _parse_range(raw_range: str, file_size: int) -> Optional[Tuple[int, int]]:
        if not raw_range:
            return None
        match = RANGE_PATTERN.fullmatch(raw_range.strip())
        if match is None or file_size <= 0:
            return None
        raw_start, raw_end = match.groups()
        if not raw_start and not raw_end:
            return None
        if not raw_start:
            suffix_length = int(raw_end)
            if suffix_length <= 0:
                return None
            return max(0, file_size - suffix_length), file_size - 1

        start = int(raw_start)
        end = int(raw_end) if raw_end else file_size - 1
        if start >= file_size or end < start:
            return None
        return start, min(end, file_size - 1)

    async def _send_error(self, writer: asyncio.StreamWriter, status_code: int, reason: str) -> None:
        body = f"{status_code} {reason}\n".encode("utf-8")
        await self._write_headers(
            writer,
            status_code,
            reason,
            {"Content-Type": "text/plain; charset=utf-8"},
            len(body),
        )
        writer.write(body)
        await writer.drain()

    @staticmethod
    async def _write_headers(
        writer: asyncio.StreamWriter,
        status_code: int,
        reason: str,
        headers: Dict[str, str],
        content_length: int,
    ) -> None:
        response_headers = {
            "Connection": "close",
            "Content-Length": str(content_length),
            "Server": "NitterToMaiBot",
            **headers,
        }
        header_text = f"HTTP/1.1 {status_code} {reason}\r\n"
        header_text += "".join(f"{name}: {value}\r\n" for name, value in response_headers.items())
        writer.write(f"{header_text}\r\n".encode("iso-8859-1"))
        await writer.drain()


def seconds_until_cleanup(cleanup_time: str, now: Optional[datetime] = None) -> float:
    """计算距离下一次北京时间每日清理的秒数。"""

    current_time = now.astimezone(BEIJING_TIMEZONE) if now is not None else datetime.now(BEIJING_TIMEZONE)
    hour, minute = (int(part) for part in cleanup_time.split(":", maxsplit=1))
    next_cleanup = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_cleanup <= current_time:
        next_cleanup += timedelta(days=1)
    return max(0.0, (next_cleanup - current_time).total_seconds())
