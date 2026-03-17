import argparse
import csv
import html
import time
import warnings
import xml.etree.ElementTree as ET
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry


REQUEST_TIMEOUT = 10
PAGES_REPORT_FILE = "pages_report.csv"
BROKEN_LINKS_REPORT_FILE = "broken_links_report.csv"
SITE_REPORT_FILE = "site_report.csv"
SUMMARY_REPORT_FILE = "summary_report.csv"
ORPHAN_PAGES_REPORT_FILE = "orphan_pages_report.csv"
IMAGE_ISSUES_REPORT_FILE = "image_issues_report.csv"
HTML_REPORT_FILE = "audit_report.html"
ISSUES_REPORT_FILE = "issues_report.csv"
REQUEST_FAILED_STATUS = "REQUEST_FAILED"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Website Auditor: базовый аудит страниц сайта с сохранением отчётов в CSV."
    )
    parser.add_argument("start_url", help="Стартовый URL для обхода сайта")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Максимальное количество страниц для обхода (по умолчанию: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT,
        help=f"Таймаут HTTP-запроса в секундах (по умолчанию: {REQUEST_TIMEOUT})",
    )
    parser.add_argument(
        "--allow-insecure",
        action="store_true",
        help="Отключить SSL-проверку сертификата, если сайт отвечает с ошибками TLS.",
    )
    return parser.parse_args()


def normalize_url(url: str) -> Optional[str]:
    cleaned_url, _ = urldefrag(url.strip())
    parsed = urlparse(cleaned_url)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    path = parsed.path or "/"
    return parsed._replace(path=path, fragment="").geturl()


def is_internal_link(url: str, domain: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname == domain


def create_session(allow_insecure: bool) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Website Auditor/1.1"})

    retry_strategy = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = not allow_insecure

    if allow_insecure:
        warnings.simplefilter("ignore", InsecureRequestWarning)

    return session


def send_request(
    session: requests.Session,
    method: str,
    url: str,
    timeout: int,
) -> Tuple[Optional[requests.Response], Optional[str], int]:
    start = time.perf_counter()

    try:
        response = session.request(method=method, url=url, timeout=timeout, allow_redirects=True)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return response, None, elapsed_ms
    except requests.RequestException as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return None, str(exc), elapsed_ms


def build_absolute_asset_url(base_url: str, asset_url: str) -> str:
    if not asset_url:
        return ""

    if asset_url.startswith(("data:", "javascript:")):
        return asset_url

    absolute_url = urljoin(base_url, asset_url)
    return normalize_url(absolute_url) or absolute_url


def get_meta_content(soup: BeautifulSoup, attr_name: str, attr_value: str) -> str:
    tag = soup.find(
        "meta",
        attrs={attr_name: lambda value: isinstance(value, str) and value.lower() == attr_value.lower()},
    )
    if not tag:
        return ""
    return tag.get("content", "").strip()


def extract_page_data(html_content: str, base_url: str, domain: str) -> Dict[str, object]:
    soup = BeautifulSoup(html_content, "html.parser")
    parsed_base_url = urlparse(base_url)
    page_scheme = parsed_base_url.scheme

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    title_length = len(title)

    meta_description = get_meta_content(soup, "name", "description")
    meta_description_length = len(meta_description)

    robots_directives = get_meta_content(soup, "name", "robots").lower()
    og_title = get_meta_content(soup, "property", "og:title")
    og_description = get_meta_content(soup, "property", "og:description")
    og_image = get_meta_content(soup, "property", "og:image")
    twitter_card = get_meta_content(soup, "name", "twitter:card")

    canonical_tag = soup.find(
        "link",
        rel=lambda value: isinstance(value, str) and "canonical" in value.lower(),
    )
    canonical_url = ""
    if canonical_tag and canonical_tag.get("href"):
        canonical_candidate = urljoin(base_url, canonical_tag["href"])
        canonical_url = normalize_url(canonical_candidate) or canonical_candidate

    html_tag = soup.find("html")
    html_lang = ""
    if html_tag:
        html_lang = html_tag.get("lang", "").strip()

    h1_tags = soup.find_all("h1")
    hreflang_tags = [
        tag for tag in soup.find_all("link", href=True)
        if tag.get("hreflang")
    ]
    internal_links: Set[str] = set()
    image_issues: List[Dict[str, object]] = []
    mixed_content_resources: List[str] = []
    images = soup.find_all("img")

    for image in images:
        src = image.get("src", "").strip()
        alt_value = image.get("alt")
        normalized_image_url = build_absolute_asset_url(base_url, src)
        if page_scheme == "https" and normalized_image_url.startswith("http://"):
            mixed_content_resources.append(normalized_image_url)

        if alt_value is None:
            image_issues.append(
                {
                    "image_url": normalized_image_url,
                    "issue_type": "missing_alt",
                    "alt_text": "",
                }
            )
            continue

        if not alt_value.strip():
            image_issues.append(
                {
                    "image_url": normalized_image_url,
                    "issue_type": "empty_alt",
                    "alt_text": alt_value,
                }
            )

    for tag_name, attr_name in [("script", "src"), ("link", "href"), ("a", "href")]:
        for tag in soup.find_all(tag_name):
            asset_url = tag.get(attr_name, "").strip()
            normalized_asset_url = build_absolute_asset_url(base_url, asset_url)
            if page_scheme == "https" and normalized_asset_url.startswith("http://"):
                mixed_content_resources.append(normalized_asset_url)

    canonical_domain = urlparse(canonical_url).hostname if canonical_url else ""
    external_canonical = bool(canonical_domain) and canonical_domain != domain

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue

        absolute_url = urljoin(base_url, href)
        normalized_url = normalize_url(absolute_url)
        if normalized_url and is_internal_link(normalized_url, domain):
            internal_links.add(normalized_url)

    return {
        "title": title,
        "title_length": title_length,
        "meta_description": meta_description,
        "meta_description_length": meta_description_length,
        "robots_directives": robots_directives,
        "noindex": "noindex" in robots_directives,
        "nofollow": "nofollow" in robots_directives,
        "html_lang": html_lang,
        "missing_lang": not bool(html_lang),
        "hreflang_count": len(hreflang_tags),
        "og_title_present": bool(og_title),
        "og_description_present": bool(og_description),
        "og_image_present": bool(og_image),
        "twitter_card_present": bool(twitter_card),
        "title_too_short": bool(title) and title_length < 30,
        "title_too_long": title_length > 60,
        "meta_description_too_short": bool(meta_description) and meta_description_length < 70,
        "meta_description_too_long": meta_description_length > 160,
        "canonical_url": canonical_url,
        "external_canonical": external_canonical,
        "mixed_content_present": bool(mixed_content_resources),
        "mixed_content_count": len(mixed_content_resources),
        "h1_count": len(h1_tags),
        "images_count": len(images),
        "images_missing_alt": sum(1 for issue in image_issues if issue["issue_type"] == "missing_alt"),
        "images_empty_alt": sum(1 for issue in image_issues if issue["issue_type"] == "empty_alt"),
        "image_issues": image_issues,
        "mixed_content_resources": mixed_content_resources,
        "internal_links": internal_links,
        "missing_title": not bool(title),
        "missing_meta_description": not bool(meta_description),
        "missing_h1": len(h1_tags) == 0,
    }


def get_link_status(
    session: requests.Session,
    url: str,
    timeout: int,
    status_cache: Dict[str, Tuple[Optional[int], bool]],
) -> Tuple[Optional[int], bool]:
    if url in status_cache:
        return status_cache[url]

    response, error, _ = send_request(session, "HEAD", url, timeout)

    if error or (response is not None and response.status_code in {403, 405}):
        response, error, _ = send_request(session, "GET", url, timeout)

    if error:
        status_cache[url] = (None, True)
        return status_cache[url]

    status_cache[url] = (response.status_code, response.status_code >= 400)
    return status_cache[url]


def inspect_resource(
    session: requests.Session,
    url: str,
    timeout: int,
) -> Tuple[object, bool, str]:
    response, error, _ = send_request(session, "GET", url, timeout)
    if error:
        return REQUEST_FAILED_STATUS, False, ""

    is_present = response.status_code < 400
    return response.status_code, is_present, response.text if is_present else ""


def inspect_site_resources(
    session: requests.Session,
    start_url: str,
    timeout: int,
) -> Dict[str, object]:
    parsed_start = urlparse(start_url)
    site_root = f"{parsed_start.scheme}://{parsed_start.netloc}"
    robots_url = f"{site_root}/robots.txt"
    default_sitemap_url = f"{site_root}/sitemap.xml"

    robots_status, robots_present, robots_content = inspect_resource(session, robots_url, timeout)

    sitemap_url = default_sitemap_url
    if robots_present:
        for line in robots_content.splitlines():
            if line.lower().startswith("sitemap:"):
                candidate = line.split(":", 1)[1].strip()
                normalized_candidate = normalize_url(candidate)
                if normalized_candidate:
                    sitemap_url = normalized_candidate
                    break

    sitemap_status, sitemap_present, sitemap_content = inspect_resource(session, sitemap_url, timeout)
    sitemap_urls = extract_sitemap_urls(
        session=session,
        sitemap_url=sitemap_url,
        sitemap_content=sitemap_content,
        timeout=timeout,
        visited_sitemaps=set(),
    ) if sitemap_present else set()

    return {
        "site_root": site_root,
        "robots_url": robots_url,
        "robots_status": robots_status,
        "robots_present": robots_present,
        "sitemap_url": sitemap_url,
        "sitemap_status": sitemap_status,
        "sitemap_present": sitemap_present,
        "sitemap_urls": sitemap_urls,
        "sitemap_urls_count": len(sitemap_urls),
    }


def extract_sitemap_urls(
    session: requests.Session,
    sitemap_url: str,
    sitemap_content: str,
    timeout: int,
    visited_sitemaps: Set[str],
) -> Set[str]:
    normalized_sitemap_url = normalize_url(sitemap_url) or sitemap_url
    if normalized_sitemap_url in visited_sitemaps:
        return set()

    visited_sitemaps.add(normalized_sitemap_url)

    try:
        root = ET.fromstring(sitemap_content)
    except ET.ParseError:
        return set()

    urls: Set[str] = set()
    root_tag = root.tag.lower()

    if root_tag.endswith("urlset"):
        for element in root.iter():
            if element.tag.lower().endswith("loc") and element.text:
                normalized_url = normalize_url(element.text)
                if normalized_url:
                    urls.add(normalized_url)
        return urls

    if root_tag.endswith("sitemapindex"):
        for element in root.iter():
            if not element.tag.lower().endswith("loc") or not element.text:
                continue

            nested_sitemap_url = normalize_url(element.text)
            if not nested_sitemap_url:
                continue

            _, is_present, nested_content = inspect_resource(session, nested_sitemap_url, timeout)
            if not is_present:
                continue

            urls.update(
                extract_sitemap_urls(
                    session=session,
                    sitemap_url=nested_sitemap_url,
                    sitemap_content=nested_content,
                    timeout=timeout,
                    visited_sitemaps=visited_sitemaps,
                )
            )

    return urls


def annotate_duplicate_titles(pages_report: List[Dict[str, object]]) -> None:
    title_counts = Counter(
        str(page["title"]).strip().lower()
        for page in pages_report
        if str(page["title"]).strip()
    )
    for page in pages_report:
        normalized_title = str(page["title"]).strip().lower()
        page["duplicate_title"] = bool(normalized_title) and title_counts[normalized_title] > 1


def annotate_duplicate_meta_descriptions(pages_report: List[Dict[str, object]]) -> None:
    description_counts = Counter(
        str(page["meta_description"]).strip().lower()
        for page in pages_report
        if str(page["meta_description"]).strip()
    )
    for page in pages_report:
        normalized_description = str(page["meta_description"]).strip().lower()
        page["duplicate_meta_description"] = (
            bool(normalized_description) and description_counts[normalized_description] > 1
        )


def build_summary_report(
    pages_report: List[Dict[str, object]],
    broken_links_report: List[Dict[str, object]],
    site_report: Dict[str, object],
    orphan_pages_report: List[Dict[str, object]],
    image_issues_report: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    return [
        {"metric": "pages_crawled", "value": len(pages_report)},
        {"metric": "broken_links_found", "value": len(broken_links_report)},
        {"metric": "pages_missing_title", "value": sum(1 for page in pages_report if page["missing_title"])},
        {
            "metric": "pages_missing_meta_description",
            "value": sum(1 for page in pages_report if page["missing_meta_description"]),
        },
        {"metric": "pages_missing_h1", "value": sum(1 for page in pages_report if page["missing_h1"])},
        {"metric": "pages_missing_lang", "value": sum(1 for page in pages_report if page["missing_lang"])},
        {"metric": "pages_with_short_title", "value": sum(1 for page in pages_report if page["title_too_short"])},
        {"metric": "pages_with_long_title", "value": sum(1 for page in pages_report if page["title_too_long"])},
        {
            "metric": "pages_with_short_meta_description",
            "value": sum(1 for page in pages_report if page["meta_description_too_short"]),
        },
        {
            "metric": "pages_with_long_meta_description",
            "value": sum(1 for page in pages_report if page["meta_description_too_long"]),
        },
        {"metric": "pages_with_duplicate_title", "value": sum(1 for page in pages_report if page["duplicate_title"])},
        {
            "metric": "pages_with_duplicate_meta_description",
            "value": sum(1 for page in pages_report if page["duplicate_meta_description"]),
        },
        {"metric": "pages_missing_og_title", "value": sum(1 for page in pages_report if not page["og_title_present"])},
        {
            "metric": "pages_missing_og_description",
            "value": sum(1 for page in pages_report if not page["og_description_present"]),
        },
        {"metric": "pages_missing_og_image", "value": sum(1 for page in pages_report if not page["og_image_present"])},
        {
            "metric": "pages_missing_twitter_card",
            "value": sum(1 for page in pages_report if not page["twitter_card_present"]),
        },
        {"metric": "pages_with_hreflang", "value": sum(1 for page in pages_report if page["hreflang_count"] > 0)},
        {"metric": "pages_with_noindex", "value": sum(1 for page in pages_report if page["noindex"])},
        {"metric": "pages_with_nofollow", "value": sum(1 for page in pages_report if page["nofollow"])},
        {"metric": "pages_with_redirects", "value": sum(1 for page in pages_report if page["redirect_count"] > 0)},
        {"metric": "pages_with_external_canonical", "value": sum(1 for page in pages_report if page["external_canonical"])},
        {"metric": "pages_with_mixed_content", "value": sum(1 for page in pages_report if page["mixed_content_present"])},
        {"metric": "mixed_content_resources_found", "value": sum(int(page["mixed_content_count"]) for page in pages_report)},
        {"metric": "images_found", "value": sum(int(page["images_count"]) for page in pages_report)},
        {"metric": "images_missing_alt", "value": sum(int(page["images_missing_alt"]) for page in pages_report)},
        {"metric": "images_empty_alt", "value": sum(int(page["images_empty_alt"]) for page in pages_report)},
        {
            "metric": "pages_with_image_alt_issues",
            "value": sum(
                1
                for page in pages_report
                if int(page["images_missing_alt"]) > 0 or int(page["images_empty_alt"]) > 0
            ),
        },
        {"metric": "image_issues_found", "value": len(image_issues_report)},
        {
            "metric": "pages_with_canonical_mismatch",
            "value": sum(1 for page in pages_report if page["canonical_mismatch"]),
        },
        {"metric": "robots_txt_present", "value": site_report["robots_present"]},
        {"metric": "sitemap_present", "value": site_report["sitemap_present"]},
        {"metric": "sitemap_urls_total", "value": site_report["sitemap_urls_count"]},
        {"metric": "orphan_pages_found", "value": len(orphan_pages_report)},
    ]


def build_issues_report(
    pages_report: List[Dict[str, object]],
    broken_links_report: List[Dict[str, object]],
    site_report: Dict[str, object],
    orphan_pages_report: List[Dict[str, object]],
    image_issues_report: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    issues_report: List[Dict[str, object]] = []

    def add_issue(scope: str, source: str, issue_type: str, severity: str, details: str) -> None:
        issues_report.append(
            {
                "scope": scope,
                "source": source,
                "issue_type": issue_type,
                "severity": severity,
                "details": details,
            }
        )

    if not site_report["robots_present"]:
        add_issue("site", site_report["site_root"], "missing_robots_txt", "medium", str(site_report["robots_status"]))
    if not site_report["sitemap_present"]:
        add_issue("site", site_report["site_root"], "missing_sitemap", "medium", str(site_report["sitemap_status"]))

    for page in pages_report:
        source = str(page["final_url"])
        if page["missing_title"]:
            add_issue("page", source, "missing_title", "high", "Title tag is empty or missing.")
        if page["missing_meta_description"]:
            add_issue("page", source, "missing_meta_description", "medium", "Meta description is empty or missing.")
        if page["missing_h1"]:
            add_issue("page", source, "missing_h1", "medium", "No H1 tag found.")
        if page["missing_lang"]:
            add_issue("page", source, "missing_lang", "medium", "HTML lang attribute is missing.")
        if page["title_too_short"]:
            add_issue("page", source, "short_title", "low", f"Title length: {page['title_length']}.")
        if page["title_too_long"]:
            add_issue("page", source, "long_title", "low", f"Title length: {page['title_length']}.")
        if page["meta_description_too_short"]:
            add_issue(
                "page",
                source,
                "short_meta_description",
                "low",
                f"Meta description length: {page['meta_description_length']}.",
            )
        if page["meta_description_too_long"]:
            add_issue(
                "page",
                source,
                "long_meta_description",
                "low",
                f"Meta description length: {page['meta_description_length']}.",
            )
        if page["duplicate_title"]:
            add_issue("page", source, "duplicate_title", "medium", str(page["title"]))
        if page["duplicate_meta_description"]:
            add_issue("page", source, "duplicate_meta_description", "medium", str(page["meta_description"]))
        if not page["og_title_present"]:
            add_issue("page", source, "missing_og_title", "low", "Open Graph title is missing.")
        if not page["og_description_present"]:
            add_issue("page", source, "missing_og_description", "low", "Open Graph description is missing.")
        if not page["og_image_present"]:
            add_issue("page", source, "missing_og_image", "low", "Open Graph image is missing.")
        if not page["twitter_card_present"]:
            add_issue("page", source, "missing_twitter_card", "low", "Twitter card is missing.")
        if page["canonical_mismatch"]:
            add_issue("page", source, "canonical_mismatch", "medium", str(page["canonical_url"]))
        if page["external_canonical"]:
            add_issue("page", source, "external_canonical", "medium", str(page["canonical_url"]))
        if page["mixed_content_present"]:
            add_issue(
                "page",
                source,
                "mixed_content",
                "high",
                f"HTTP resources found on HTTPS page: {page['mixed_content_count']}.",
            )

    for item in broken_links_report:
        add_issue("link", str(item["source_page"]), "broken_link", "high", f"{item['broken_link']} ({item['status']})")

    for item in image_issues_report:
        add_issue("image", str(item["source_page"]), str(item["issue_type"]), "medium", str(item["image_url"]))

    for item in orphan_pages_report:
        add_issue("sitemap", str(item["sitemap_url"]), "orphan_page", "low", "URL found in sitemap but not crawled.")

    return issues_report


def write_pages_report(pages: List[Dict[str, object]]) -> None:
    with open(PAGES_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "url",
                "final_url",
                "http_status",
                "response_time_ms",
                "redirect_count",
                "title",
                "title_length",
                "title_too_short",
                "title_too_long",
                "meta_description_length",
                "meta_description",
                "meta_description_too_short",
                "meta_description_too_long",
                "robots_directives",
                "noindex",
                "nofollow",
                "html_lang",
                "missing_lang",
                "hreflang_count",
                "og_title_present",
                "og_description_present",
                "og_image_present",
                "twitter_card_present",
                "canonical_url",
                "canonical_mismatch",
                "external_canonical",
                "mixed_content_present",
                "mixed_content_count",
                "h1_count",
                "images_count",
                "images_missing_alt",
                "images_empty_alt",
                "internal_links_count",
                "missing_title",
                "missing_meta_description",
                "missing_h1",
                "duplicate_title",
                "duplicate_meta_description",
            ]
        )
        for page in pages:
            writer.writerow(
                [
                    page["url"],
                    page["final_url"],
                    page["status"],
                    page["response_time_ms"],
                    page["redirect_count"],
                    page["title"],
                    page["title_length"],
                    page["title_too_short"],
                    page["title_too_long"],
                    page["meta_description_length"],
                    page["meta_description"],
                    page["meta_description_too_short"],
                    page["meta_description_too_long"],
                    page["robots_directives"],
                    page["noindex"],
                    page["nofollow"],
                    page["html_lang"],
                    page["missing_lang"],
                    page["hreflang_count"],
                    page["og_title_present"],
                    page["og_description_present"],
                    page["og_image_present"],
                    page["twitter_card_present"],
                    page["canonical_url"],
                    page["canonical_mismatch"],
                    page["external_canonical"],
                    page["mixed_content_present"],
                    page["mixed_content_count"],
                    page["h1_count"],
                    page["images_count"],
                    page["images_missing_alt"],
                    page["images_empty_alt"],
                    page["internal_links_count"],
                    page["missing_title"],
                    page["missing_meta_description"],
                    page["missing_h1"],
                    page["duplicate_title"],
                    page["duplicate_meta_description"],
                ]
            )


def write_broken_links_report(broken_links: List[Dict[str, object]]) -> None:
    with open(BROKEN_LINKS_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["source_page", "broken_link", "status"])
        for item in broken_links:
            writer.writerow([item["source_page"], item["broken_link"], item["status"]])


def write_site_report(site_report: Dict[str, object]) -> None:
    with open(SITE_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "site_root",
                "robots_url",
                "robots_status",
                "robots_present",
                "sitemap_url",
                "sitemap_status",
                "sitemap_present",
                "sitemap_urls_count",
            ]
        )
        writer.writerow(
            [
                site_report["site_root"],
                site_report["robots_url"],
                site_report["robots_status"],
                site_report["robots_present"],
                site_report["sitemap_url"],
                site_report["sitemap_status"],
                site_report["sitemap_present"],
                site_report["sitemap_urls_count"],
            ]
        )


def write_summary_report(summary_report: List[Dict[str, object]]) -> None:
    with open(SUMMARY_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["metric", "value"])
        for item in summary_report:
            writer.writerow([item["metric"], item["value"]])


def write_orphan_pages_report(orphan_pages_report: List[Dict[str, object]]) -> None:
    with open(ORPHAN_PAGES_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["sitemap_url", "in_sitemap", "crawled"])
        for item in orphan_pages_report:
            writer.writerow([item["sitemap_url"], item["in_sitemap"], item["crawled"]])


def write_image_issues_report(image_issues_report: List[Dict[str, object]]) -> None:
    with open(IMAGE_ISSUES_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["source_page", "image_url", "issue_type", "alt_text"])
        for item in image_issues_report:
            writer.writerow(
                [
                    item["source_page"],
                    item["image_url"],
                    item["issue_type"],
                    item["alt_text"],
                ]
            )


def write_issues_report(issues_report: List[Dict[str, object]]) -> None:
    with open(ISSUES_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["scope", "source", "issue_type", "severity", "details"])
        for item in issues_report:
            writer.writerow(
                [
                    item["scope"],
                    item["source"],
                    item["issue_type"],
                    item["severity"],
                    item["details"],
                ]
            )


def render_table(headers: List[str], rows: List[List[object]]) -> str:
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")

    body_html = "".join(body_rows) if body_rows else f"<tr><td colspan=\"{len(headers)}\">No data</td></tr>"
    return (
        "<div class=\"table-wrap\">"
        "<table>"
        f"<thead><tr>{header_html}</tr></thead>"
        f"<tbody>{body_html}</tbody>"
        "</table>"
        "</div>"
    )


def write_html_report(
    start_url: str,
    summary_report: List[Dict[str, object]],
    site_report: Dict[str, object],
    pages_report: List[Dict[str, object]],
    broken_links_report: List[Dict[str, object]],
    orphan_pages_report: List[Dict[str, object]],
    image_issues_report: List[Dict[str, object]],
    issues_report: List[Dict[str, object]],
) -> None:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary_cards = "".join(
        (
            "<div class=\"card\">"
            f"<div class=\"metric\">{html.escape(str(item['value']))}</div>"
            f"<div class=\"label\">{html.escape(str(item['metric']))}</div>"
            "</div>"
        )
        for item in summary_report
    )

    pages_rows = [
        [
            page["final_url"],
            page["status"],
            page["title"],
            page["html_lang"],
            page["hreflang_count"],
            page["og_title_present"],
            page["twitter_card_present"],
            page["mixed_content_present"],
            page["external_canonical"],
            page["images_count"],
            page["images_missing_alt"],
            page["images_empty_alt"],
            page["canonical_mismatch"],
        ]
        for page in pages_report
    ]
    broken_rows = [
        [item["source_page"], item["broken_link"], item["status"]]
        for item in broken_links_report
    ]
    image_rows = [
        [item["source_page"], item["image_url"], item["issue_type"], item["alt_text"]]
        for item in image_issues_report
    ]
    issues_rows = [
        [item["scope"], item["source"], item["issue_type"], item["severity"], item["details"]]
        for item in issues_report
    ]
    orphan_rows = [
        [item["sitemap_url"], item["in_sitemap"], item["crawled"]]
        for item in orphan_pages_report
    ]
    site_rows = [[
        site_report["site_root"],
        site_report["robots_status"],
        site_report["sitemap_status"],
        site_report["sitemap_urls_count"],
    ]]

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Website Auditor Report</title>
  <style>
    :root {{
      --bg: #f5efe5;
      --panel: #fffaf2;
      --ink: #1f1a16;
      --muted: #6c6258;
      --line: #d6c7b6;
      --accent: #b85c38;
      --accent-soft: #f0d7c7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #f9e3d2 0, transparent 28%),
        linear-gradient(160deg, #f6f0e6 0%, #ece2d3 100%);
    }}
    .page {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    .hero {{
      background: rgba(255, 250, 242, 0.88);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 28px;
      box-shadow: 0 18px 40px rgba(55, 35, 20, 0.08);
    }}
    h1, h2 {{
      margin: 0 0 12px;
      font-weight: 600;
    }}
    .subtitle {{
      color: var(--muted);
      margin: 0;
      line-height: 1.6;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin: 24px 0 0;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
    }}
    .metric {{
      font-size: 30px;
      font-weight: 700;
      color: var(--accent);
      line-height: 1.1;
    }}
    .label {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 14px;
      word-break: break-word;
    }}
    .section {{
      margin-top: 24px;
      background: rgba(255, 250, 242, 0.88);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 24px;
      box-shadow: 0 18px 40px rgba(55, 35, 20, 0.06);
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--panel);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 640px;
    }}
    th, td {{
      padding: 12px 14px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      background: var(--accent-soft);
      color: var(--ink);
      position: sticky;
      top: 0;
    }}
    tr:last-child td {{
      border-bottom: 0;
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 16px;
      color: var(--muted);
      font-size: 14px;
    }}
    @media (max-width: 720px) {{
      .page {{
        padding: 20px 14px 40px;
      }}
      .hero, .section {{
        padding: 18px;
        border-radius: 18px;
      }}
      .metric {{
        font-size: 24px;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <h1>Website Auditor Report</h1>
      <p class="subtitle">Audit results for {html.escape(start_url)}. This dashboard combines crawl metrics, SEO flags, broken links, sitemap coverage, and image accessibility issues.</p>
      <div class="meta">
        <span>Generated: {html.escape(generated_at)}</span>
        <span>Pages crawled: {html.escape(str(len(pages_report)))}</span>
        <span>Broken links: {html.escape(str(len(broken_links_report)))}</span>
        <span>Image issues: {html.escape(str(len(image_issues_report)))}</span>
        <span>Total issues: {html.escape(str(len(issues_report)))}</span>
      </div>
      <div class="grid">{summary_cards}</div>
    </section>
    <section class="section">
      <h2>Site Resources</h2>
      {render_table(["Site root", "robots.txt", "sitemap", "URLs in sitemap"], site_rows)}
    </section>
    <section class="section">
      <h2>Pages Overview</h2>
      {render_table(["Final URL", "HTTP", "Title", "Lang", "hreflang", "OG title", "Twitter card", "Mixed content", "External canonical", "Images", "Missing alt", "Empty alt", "Canonical mismatch"], pages_rows)}
    </section>
    <section class="section">
      <h2>Broken Links</h2>
      {render_table(["Source page", "Broken link", "Status"], broken_rows)}
    </section>
    <section class="section">
      <h2>Image Issues</h2>
      {render_table(["Source page", "Image URL", "Issue type", "Alt text"], image_rows)}
    </section>
    <section class="section">
      <h2>Unified Issues</h2>
      {render_table(["Scope", "Source", "Issue type", "Severity", "Details"], issues_rows)}
    </section>
    <section class="section">
      <h2>Orphan Pages From Sitemap</h2>
      {render_table(["Sitemap URL", "In sitemap", "Crawled"], orphan_rows)}
    </section>
  </main>
</body>
</html>
"""

    with open(HTML_REPORT_FILE, "w", encoding="utf-8") as file:
        file.write(html_content)


def append_failed_page(
    pages_report: List[Dict[str, object]],
    url: str,
    status: object,
    response_time_ms: int,
) -> None:
    pages_report.append(
        {
            "url": url,
            "final_url": url,
            "status": status,
            "response_time_ms": response_time_ms,
            "redirect_count": 0,
            "title": "",
            "title_length": 0,
            "title_too_short": False,
            "title_too_long": False,
            "meta_description_length": 0,
            "meta_description": "",
            "meta_description_too_short": False,
            "meta_description_too_long": False,
            "robots_directives": "",
            "noindex": False,
            "nofollow": False,
            "html_lang": "",
            "missing_lang": True,
            "hreflang_count": 0,
            "og_title_present": False,
            "og_description_present": False,
            "og_image_present": False,
            "twitter_card_present": False,
            "canonical_url": "",
            "canonical_mismatch": False,
            "external_canonical": False,
            "mixed_content_present": False,
            "mixed_content_count": 0,
            "h1_count": 0,
            "images_count": 0,
            "images_missing_alt": 0,
            "images_empty_alt": 0,
            "internal_links_count": 0,
            "missing_title": True,
            "missing_meta_description": True,
            "missing_h1": True,
            "duplicate_title": False,
            "duplicate_meta_description": False,
        }
    )


def print_summary(
    pages_report: List[Dict[str, object]],
    broken_links_report: List[Dict[str, object]],
    site_report: Dict[str, object],
    orphan_pages_report: List[Dict[str, object]],
    image_issues_report: List[Dict[str, object]],
) -> None:
    title_issues = sum(1 for page in pages_report if page["missing_title"])
    meta_issues = sum(1 for page in pages_report if page["missing_meta_description"])
    h1_issues = sum(1 for page in pages_report if page["missing_h1"])
    lang_issues = sum(1 for page in pages_report if page["missing_lang"])
    short_titles = sum(1 for page in pages_report if page["title_too_short"])
    long_titles = sum(1 for page in pages_report if page["title_too_long"])
    short_meta = sum(1 for page in pages_report if page["meta_description_too_short"])
    long_meta = sum(1 for page in pages_report if page["meta_description_too_long"])
    duplicate_titles = sum(1 for page in pages_report if page["duplicate_title"])
    duplicate_meta_descriptions = sum(1 for page in pages_report if page["duplicate_meta_description"])
    missing_og_title = sum(1 for page in pages_report if not page["og_title_present"])
    missing_og_description = sum(1 for page in pages_report if not page["og_description_present"])
    missing_og_image = sum(1 for page in pages_report if not page["og_image_present"])
    missing_twitter_card = sum(1 for page in pages_report if not page["twitter_card_present"])
    noindex_pages = sum(1 for page in pages_report if page["noindex"])
    nofollow_pages = sum(1 for page in pages_report if page["nofollow"])
    canonical_mismatches = sum(1 for page in pages_report if page["canonical_mismatch"])
    external_canonicals = sum(1 for page in pages_report if page["external_canonical"])
    mixed_content_pages = sum(1 for page in pages_report if page["mixed_content_present"])
    mixed_content_resources = sum(int(page["mixed_content_count"]) for page in pages_report)
    image_issues = len(image_issues_report)
    images_missing_alt = sum(int(page["images_missing_alt"]) for page in pages_report)
    images_empty_alt = sum(int(page["images_empty_alt"]) for page in pages_report)
    redirects = sum(1 for page in pages_report if page["redirect_count"] > 0)
    status_counter = Counter(str(item["status"]) for item in broken_links_report)

    print(
        f"[SUMMARY] Без title: {title_issues}, без meta description: {meta_issues}, "
        f"без H1: {h1_issues}, без lang: {lang_issues}, с дубликатом title: {duplicate_titles}, "
        f"с дубликатом meta description: {duplicate_meta_descriptions}"
    )
    print(
        f"[SUMMARY] Короткий title: {short_titles}, длинный title: {long_titles}, "
        f"короткий meta description: {short_meta}, длинный meta description: {long_meta}"
    )
    print(
        f"[SUMMARY] Без og:title: {missing_og_title}, без og:description: {missing_og_description}, "
        f"без og:image: {missing_og_image}, без twitter:card: {missing_twitter_card}"
    )
    print(
        f"[SUMMARY] Страниц с noindex: {noindex_pages}, с nofollow: {nofollow_pages}, "
        f"с canonical mismatch: {canonical_mismatches}, с external canonical: {external_canonicals}, "
        f"страниц с редиректами: {redirects}"
    )
    print(
        f"[SUMMARY] Проблем mixed content: страниц {mixed_content_pages}, ресурсов {mixed_content_resources}. "
        f"Проблем изображений: {image_issues}, missing alt: {images_missing_alt}, empty alt: {images_empty_alt}"
    )
    print(
        f"[SUMMARY] robots.txt: {site_report['robots_status']} "
        f"({ 'найден' if site_report['robots_present'] else 'не найден' }), "
        f"sitemap: {site_report['sitemap_status']} "
        f"({ 'найден' if site_report['sitemap_present'] else 'не найден' }), "
        f"URL в sitemap: {site_report['sitemap_urls_count']}, orphan pages: {len(orphan_pages_report)}"
    )
    if status_counter:
        formatted = ", ".join(f"{status}: {count}" for status, count in sorted(status_counter.items()))
        print(f"[SUMMARY] Битые ссылки по статусам: {formatted}")


def audit_website(
    start_url: str,
    max_pages: int,
    timeout: int,
    allow_insecure: bool,
) -> Tuple[
    List[Dict[str, object]],
    List[Dict[str, object]],
    Dict[str, object],
    List[Dict[str, object]],
    List[Dict[str, object]],
]:
    normalized_start_url = normalize_url(start_url)
    if not normalized_start_url:
        raise ValueError("Стартовый URL должен быть абсолютным HTTP(S)-адресом.")

    if max_pages <= 0:
        raise ValueError("--max-pages должен быть положительным числом.")

    if timeout <= 0:
        raise ValueError("--timeout должен быть положительным числом.")

    domain = urlparse(normalized_start_url).hostname
    if not domain:
        raise ValueError("Не удалось определить домен стартового URL.")

    pages_report: List[Dict[str, object]] = []
    broken_links_report: List[Dict[str, object]] = []
    image_issues_report: List[Dict[str, object]] = []
    queued_urls = deque([normalized_start_url])
    visited_urls: Set[str] = set()
    discovered_urls: Set[str] = {normalized_start_url}
    status_cache: Dict[str, Tuple[Optional[int], bool]] = {}

    with create_session(allow_insecure) as session:
        site_report = inspect_site_resources(session, normalized_start_url, timeout)

        while queued_urls and len(visited_urls) < max_pages:
            current_url = queued_urls.popleft()
            if current_url in visited_urls:
                continue

            print(f"[INFO] Проверка страницы: {current_url}")
            response, error, elapsed_ms = send_request(session, "GET", current_url, timeout)
            visited_urls.add(current_url)

            if error:
                print(f"[WARN] Ошибка запроса: {error}")
                status_cache[current_url] = (None, True)
                append_failed_page(pages_report, current_url, REQUEST_FAILED_STATUS, elapsed_ms)
                continue

            status_cache[current_url] = (response.status_code, response.status_code >= 400)

            if response.status_code >= 400:
                print(f"[WARN] Страница вернула HTTP {response.status_code}")
                append_failed_page(pages_report, current_url, response.status_code, elapsed_ms)
                continue

            content_type = response.headers.get("Content-Type", "")
            final_url = normalize_url(response.url) or response.url
            redirect_count = len(response.history)
            if "html" not in content_type.lower():
                print("[INFO] Пропуск разбора: контент не является HTML")
                pages_report.append(
                    {
                        "url": current_url,
                        "final_url": final_url,
                        "status": response.status_code,
                        "response_time_ms": elapsed_ms,
                        "redirect_count": redirect_count,
                        "title": "",
                        "title_length": 0,
                        "title_too_short": False,
                        "title_too_long": False,
                        "meta_description_length": 0,
                        "meta_description": "",
                        "meta_description_too_short": False,
                        "meta_description_too_long": False,
                        "robots_directives": "",
                        "noindex": False,
                        "nofollow": False,
                        "html_lang": "",
                        "missing_lang": True,
                        "hreflang_count": 0,
                        "og_title_present": False,
                        "og_description_present": False,
                        "og_image_present": False,
                        "twitter_card_present": False,
                        "canonical_url": "",
                        "canonical_mismatch": False,
                        "external_canonical": False,
                        "mixed_content_present": False,
                        "mixed_content_count": 0,
                        "h1_count": 0,
                        "images_count": 0,
                        "images_missing_alt": 0,
                        "images_empty_alt": 0,
                        "internal_links_count": 0,
                        "missing_title": True,
                        "missing_meta_description": True,
                        "missing_h1": True,
                        "duplicate_title": False,
                        "duplicate_meta_description": False,
                    }
                )
                continue

            page_data = extract_page_data(response.text, final_url, domain)
            internal_links = page_data["internal_links"]
            canonical_mismatch = bool(page_data["canonical_url"]) and page_data["canonical_url"] != final_url
            for issue in page_data["image_issues"]:
                image_issues_report.append(
                    {
                        "source_page": current_url,
                        "image_url": issue["image_url"],
                        "issue_type": issue["issue_type"],
                        "alt_text": issue["alt_text"],
                    }
                )
            print(
                f"[INFO] HTTP {response.status_code}, {elapsed_ms} мс, "
                f"редиректов: {redirect_count}, внутренних ссылок: {len(internal_links)}"
            )

            pages_report.append(
                {
                    "url": current_url,
                    "final_url": final_url,
                    "status": response.status_code,
                    "response_time_ms": elapsed_ms,
                    "redirect_count": redirect_count,
                    "title": page_data["title"],
                    "title_length": page_data["title_length"],
                    "title_too_short": page_data["title_too_short"],
                    "title_too_long": page_data["title_too_long"],
                    "meta_description_length": page_data["meta_description_length"],
                    "meta_description": page_data["meta_description"],
                    "meta_description_too_short": page_data["meta_description_too_short"],
                    "meta_description_too_long": page_data["meta_description_too_long"],
                    "robots_directives": page_data["robots_directives"],
                    "noindex": page_data["noindex"],
                    "nofollow": page_data["nofollow"],
                    "html_lang": page_data["html_lang"],
                    "missing_lang": page_data["missing_lang"],
                    "hreflang_count": page_data["hreflang_count"],
                    "og_title_present": page_data["og_title_present"],
                    "og_description_present": page_data["og_description_present"],
                    "og_image_present": page_data["og_image_present"],
                    "twitter_card_present": page_data["twitter_card_present"],
                    "canonical_url": page_data["canonical_url"],
                    "canonical_mismatch": canonical_mismatch,
                    "external_canonical": page_data["external_canonical"],
                    "mixed_content_present": page_data["mixed_content_present"],
                    "mixed_content_count": page_data["mixed_content_count"],
                    "h1_count": page_data["h1_count"],
                    "images_count": page_data["images_count"],
                    "images_missing_alt": page_data["images_missing_alt"],
                    "images_empty_alt": page_data["images_empty_alt"],
                    "internal_links_count": len(internal_links),
                    "missing_title": page_data["missing_title"],
                    "missing_meta_description": page_data["missing_meta_description"],
                    "missing_h1": page_data["missing_h1"],
                    "duplicate_title": False,
                    "duplicate_meta_description": False,
                }
            )

            for link in sorted(internal_links):
                status_code, is_broken = get_link_status(session, link, timeout, status_cache)
                if is_broken:
                    broken_links_report.append(
                        {
                            "source_page": current_url,
                            "broken_link": link,
                            "status": status_code if status_code is not None else REQUEST_FAILED_STATUS,
                        }
                    )

                if link not in discovered_urls and not is_broken and len(discovered_urls) < max_pages:
                    discovered_urls.add(link)
                    queued_urls.append(link)

    annotate_duplicate_titles(pages_report)
    annotate_duplicate_meta_descriptions(pages_report)
    crawled_urls = {str(page["final_url"]) for page in pages_report}
    orphan_pages_report = [
        {"sitemap_url": url, "in_sitemap": True, "crawled": False}
        for url in sorted(site_report["sitemap_urls"])
        if url not in crawled_urls
    ]
    return pages_report, broken_links_report, site_report, orphan_pages_report, image_issues_report


def main() -> None:
    args = parse_args()

    try:
        pages_report, broken_links_report, site_report, orphan_pages_report, image_issues_report = audit_website(
            args.start_url,
            args.max_pages,
            args.timeout,
            args.allow_insecure,
        )
        summary_report = build_summary_report(
            pages_report,
            broken_links_report,
            site_report,
            orphan_pages_report,
            image_issues_report,
        )
        issues_report = build_issues_report(
            pages_report,
            broken_links_report,
            site_report,
            orphan_pages_report,
            image_issues_report,
        )
        write_pages_report(pages_report)
        write_broken_links_report(broken_links_report)
        write_site_report(site_report)
        write_summary_report(summary_report)
        write_orphan_pages_report(orphan_pages_report)
        write_image_issues_report(image_issues_report)
        write_issues_report(issues_report)
        write_html_report(
            args.start_url,
            summary_report,
            site_report,
            pages_report,
            broken_links_report,
            orphan_pages_report,
            image_issues_report,
            issues_report,
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("\n[ERROR] Выполнение прервано пользователем.")
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Непредвиденная ошибка: {exc}")
        raise SystemExit(1) from exc

    print(
        f"[DONE] Проверено страниц: {len(pages_report)}. "
        f"Битых ссылок найдено: {len(broken_links_report)}."
    )
    print(f"[DONE] Отчёт по страницам: {PAGES_REPORT_FILE}")
    print(f"[DONE] Отчёт по битым ссылкам: {BROKEN_LINKS_REPORT_FILE}")
    print(f"[DONE] Отчёт по сайту: {SITE_REPORT_FILE}")
    print(f"[DONE] Сводный отчёт: {SUMMARY_REPORT_FILE}")
    print(f"[DONE] Отчёт по orphan-страницам: {ORPHAN_PAGES_REPORT_FILE}")
    print(f"[DONE] Отчёт по изображениям: {IMAGE_ISSUES_REPORT_FILE}")
    print(f"[DONE] Унифицированный отчёт по проблемам: {ISSUES_REPORT_FILE}")
    print(f"[DONE] HTML-отчёт: {HTML_REPORT_FILE}")
    print_summary(
        pages_report,
        broken_links_report,
        site_report,
        orphan_pages_report,
        image_issues_report,
    )


if __name__ == "__main__":
    main()
