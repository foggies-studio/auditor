import argparse
import csv
import time
from collections import deque
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


REQUEST_TIMEOUT = 10
PAGES_REPORT_FILE = "pages_report.csv"
BROKEN_LINKS_REPORT_FILE = "broken_links_report.csv"


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


def fetch_url(session: requests.Session, url: str) -> Tuple[Optional[requests.Response], Optional[str], int]:
    start = time.perf_counter()

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return response, None, elapsed_ms
    except requests.RequestException as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return None, str(exc), elapsed_ms


def extract_title_and_links(html: str, base_url: str, domain: str) -> Tuple[str, Set[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    internal_links: Set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue

        absolute_url = urljoin(base_url, href)
        normalized_url = normalize_url(absolute_url)
        if normalized_url and is_internal_link(normalized_url, domain):
            internal_links.add(normalized_url)

    return title, internal_links


def get_link_status(
    session: requests.Session,
    url: str,
    status_cache: Dict[str, Tuple[Optional[int], bool]],
) -> Tuple[Optional[int], bool]:
    if url in status_cache:
        return status_cache[url]

    response, error, _ = fetch_url(session, url)
    if error:
        status_cache[url] = (None, True)
        return status_cache[url]

    status_cache[url] = (response.status_code, response.status_code >= 400)
    return status_cache[url]


def write_pages_report(pages: List[Dict[str, object]]) -> None:
    with open(PAGES_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            ["url", "http_status", "response_time_ms", "title", "internal_links_count"]
        )
        for page in pages:
            writer.writerow(
                [
                    page["url"],
                    page["status"],
                    page["response_time_ms"],
                    page["title"],
                    page["internal_links_count"],
                ]
            )


def write_broken_links_report(broken_links: List[Dict[str, object]]) -> None:
    with open(BROKEN_LINKS_REPORT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["source_page", "broken_link", "status"])
        for item in broken_links:
            writer.writerow([item["source_page"], item["broken_link"], item["status"]])


def audit_website(start_url: str, max_pages: int) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    normalized_start_url = normalize_url(start_url)
    if not normalized_start_url:
        raise ValueError("Стартовый URL должен быть абсолютным HTTP(S)-адресом.")

    if max_pages <= 0:
        raise ValueError("--max-pages должен быть положительным числом.")

    domain = urlparse(normalized_start_url).hostname
    if not domain:
        raise ValueError("Не удалось определить домен стартового URL.")

    pages_report: List[Dict[str, object]] = []
    broken_links_report: List[Dict[str, object]] = []
    queued_urls = deque([normalized_start_url])
    visited_urls: Set[str] = set()
    discovered_urls: Set[str] = {normalized_start_url}
    status_cache: Dict[str, Tuple[Optional[int], bool]] = {}

    with requests.Session() as session:
        session.headers.update({"User-Agent": "Website Auditor/1.0"})

        while queued_urls and len(visited_urls) < max_pages:
            current_url = queued_urls.popleft()
            if current_url in visited_urls:
                continue

            print(f"[INFO] Проверка страницы: {current_url}")
            response, error, elapsed_ms = fetch_url(session, current_url)
            visited_urls.add(current_url)

            if error:
                print(f"[WARN] Ошибка запроса: {error}")
                status_cache[current_url] = (None, True)
                pages_report.append(
                    {
                        "url": current_url,
                        "status": "REQUEST_FAILED",
                        "response_time_ms": elapsed_ms,
                        "title": "",
                        "internal_links_count": 0,
                    }
                )
                continue

            status_cache[current_url] = (response.status_code, response.status_code >= 400)

            if response.status_code >= 400:
                print(f"[WARN] Страница вернула HTTP {response.status_code}")
                pages_report.append(
                    {
                        "url": current_url,
                        "status": response.status_code,
                        "response_time_ms": elapsed_ms,
                        "title": "",
                        "internal_links_count": 0,
                    }
                )
                continue

            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                print("[INFO] Пропуск разбора: контент не является HTML")
                pages_report.append(
                    {
                        "url": current_url,
                        "status": response.status_code,
                        "response_time_ms": elapsed_ms,
                        "title": "",
                        "internal_links_count": 0,
                    }
                )
                continue

            title, internal_links = extract_title_and_links(response.text, current_url, domain)
            print(
                f"[INFO] HTTP {response.status_code}, {elapsed_ms} мс, найдено внутренних ссылок: {len(internal_links)}"
            )

            pages_report.append(
                {
                    "url": current_url,
                    "status": response.status_code,
                    "response_time_ms": elapsed_ms,
                    "title": title,
                    "internal_links_count": len(internal_links),
                }
            )

            for link in sorted(internal_links):
                status_code, is_broken = get_link_status(session, link, status_cache)
                if is_broken:
                    broken_links_report.append(
                        {
                            "source_page": current_url,
                            "broken_link": link,
                            "status": status_code if status_code is not None else "REQUEST_FAILED",
                        }
                    )

                if link not in discovered_urls and not is_broken and len(discovered_urls) < max_pages:
                    discovered_urls.add(link)
                    queued_urls.append(link)

    return pages_report, broken_links_report


def main() -> None:
    args = parse_args()

    try:
        pages_report, broken_links_report = audit_website(args.start_url, args.max_pages)
        write_pages_report(pages_report)
        write_broken_links_report(broken_links_report)
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


if __name__ == "__main__":
    main()
