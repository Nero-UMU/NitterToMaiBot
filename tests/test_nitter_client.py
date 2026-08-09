"""Nitter RSS 与状态页解析测试。"""

from datetime import timezone
from unittest import TestCase

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

QUOTE_RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <item>
      <title>Update</title>
      <dc:creator>@example</dc:creator>
      <description><![CDATA[
        <p>Update</p>
        <blockquote>
          <b>Example (@example)</b>
          <p>Quoted post</p>
          <footer>
            - <cite><a href="http://118.25.44.48/example/status/654321#m">http://118.25.44.48/example/status/654321#m</a></cite>
          </footer>
        </blockquote>
      ]]></description>
      <pubDate>Sat, 08 Aug 2026 04:04:25 GMT</pubDate>
      <guid isPermaLink="false">777778</guid>
      <link>http://118.25.44.48/example/status/777778#m</link>
    </item>
  </channel>
</rss>
"""


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

    def test_parse_rss_rewrites_quoted_nitter_status_url(self) -> None:
        posts = self.client.parse_rss(QUOTE_RSS_SAMPLE, "example")

        self.assertEqual(len(posts), 1)
        self.assertIn(
            "https://x.com/example/status/654321",
            posts[0].text,
        )
        self.assertNotIn("118.25.44.48", posts[0].text)
        self.assertNotIn("#m", posts[0].text)

    def test_status_url_rewrite_keeps_adjacent_text(self) -> None:
        text = self.client._officialize_status_urls(
            "http://118.25.44.48/example/status/654321#m中文翻译：内容"
        )

        self.assertEqual(
            text,
            "https://x.com/example/status/654321中文翻译：内容",
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
