import os
import re
import json
from collections import deque
from urllib.parse import urlparse, urljoin, urldefrag, parse_qs
from urllib import robotparser

import requests
from playwright.sync_api import sync_playwright

from utils.fetch_rendered import fetch_page_rendered


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc
    path = parsed.path or "/"
    return f"{scheme}://{netloc}{path}"


def _same_scope(url: str, base_netloc: str, blog_id: str | None, path_prefix: str | None) -> bool:
    parsed = urlparse(url)
    if parsed.netloc != base_netloc:
        return False
    path = parsed.path or "/"
    if blog_id:
        if path == f"/{blog_id}" or path.startswith(f"/{blog_id}/"):
            return True
        qs = parse_qs(parsed.query)
        blog_ids = qs.get("blogId") or qs.get("blogid")
        if blog_ids and blog_ids[0] == blog_id:
            return True
        return False
    if not path_prefix:
        return True
    return path.startswith(path_prefix)


def _load_robots(base_url: str) -> robotparser.RobotFileParser | None:
    try:
        rp = robotparser.RobotFileParser()
        rp.set_url(urljoin(base_url, "/robots.txt"))
        rp.read()
        return rp
    except Exception:
        return None


def _is_allowed(url: str, rp: robotparser.RobotFileParser | None) -> bool:
    if rp is None:
        return True
    try:
        return rp.can_fetch("*", url)
    except Exception:
        return True


def _sanitize_filename(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if not path or path == "/":
        base = "index"
    else:
        name = path.strip("/\\")
        safe = []
        for ch in name:
            if ch.isalnum() or ch in ("-", "_", "."):
                safe.append(ch)
            else:
                safe.append("-")
        base = "_".join("".join(safe).split("/")) or "page"

    qs = parse_qs(parsed.query)
    suffix_parts: list[str] = []
    for key in ("logNo", "logno", "categoryNo", "page"):
        vals = qs.get(key)
        if vals and vals[0]:
            suffix_parts.append(f"{key}-{vals[0]}")

    if suffix_parts:
        return base + "_" + "_".join(suffix_parts)
    return base


def _get_naver_post_urls(blog_id: str) -> list[str]:
    """Fetch all post URLs for a Naver blog using the PostTitleListAsync API.

    Returns PostView URLs in newest-first order (one per post, no duplicates).
    Each URL points directly to the post content page.
    """
    urls: list[str] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "blogscraperr/0.1"})
    per_page = 100
    page = 1

    while True:
        try:
            resp = session.get(
                "https://blog.naver.com/PostTitleListAsync.naver",
                params={
                    "blogId": blog_id,
                    "currentPage": page,
                    "categoryNo": 0,
                    "countPerPage": per_page,
                },
                timeout=10,
            )
            try:
                data = resp.json()
            except ValueError:
                # Naver sometimes puts invalid JSON escape sequences (e.g. \k)
                # in post titles. Fix lone backslashes before parsing.
                fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', resp.text)
                try:
                    data = json.loads(fixed)
                except ValueError as exc2:
                    print(f"  [naver-api] page {page} JSON unfixable, skipping: {exc2}")
                    page += 1
                    continue
        except Exception as exc:
            print(f"  [naver-api] page {page} request failed: {exc}")
            page += 1
            continue

        posts = data.get("postList") or []
        for post in posts:
            log_no = post.get("logNo")
            if log_no:
                # PostView.naver is the actual content page (the iframe target).
                # Visiting it directly gives us the post body without any
                # additional iframe nesting.
                urls.append(
                    f"https://blog.naver.com/PostView.naver"
                    f"?blogId={blog_id}&logNo={log_no}"
                )

        total = data.get("totalCount", "?")
        print(f"  [naver-api] page {page}: {len(posts)} posts  (total so far: {len(urls)} / {total})")

        if not posts:
            break
        page += 1

    return urls


def scrape_blog(
    start_url: str,
    out_dir: str = "scraped",
    max_pages: int = 5000,
    merged_filename: str | None = "all_posts.txt",
    write_individual_files: bool = False,
) -> list[dict]:
    """Crawl a blog starting from `start_url` and save pages to disk.

    For Naver blogs
    ---------------
    Uses the PostTitleListAsync API to enumerate every post in newest-first
    order and visits each PostView page exactly once.  Link/iframe following
    is intentionally disabled to prevent loops.

    For generic blogs
    -----------------
    BFS crawl that follows <a href> and <iframe src> links within the same
    domain / path scope.

    Notes
    -----
    Only crawl websites you are allowed to scrape and that permit
    automated access. Always review the site's terms of service.
    """
    start_url = _normalize_url(start_url)
    parsed_start = urlparse(start_url)
    base_netloc = parsed_start.netloc
    base_root = f"{parsed_start.scheme}://{parsed_start.netloc}"

    blog_id: str | None = None
    path_prefix: str | None = None
    start_path = (parsed_start.path or "/").strip("/\\")
    if start_path:
        first_segment = start_path.split("/", 1)[0]
        if base_netloc == "blog.naver.com":
            blog_id = first_segment
        else:
            path_prefix = f"/{first_segment}/"

    domain_dir = os.path.join(out_dir, base_netloc)
    os.makedirs(domain_dir, exist_ok=True)

    merged_path = os.path.join(domain_dir, merged_filename) if merged_filename else None
    merged_file = None
    if merged_path is not None:
        try:
            merged_file = open(merged_path, "w", encoding="utf-8")
        except OSError:
            merged_file = None

    # --- Build the queue ---
    if blog_id:
        # Naver: enumerate every post via the API (newest first).
        # Skip the outer blog homepage — it's just a shell.
        print(f"Fetching full post list from Naver API for blog: {blog_id}")
        queue: deque[str] = deque(_get_naver_post_urls(blog_id))
        print(f"Queued {len(queue)} posts. Starting scrape...\n")
        follow_links = False  # API already gave us all URLs; following links causes loops
        rp = None  # Skip robots.txt for Naver; we're using their official API
    else:
        queue = deque([start_url])
        rp = _load_robots(base_root)
        follow_links = True

    visited: set[str] = set()
    index: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            while queue and len(visited) < max_pages:
                current = queue.popleft()
                current, _ = urldefrag(current)

                if current in visited:
                    continue
                visited.add(current)

                if not _same_scope(current, base_netloc, blog_id, path_prefix):
                    continue

                if not _is_allowed(current, rp):
                    continue

                print(f"  [{len(visited)}/{len(visited) + len(queue)}] {current}")

                try:
                    title, text, links, iframe_srcs = fetch_page_rendered(current, browser)
                except Exception as exc:
                    print(f"    [skip] {exc}")
                    continue

                # Collapse excessive blank lines
                clean_text = re.sub(r"\n{3,}", "\n\n", text).strip()

                print(f"    title={title!r}  chars={len(clean_text)}")

                filepath: str | None = None
                if write_individual_files:
                    filename = _sanitize_filename(current)
                    filepath = os.path.join(domain_dir, f"{filename}.txt")
                    try:
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(f"URL: {current}\nTitle: {title}\n\n{clean_text}")
                    except OSError:
                        filepath = None

                index.append({"url": current, "title": title, "filepath": filepath})

                if merged_file is not None:
                    merged_file.write("=" * 80 + "\n")
                    merged_file.write(f"URL: {current}\nTitle: {title}\n\n")
                    merged_file.write(clean_text)
                    merged_file.write("\n\n")

                # For generic blogs only: follow links discovered on the page
                if follow_links:
                    for href in links:
                        next_url, _ = urldefrag(href)
                        if next_url not in visited and _same_scope(next_url, base_netloc, blog_id, path_prefix):
                            queue.append(next_url)
                    for src in iframe_srcs:
                        next_url, _ = urldefrag(src)
                        if next_url not in visited and _same_scope(next_url, base_netloc, blog_id, path_prefix):
                            queue.append(next_url)

        finally:
            browser.close()

    if merged_file is not None:
        try:
            merged_file.close()
        except OSError:
            pass

    index_path = os.path.join(domain_dir, "index.json")
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

    return index
