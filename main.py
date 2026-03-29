import sys

from utils.scraper import scrape_blog


def main() -> None:
    """CLI entry point.

    Usage:
        python main.py https://example-blog.com
    """

    if len(sys.argv) < 2:
        print("Usage: python main.py <blog_url>")
        sys.exit(1)

    blog_url = sys.argv[1]

    print(f"Scraping blog starting from: {blog_url}")
    index = scrape_blog(blog_url)

    print(f"Scraped {len(index)} pages.")
    if index:
        sample = index[0]
        print("Sample page:")
        print(f"  URL: {sample['url']}")
        print(f"  Title: {sample['title']}")
        if sample.get("filepath"):
            print(f"  Saved to: {sample['filepath']}")
        print("All posts have been concatenated into 'all_posts.txt' under scraped/<domain>/.")


if __name__ == "__main__":
    main()
