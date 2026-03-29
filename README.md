<h1 align="center">Pocket Flow Project Template: Agentic Coding</h1>

<p align="center">
  <a href="https://github.com/The-Pocket/PocketFlow" target="_blank">
    <img 
      src="./assets/banner.png" width="800"
    />
  </a>
</p>

This is a project template for Agentic Coding with [Pocket Flow](https://github.com/The-Pocket/PocketFlow), a 100-line LLM framework, and your editor of choice.

- We have included rules files for various AI coding assistants to help you build LLM projects:
  - [.cursorrules](.cursorrules) for Cursor AI
  - [.clinerules](.clinerules) for Cline
  - [.windsurfrules](.windsurfrules) for Windsurf
  - [.goosehints](.goosehints) for Goose
  - Configuration in [.github](.github) for GitHub Copilot
  - [CLAUDE.md](CLAUDE.md) for Claude Code
  - [GEMINI.md](GEMINI.md) for Gemini
  
- Want to learn how to build LLM projects with Agentic Coding?

  - Check out the [Agentic Coding Guidance](https://the-pocket.github.io/PocketFlow/guide.html)
    
  - Check out the [YouTube Tutorial](https://www.youtube.com/@ZacharyLLM?sub_confirmation=1)

## Blog Scraper

This repo now includes a simple blog scraper.

- Entry point: [main.py](main.py)
- Scraper implementation: [utils/scraper.py](utils/scraper.py)

Usage (from the project root):

```bash
pip install -r requirements.txt
python main.py https://example-blog.com
```

The scraper will:

- Crawl pages on the same domain as the starting blog URL (up to a safety limit).
- Save each page as a UTF-8 `.txt` file under `scraped/<domain>/`.
- Create an `index.json` with metadata (`url`, `title`, `filepath`).

Non‑English blogs are supported because all text is handled as Unicode.

Only scrape sites you are permitted to crawl, and always respect their terms of service.
