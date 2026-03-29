"""Playwright-based page fetcher.

Uses a headless browser so that JavaScript-rendered content is visible,
and extracts text via `innerText` so that CSS `user-select: none`
copy-protection is bypassed entirely.
"""

from __future__ import annotations

from playwright.sync_api import Browser, sync_playwright


def fetch_page_rendered(
    url: str,
    browser: Browser,
    timeout_ms: int = 30_000,
) -> tuple[str, str, list[str], list[str]]:
    """Navigate to *url* and return *(title, text, links, iframe_srcs)*.

    Parameters
    ----------
    url : str
        The page to fetch.
    browser : playwright.sync_api.Browser
        A shared browser instance (create once, reuse across calls).
    timeout_ms : int
        Navigation timeout in milliseconds.

    Returns
    -------
    title : str
        Page ``<title>`` text (falls back to the URL).
    text : str
        Visible text extracted from the page and all of its iframes.
        Because we use ``innerText`` via JavaScript, CSS rules such as
        ``user-select: none`` have no effect on what we can read.
    links : list[str]
        All absolute ``href`` values found in ``<a>`` elements after JS
        rendering, collected from every frame (main + iframes).
    iframe_srcs : list[str]
        All absolute ``src`` values found in ``<iframe>`` elements after
        JS rendering (the current scraper follows these for Naver blogs).
    """
    page = browser.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)

        title: str = page.title() or url

        # Collect visible text and links from every frame (main page + all iframes).
        # innerText reads rendered text and ignores user-select CSS.
        text_parts: list[str] = []
        links: list[str] = []

        for frame in page.frames:
            try:
                frame_text: str = frame.evaluate(
                    """() => {
                        if (!document.body) return '';
                        // innerText on the live document works correctly and
                        // ignores user-select:none (that's a UI-only CSS rule).
                        // script/style content is automatically excluded by
                        // innerText because those elements are not rendered.
                        return document.body.innerText || '';
                    }"""
                )
                stripped = frame_text.strip() if frame_text else ""
                if stripped:
                    text_parts.append(stripped)
            except Exception:
                pass

            # Collect links from this frame too
            try:
                frame_links: list[str] = frame.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.href)
                        .filter(h => h && h.startsWith('http'))"""
                )
                links.extend(frame_links)
            except Exception:
                pass

        text = "\n\n".join(text_parts)

        # Collect iframe sources from the main frame
        iframe_srcs: list[str] = page.evaluate(
            """() => Array.from(document.querySelectorAll('iframe[src]'))
                .map(f => f.src)
                .filter(s => s && s.startsWith('http'))"""
        )

        return title, text, links, iframe_srcs

    finally:
        page.close()


if __name__ == "__main__":
    # Quick smoke test: python -m utils.fetch_rendered
    test_url = "https://blog.naver.com/ranto28"
    print(f"Fetching: {test_url}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        title, text, links, iframes = fetch_page_rendered(test_url, browser)
        print(f"Title   : {title}")
        print(f"Text    : {text[:300]}...")
        print(f"Links   : {len(links)}")
        print(f"Iframes : {len(iframes)}")
        for l in links[:10]:
            print(f"  link: {l}")
        for i in iframes:
            print(f"  iframe: {i}")
        browser.close()
