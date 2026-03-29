import os
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
    """Return True if `url` is within the same crawl scope.

    Scope is defined as:
    - Same domain (`base_netloc`), and
    - If `path_prefix` is provided, the path must start with that prefix.

    This is useful for multi-blog hosts like blog.naver.com where
    different blogs live under different first path segments, e.g.
    /ranto28/ vs /someoneelse/.
    """
    parsed = urlparse(url)
    if parsed.netloc != base_netloc:
        return False

    path = parsed.path or "/"

    # If we know a specific blog_id (e.g., "ranto28" on blog.naver.com),
    # keep URLs that either live under "/<blog_id>/" or have
    # "blogId=<blog_id>" in the query string.
    if blog_id:
        if path == f"/{blog_id}" or path.startswith(f"/{blog_id}/"):
            return True
        qs = parse_qs(parsed.query)
        blog_ids = qs.get("blogId") or qs.get("blogid")
        if blog_ids and blog_ids[0] == blog_id:
            return True
        return False

    # Generic path-based scoping for other hosts.
    if not path_prefix:
        return True
    return path.startswith(path_prefix)


def _load_robots(base_url: str) -> robotparser.RobotFileParser | None:
    """Best-effort load robots.txt; return None on failure.

    This helps you respect site policies but does not guarantee compliance.
    """
    try:
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(base_url, "/robots.txt")
        rp.set_url(robots_url)
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
    """Create a filesystem-safe, mostly-unique filename for a URL.

    We base it on the path plus a few key query parameters so that
    different posts (e.g., different `logNo` values on Naver) don't
    overwrite each other.
    """

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
    # Include a few common distinguishing parameters if present.
    suffix_parts: list[str] = []
    for key in ("logNo", "logno", "categoryNo", "page"):
        vals = qs.get(key)
        if vals and vals[0]:
            suffix_parts.append(f"{key}-{vals[0]}")

    if suffix_parts:
        return base + "_" + "_".join(suffix_parts)
    return base


def _seed_naver_post_urls(blog_id: str) -> list[str]:
    """Use Naver's PostTitleListAsync API to enumerate every post URL.

    Returns a list of post URLs like
    https://blog.naver.com/<blog_id>/<logNo>.
    Falls back to an empty list on any error.
    """
    urls: list[str] = []
    page = 1
    per_page = 100
    session = requests.Session()
    session.headers.update({"User-Agent": "blogscraperr/0.1"})

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
            data = resp.json()
        except Exception as exc:
            print(f"  [naver-api] page {page} failed: {exc}")
            break

        posts = data.get("postList") or []
        for post in posts:
            log_no = post.get("logNo")
            if log_no:
                urls.append(f"https://blog.naver.com/{blog_id}/{log_no}")

        print(f"  [naver-api] page {page}: found {len(posts)} posts (total so far: {len(urls)})")

        if len(posts) < per_page:
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

    - Only HTTP(S) pages on the same domain are visited.
    - Best-effort respect for robots.txt.
    - Content is rendered via a headless browser so JavaScript-generated
      text is captured and CSS copy-protection (user-select: none) is
      bypassed.
    - Each page is saved as a UTF-8 text file; an index.json is created.

    Parameters
    ----------
    start_url : str
        URL of any page within the blog (typically the homepage).
    out_dir : str
        Base output directory. A subfolder named after the domain is created.
    max_pages : int
        Safety limit on the number of pages to crawl.
    merged_filename : str | None
        If provided, all scraped pages are also appended to this single
        UTF-8 text file in the domain-specific output directory.
    write_individual_files : bool
        When True, save one `.txt` file per page as well; by default we
        only write the merged file to avoid thousands of small files.

    Returns
    -------
    list[dict]
        Metadata entries for all scraped pages: {"url", "title", "filepath"}.

    Notes
    -----
    Only crawl websites you are allowed to scrape and that permit
    automated access. Always review the site's terms of service.
    """

    start_url = _normalize_url(start_url)
    parsed_start = urlparse(start_url)
    base_netloc = parsed_start.netloc
    base_root = f"{parsed_start.scheme}://{parsed_start.netloc}"

    # For multi-blog hosts (like blog.naver.com/<blog_id>/...), we treat the
    # first path segment as the blog_id. For generic hosts, we keep using a
    # simple path prefix.
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

    rp = _load_robots(base_root)

    seed_urls = [start_url]
    if blog_id:
        print(f"  Fetching post list from Naver API for blog: {blog_id}")
        naver_posts = _seed_naver_post_urls(blog_id)
        if naver_posts:
            print(f"  Seeding queue with {len(naver_posts)} post URLs from API.")
            seed_urls.extend(naver_posts)

    queue: deque[str] = deque(seed_urls)
    visited: set[str] = set()
    index: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            while queue and len(visited) < max_pages:
                current = queue.popleft()
                current, _ = urldefrag(current)  # drop fragment

                if current in visited:
                    continue
                visited.add(current)

                if not _same_scope(current, base_netloc, blog_id, path_prefix):
                    continue

                if not _is_allowed(current, rp):
                    continue

                print(f"  Fetching ({len(visited)}/{max_pages}): {current}")

                try:
                    title, text, links, iframe_srcs = fetch_page_rendered(current, browser)
                except Exception as exc:
                    print(f"    [skip] {exc}")
                    continue

                print(f"    title={title!r}  text={len(text)}chars  links={len(links)}  iframes={len(iframe_srcs)}")

                filepath: str | None = None
                if write_individual_files:
                    filename = _sanitize_filename(current)
                    filepath = os.path.join(domain_dir, f"{filename}.txt")

                    try:
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(f"URL: {current}\n")
                            f.write(f"Title: {title}\n\n")
                            f.write(text)
                    except OSError:
                        filepath = None

                index.append({"url": current, "title": title, "filepath": filepath})

                if merged_file is not None:
                    # Collapse runs of blank lines to a single blank line
                    import re
                    clean_text = re.sub(r"\n{3,}", "\n\n", text)
                    merged_file.write("=" * 80 + "\n")
                    merged_file.write(f"URL: {current}\n")
                    merged_file.write(f"Title: {title}\n\n")
                    merged_file.write(clean_text)
                    merged_file.write("\n\n")

                # Enqueue links found after JS rendering
                for href in links:
                    next_url, _ = urldefrag(href)
                    if next_url not in visited and _same_scope(next_url, base_netloc, blog_id, path_prefix):
                        queue.append(next_url)

                # Enqueue iframe sources (Naver loads post body inside iframes)
                for src in iframe_srcs:
                    next_url, _ = urldefrag(src)
                    if next_url not in visited and _same_scope(next_url, base_netloc, blog_id, path_prefix):
                        queue.append(next_url)

        finally:
            browser.close()

    # Close merged file if opened
    if merged_file is not None:
        try:
            merged_file.close()
        except OSError:
            pass

    # Save index
    index_path = os.path.join(domain_dir, "index.json")
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

    return index
