"""可选的本地 Nitter 联通测试。"""

from unittest import IsolatedAsyncioTestCase, skipUnless

import os

from plugins.NitterToMaiBot.nitter_client import NitterClient, NitterClientError


LIVE_URL = os.environ.get("NITTER_LIVE_TEST_URL", "")
LIVE_ACCOUNT = os.environ.get("NITTER_LIVE_TEST_ACCOUNT", "elonmusk")


@skipUnless(LIVE_URL, "未设置 NITTER_LIVE_TEST_URL")
class LiveNitterTests(IsolatedAsyncioTestCase):
    """访问开发者显式指定的 Nitter 实例。"""

    async def test_fetch_timeline(self) -> None:
        client = NitterClient(LIVE_URL, 20)
        posts = await client.fetch_timeline(LIVE_ACCOUNT)

        self.assertGreater(len(posts), 0)
        self.assertTrue(posts[0].post_id)

    async def test_fetch_video_status_media(self) -> None:
        client = NitterClient(LIVE_URL, 20)
        posts = await client.fetch_timeline(LIVE_ACCOUNT)
        video_post = next((post for post in posts if post.has_video), None)
        if video_post is None:
            self.skipTest("当前 RSS 中没有视频推文")

        try:
            enriched_post = await client.enrich_status_media(video_post)
        except NitterClientError as exc:
            self.skipTest(f"Nitter 状态页当前不可用: {exc}")
        self.assertTrue(any(media.media_type == "video" for media in enriched_post.media))
