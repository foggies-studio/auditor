import argparse
import csv
import time
import warnings
from collections import Counter, deque
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


def extract_page_data(html: str, base_url: str, domain: str) -> Dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    meta_description_tag = soup.find(
        "meta",
        attrs={"name": lambda value: isinstance(value, str) and value.lower() == "description"},
    )
    meta_description = ""
    if meta_description_tag:
        meta_description = meta_description_tag.get("content", "").strip()

    canonical_tag = soup.find(
        "link",
        rel=lambda value: isinstance(value, str) and "canonical" in value.lower(),
    )
    canonical_url = ""
    if canonical_tag and canonical_tag.get("href"):
        canonical_candidate = urljoin(base_url, canonical_tag["href"])
        canonical_url = normalize_url(canonical_candidate) or canonical_candidate

    h1_tags = soup.find_all("h1")
    internal_links: Set[str] = set()

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
        "meta_description": meta_description,
        "meta_description_length": len(meta_description),
        "canonical_url": canonical_url,
        "h1_count": len(h1_tags),
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

    sitemap_status, sitemap_present, _ = inspect_resource(session, sitemap_url, timeout)

    return {
        "site_root": site_root,
        "robots_url": robots_url,
        "robots_status": robots_status,
        "robots_present": robots_present,
        "sitemap_url": sitemap_url,
        "sitemap_status": sitemap_status,
        "sitemap_present": sitemap_present,
    }


def annotate_duplicate_titles(pages_report: List[Dict[str, object]]) -> None:
    title_counts = Counter(
        str(page["title"]).strip().lower()
        for page in pages_report
        if str(page["title"]).strip()
    )
    for page in pages_report:
        normalized_title = str(page["title"]).strip().lower()
        page["duplicate_title"] = bool(normalized_title) and title_counts[normalized_title] > 1


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
                "meta_description_length",
                "canonical_url",
                "h1_count",
                "internal_links_count",
                "missing_title",
                "missing_meta_description",
                "missing_h1",
                "duplicate_title",
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
                    page["meta_description_length"],
                    page["canonical_url"],
                    page["h1_count"],
                    page["internal_links_count"],
                    page["missing_title"],
                    page["missing_meta_description"],
                    page["missing_h1"],
                    page["duplicate_title"],
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
            ]
        )


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
            "meta_description_length": 0,
            "canonical_url": "",
            "h1_count": 0,
            "internal_links_count": 0,
            "missing_title": True,
            "missing_meta_description": True,
            "missing_h1": True,
            "duplicate_title": False,
        }
    )


def print_summary(
    pages_report: List[Dict[str, object]],
    broken_links_report: List[Dict[str, object]],
    site_report: Dict[str, object],
) -> None:
    title_issues = sum(1 for page in pages_report if page["missing_title"])
    meta_issues = sum(1 for page in pages_report if page["missing_meta_description"])
    h1_issues = sum(1 for page in pages_report if page["missing_h1"])
    duplicate_titles = sum(1 for page in pages_report if page["duplicate_title"])
    redirects = sum(1 for page in pages_report if page["redirect_count"] > 0)
    status_counter = Counter(str(item["status"]) for item in broken_links_report)

    print(
        f"[SUMMARY] Без title: {title_issues}, без meta description: {meta_issues}, "
        f"без H1: {h1_issues}, с дубликатом title: {duplicate_titles}, страниц с редиректами: {redirects}"
    )
    print(
        f"[SUMMARY] robots.txt: {site_report['robots_status']} "
        f"({ 'найден' if site_report['robots_present'] else 'не найден' }), "
        f"sitemap: {site_report['sitemap_status']} "
        f"({ 'найден' if site_report['sitemap_present'] else 'не найден' })"
    )
    if status_counter:
        formatted = ", ".join(f"{status}: {count}" for status, count in sorted(status_counter.items()))
        print(f"[SUMMARY] Битые ссылки по статусам: {formatted}")


def audit_website(
    start_url: str,
    max_pages: int,
    timeout: int,
    allow_insecure: bool,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], Dict[str, object]]:
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
                        "meta_description_length": 0,
                        "canonical_url": "",
                        "h1_count": 0,
                        "internal_links_count": 0,
                        "missing_title": True,
                        "missing_meta_description": True,
                        "missing_h1": True,
                        "duplicate_title": False,
                    }
                )
                continue

            page_data = extract_page_data(response.text, final_url, domain)
            internal_links = page_data["internal_links"]
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
                    "meta_description_length": page_data["meta_description_length"],
                    "canonical_url": page_data["canonical_url"],
                    "h1_count": page_data["h1_count"],
                    "internal_links_count": len(internal_links),
                    "missing_title": page_data["missing_title"],
                    "missing_meta_description": page_data["missing_meta_description"],
                    "missing_h1": page_data["missing_h1"],
                    "duplicate_title": False,
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
    return pages_report, broken_links_report, site_report


def main() -> None:
    args = parse_args()

    try:
        pages_report, broken_links_report, site_report = audit_website(
            args.start_url,
            args.max_pages,
            args.timeout,
            args.allow_insecure,
        )
        write_pages_report(pages_report)
        write_broken_links_report(broken_links_report)
        write_site_report(site_report)
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
    print_summary(pages_report, broken_links_report, site_report)


if __name__ == "__main__":
    main()
