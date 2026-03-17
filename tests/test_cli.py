import unittest

from website_auditor.cli import build_issues_report, detect_soft_404, extract_page_data, normalize_url


class NormalizeUrlTests(unittest.TestCase):
    def test_normalize_url_strips_fragment_and_adds_root_path(self) -> None:
        self.assertEqual(
            normalize_url("https://example.com#fragment"),
            "https://example.com/",
        )

    def test_normalize_url_rejects_non_http_urls(self) -> None:
        self.assertIsNone(normalize_url("mailto:test@example.com"))


class ExtractPageDataTests(unittest.TestCase):
    def test_extract_page_data_collects_seo_and_asset_flags(self) -> None:
        html = """
        <html lang="en">
          <head>
            <title>Short title</title>
            <meta name="description" content="Short description">
            <meta property="og:title" content="OG title">
            <link rel="canonical" href="https://external.example/page">
            <link rel="alternate" hreflang="en" href="https://example.com/en/page">
          </head>
          <body>
            <h1>Heading</h1>
            <img src="/image-one.jpg">
            <img src="http://cdn.example.com/image-two.jpg" alt="">
            <script src="http://cdn.example.com/app.js"></script>
            <a href="/about">About</a>
          </body>
        </html>
        """

        result = extract_page_data(html, "https://example.com/page", "example.com")

        self.assertEqual(result["title"], "Short title")
        self.assertTrue(result["title_too_short"])
        self.assertTrue(result["meta_description_too_short"])
        self.assertEqual(result["html_lang"], "en")
        self.assertEqual(result["hreflang_count"], 1)
        self.assertTrue(result["og_title_present"])
        self.assertFalse(result["twitter_card_present"])
        self.assertTrue(result["external_canonical"])
        self.assertTrue(result["mixed_content_present"])
        self.assertEqual(result["mixed_content_count"], 2)
        self.assertEqual(result["images_missing_alt"], 1)
        self.assertEqual(result["images_empty_alt"], 1)
        self.assertIn("https://example.com/about", result["internal_links"])


class Soft404Tests(unittest.TestCase):
    def test_detect_soft_404_by_common_text_patterns(self) -> None:
        self.assertTrue(detect_soft_404("Page Not Found", "This page does not exist anymore"))
        self.assertFalse(detect_soft_404("Welcome", "This is a normal landing page"))


class IssuesReportTests(unittest.TestCase):
    def test_build_issues_report_includes_page_and_site_issues(self) -> None:
        pages_report = [
            {
                "final_url": "https://example.com/page",
                "missing_title": False,
                "missing_meta_description": True,
                "missing_h1": True,
                "missing_lang": True,
                "title_too_short": True,
                "title_too_long": False,
                "title_length": 12,
                "meta_description_too_short": False,
                "meta_description_too_long": False,
                "meta_description_length": 0,
                "duplicate_title": False,
                "duplicate_meta_description": False,
                "title": "Short title",
                "meta_description": "",
                "og_title_present": False,
                "og_description_present": False,
                "og_image_present": False,
                "twitter_card_present": False,
                "canonical_mismatch": True,
                "canonical_url": "https://example.com/other",
                "external_canonical": True,
                "long_redirect_chain": True,
                "redirect_count": 3,
                "soft_404_suspected": True,
                "no_incoming_internal_links": True,
                "mixed_content_present": True,
                "mixed_content_count": 2,
                "noindex": False,
                "nofollow": False,
            }
        ]
        broken_links_report = [
            {
                "source_page": "https://example.com/page",
                "broken_link": "https://example.com/bad",
                "status": 404,
            }
        ]
        site_report = {
            "site_root": "https://example.com",
            "robots_present": False,
            "robots_status": 404,
            "sitemap_present": False,
            "sitemap_status": 404,
        }
        orphan_pages_report = [
            {"sitemap_url": "https://example.com/orphan", "in_sitemap": True, "crawled": False}
        ]
        image_issues_report = [
            {
                "source_page": "https://example.com/page",
                "image_url": "https://example.com/image.jpg",
                "issue_type": "missing_alt",
                "alt_text": "",
            }
        ]

        issues = build_issues_report(
            pages_report,
            broken_links_report,
            site_report,
            orphan_pages_report,
            image_issues_report,
        )
        issue_types = {issue["issue_type"] for issue in issues}

        self.assertIn("missing_robots_txt", issue_types)
        self.assertIn("missing_sitemap", issue_types)
        self.assertIn("missing_meta_description", issue_types)
        self.assertIn("missing_h1", issue_types)
        self.assertIn("missing_lang", issue_types)
        self.assertIn("short_title", issue_types)
        self.assertIn("missing_og_title", issue_types)
        self.assertIn("canonical_mismatch", issue_types)
        self.assertIn("external_canonical", issue_types)
        self.assertIn("long_redirect_chain", issue_types)
        self.assertIn("soft_404_suspected", issue_types)
        self.assertIn("no_incoming_internal_links", issue_types)
        self.assertIn("mixed_content", issue_types)
        self.assertIn("broken_link", issue_types)
        self.assertIn("missing_alt", issue_types)
        self.assertIn("orphan_page", issue_types)


if __name__ == "__main__":
    unittest.main()
