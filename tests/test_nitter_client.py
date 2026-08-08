"""Nitter RSS 与状态页解析测试。"""

from datetime import timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from plugins.NitterToMaiBot.nitter_client import NitterClient, _MainTweetMediaParser


RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <title>Example Name / @example</title>
    <item>
      <title>Hello &amp; Nitter</title>
      <dc:creator>@example</dc:creator>
      <description><![CDATA[
        <p>Hello <a href="https://example.com">Nitter</a></p>
        <img src="http://public.example/pic/media%2Fphoto.jpg" />
      ]]></description>
      <pubDate>Sat, 08 Aug 2026 04:04:25 GMT</pubDate>
      <guid isPermaLink="false">123456</guid>
      <link>http://public.example/example/status/123456#m</link>
    </item>
  </channel>
</rss>
"""

VIDEO_RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <item>
      <title>Video</title>
      <dc:creator>@example</dc:creator>
      <description><![CDATA[
        <p></p><a href="http://public.example/example/status/654321#m">
        <br>Video<br><img src="http://public.example/pic/video_thumb.jpg" />
        </a>
      ]]></description>
      <pubDate>Sat, 08 Aug 2026 04:04:25 GMT</pubDate>
      <guid isPermaLink="false">654321</guid>
      <link>http://public.example/example/status/654321#m</link>
    </item>
  </channel>
</rss>
"""

RETWEET_RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <item>
      <title>Retweeted text</title>
      <dc:creator>@original_author</dc:creator>
      <description><![CDATA[<p>Retweeted text</p>]]></description>
      <pubDate>Sat, 08 Aug 2026 04:04:25 GMT</pubDate>
      <guid isPermaLink="false">777777</guid>
      <link>http://public.example/original_author/status/777777#m</link>
    </item>
  </channel>
</rss>
"""


class _ResponseHeaders(dict[str, str]):
    """提供 urllib 响应头所需最小接口。"""

    def get_content_type(self) -> str:
        return "video/mp4"


class _StreamingResponse:
    """测试流式媒体下载使用的上下文响应。"""

    def __init__(self, media_data: bytes) -> None:
        self.headers = _ResponseHeaders({"Content-Length": str(len(media_data))})
        self._media_data = media_data

    def __enter__(self) -> "_StreamingResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self, size: int) -> bytes:
        chunk = self._media_data[:size]
        self._media_data = self._media_data[size:]
        return chunk


class NitterClientParsingTests(TestCase):
    """验证解析、URL 改写和主推文媒体作用域。"""

    def setUp(self) -> None:
        self.client = NitterClient("http://127.0.0.1:8080", 20)

    def test_parse_rss_localizes_nitter_urls(self) -> None:
        posts = self.client.parse_rss(RSS_SAMPLE, "example")

        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post.post_id, "123456")
        self.assertEqual(post.text, "Hello Nitter")
        self.assertEqual(post.url, "http://127.0.0.1:8080/example/status/123456")
        self.assertEqual(post.published_at.tzinfo, timezone.utc)
        self.assertEqual(post.media[0].url, "http://127.0.0.1:8080/pic/media%2Fphoto.jpg")

    def test_parse_profile_name_from_channel_title(self) -> None:
        self.assertEqual(
            self.client.parse_profile_name(RSS_SAMPLE, "example"),
            "Example Name",
        )

    def test_status_parser_excludes_reply_videos(self) -> None:
        parser = _MainTweetMediaParser()
        parser.feed(
            """
            <div id="m" class="main-tweet">
              <div><video><source src="https://video.twimg.com/main.mp4" type="video/mp4" /></video></div>
            </div>
            <div class="reply"><video><source src="https://video.twimg.com/reply.mp4" type="video/mp4" /></video></div>
            """
        )

        self.assertEqual(
            parser.media,
            [("https://video.twimg.com/main.mp4", "video", "video/mp4")],
        )

    def test_status_parser_extracts_main_post_and_excludes_quote_media(self) -> None:
        parser = _MainTweetMediaParser()
        parser.feed(
            """
            <div id="m" class="main-tweet">
              <a class="username">@example</a>
              <div class="tweet-content media-body">第一行<br>第二行</div>
              <a class="still-image" href="/pic/main.jpg"><img src="/pic/thumb.jpg"></a>
              <div class="quote">
                <div class="tweet-content">引用正文</div>
                <a class="still-image" href="/pic/quote.jpg"><img src="/pic/quote.jpg"></a>
              </div>
            </div>
            <div class="reply">
              <a class="still-image" href="/pic/reply.jpg"><img src="/pic/reply.jpg"></a>
            </div>
            """
        )

        self.assertEqual(parser.author, "@example")
        self.assertEqual(parser.text(), "第一行\n第二行")
        self.assertEqual(parser.media, [("/pic/main.jpg", "image", "")])

    def test_external_media_url_is_not_rewritten(self) -> None:
        self.assertEqual(
            self.client._localize_media_url("https://video.twimg.com/video.mp4"),
            "https://video.twimg.com/video.mp4",
        )

    def test_request_url_removes_browser_fragment(self) -> None:
        self.assertEqual(
            self.client._normalize_request_url(
                "http://127.0.0.1:8080/example/status/123456#m"
            ),
            "http://127.0.0.1:8080/example/status/123456",
        )

    def test_video_marker_is_detected_from_rss(self) -> None:
        posts = self.client.parse_rss(VIDEO_RSS_SAMPLE, "example")

        self.assertTrue(posts[0].has_video)

    def test_retweet_is_detected_when_feed_author_differs(self) -> None:
        posts = self.client.parse_rss(RETWEET_RSS_SAMPLE, "subscriber")

        self.assertTrue(posts[0].is_retweet)

    def test_media_can_stream_directly_to_file(self) -> None:
        media_data = b"video-content"
        with TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "video.part"
            with patch(
                "plugins.NitterToMaiBot.nitter_client.urlopen",
                return_value=_StreamingResponse(media_data),
            ):
                size, content_type = self.client._request_to_file(
                    "https://video.twimg.com/video.mp4",
                    target_path,
                    1024,
                )

            self.assertEqual(size, len(media_data))
            self.assertEqual(content_type, "video/mp4")
            self.assertEqual(target_path.read_bytes(), media_data)
