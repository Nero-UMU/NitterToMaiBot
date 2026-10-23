"""媒体缓存的范围、落盘和恢复校验。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from plugins.NitterToMaiBot.media_cache import MediaCache
from plugins.NitterToMaiBot.models import CachedMedia, StagedPost
from plugins.NitterToMaiBot.nitter_client import MediaTooLargeError
from plugins.NitterToMaiBot.tests.test_subscription_forwarding import post


class MediaCacheTests(TestCase):
    def test_atomic_cache_reuse_and_post_cleanup_are_scoped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = MediaCache(Path(temp_dir) / "media_cache")
            first = cache.file(post(1), 1, "image")
            second = cache.file(post(2), 1, "image")
            cache.write(first, b"first")
            cache.write(second, b"second")
            self.assertTrue(cache.existing(first, 10))
            with self.assertRaises(MediaTooLargeError):
                cache.existing(first, 1)
            cache.remove(post(1))
            self.assertFalse(cache.path(first).exists())
            self.assertEqual(cache.path(second).read_bytes(), b"second")

    def test_traversal_absolute_and_foreign_post_paths_are_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = MediaCache(Path(temp_dir) / "media_cache")
            for value in ["../outside.jpg", "/tmp/outside.jpg", "C:/outside.jpg", "a/1.jpg"]:
                with self.assertRaises(ValueError):
                    cache.path(CachedMedia(value, "image"))
            foreign = cache.file(post(2), 1, "image")
            cache.write(foreign, b"image")
            with self.assertRaises(ValueError):
                cache.nodes(post(1), StagedPost([foreign]), "123456", "正文")

    def test_snapshot_nodes_reference_local_files_not_message_ids_or_remote_urls(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = MediaCache(Path(temp_dir) / "media_cache")
            image, video = cache.file(post(1), 1, "image"), cache.file(post(1), 2, "video")
            cache.write(image, b"image")
            cache.write(video, b"video")
            nodes = cache.nodes(post(1), StagedPost([image, video]), "123456", "正文")
            self.assertEqual(len(nodes), 2)
            self.assertTrue(all("id" not in node["data"] for node in nodes))
            for node in nodes:
                for segment in node["data"]["content"]:
                    if segment["type"] != "text":
                        self.assertTrue(Path(segment["data"]["file"]).is_file())
