"""媒体落盘缓存、临时 HTTP 服务与清理调度测试。"""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import asyncio

from plugins.NitterToMaiBot.media_cache import MediaCacheService, seconds_until_cleanup


class _TestLogger:
    """测试使用的最小日志对象。"""

    def info(self, *_args: object, **_kwargs: object) -> None:
        pass

    def warning(self, *_args: object, **_kwargs: object) -> None:
        pass


class MediaCacheServiceTests(IsolatedAsyncioTestCase):
    """验证媒体会落盘并可通过带 Range 的临时地址读取。"""

    async def test_cache_http_range_and_cleanup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            service = MediaCacheService(
                data_dir=Path(temp_dir),
                bind_host="127.0.0.1",
                port=0,
                public_base_url="http://127.0.0.1:0",
                logger=_TestLogger(),
            )
            service.initialize()

            async def downloader(source_url: str, target_path: Path, max_bytes: int) -> tuple[int, str]:
                self.assertEqual(source_url, "https://example.com/video.mp4")
                self.assertEqual(max_bytes, 100)
                media_data = b"0123456789"
                target_path.write_bytes(media_data)
                return len(media_data), "video/mp4"

            try:
                cached_media = await service.cache_media(
                    "https://example.com/video.mp4",
                    "tweet_1_1.mp4",
                    100,
                    downloader,
                )
                parsed_url = urlsplit(cached_media.public_url)
                reader, writer = await asyncio.open_connection(parsed_url.hostname, parsed_url.port)
                writer.write(
                    (
                        f"GET {parsed_url.path} HTTP/1.1\r\n"
                        "Host: 127.0.0.1\r\n"
                        "Range: bytes=2-5\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                await writer.drain()
                response = await reader.read()
                writer.close()
                await writer.wait_closed()

                raw_headers, body = response.split(b"\r\n\r\n", maxsplit=1)
                self.assertIn(b"HTTP/1.1 206 Partial Content", raw_headers)
                self.assertIn(b"Content-Range: bytes 2-5/10", raw_headers)
                self.assertEqual(body, b"2345")

                removed_files, removed_bytes = await service.clear()
                self.assertEqual((removed_files, removed_bytes), (1, 10))
                self.assertFalse(cached_media.path.exists())
            finally:
                await service.stop()


class MediaCleanupScheduleTests(TestCase):
    """验证每日清理时间固定使用北京时间。"""

    def test_seconds_until_cleanup_uses_beijing_time(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")

        self.assertEqual(
            seconds_until_cleanup("02:00", datetime(2026, 8, 8, 1, 30, tzinfo=timezone)),
            30 * 60,
        )
        self.assertEqual(
            seconds_until_cleanup("02:00", datetime(2026, 8, 8, 2, 0, tzinfo=timezone)),
            24 * 60 * 60,
        )
