# Automated News Discovery & Summarization

A command-line Python workflow that takes a topic, discovers recent news from public RSS sources,
retrieves the article pages, cleans and deduplicates the results, writes a grounded summary for each
article, and exports machine-readable data plus a human-readable digest.

No LLM, chatbot or AI search tool is used to find, select or fetch articles. Summaries are extractive:
they are built from sentences in the retrieved article text, so nothing is generated.

## Setup

Requires Python 3.9 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python main.py --topic "electric vehicles" --limit 5
python main.py --topic "electric vehicles" --limit 8 --days 7 --out-dir output
python main.py                      # prompts for a topic
python main.py --topic "AI regulation" -v   # may return few or no articles with the current feeds
```

| Option | Default | Meaning |
|---|---|---|
| `--topic` | prompted | Topic or keywords to search for |
| `--limit` | 5 | Maximum number of articles to keep |
| `--days` | 7 | Lookback window in days |
| `--out-dir` | `output` | Where results are written |
| `-v` | off | Debug logging (per-request detail) |

Exit codes: `0` if at least one article was exported, `2` if none matched, `1` for invalid input.

Optional environment variable: `NEWS_DIGEST_CONTACT` (an email or URL added to the `User-Agent`/`From`
headers so site operators can reach you). Nothing secret is required, so no API keys are used.

## Sample run

The included sample is in `sample_output/electric-vehicles-20261112-103725/`. It was produced with:

```bash
python main.py --topic "electric vehicles" --limit 5 -v
```

It contains `digest.md` (readable summaries and links), `results.json`, and `results.csv`.
The run returned five articles from two publishers (Electrek and electrive).

## Output

Each run creates `output/<topic-slug>-<timestamp>/` containing:

- `results.json`: topic, run timestamp, per-source status, publishers found, shortfall notes, and an
  `articles` list. Each article has `title`, `source`, `url`, `published_at`, `retrieved_at`, `excerpt`,
  `summary`, `summary_basis`, `extraction_note` and `missing_fields`.
- `results.csv`: the same article fields in a flat table.
- `digest.md`: a readable digest with a summary and source link for each article, plus notes on any
  shortfall or missing data.

Missing values are kept as `null` and listed in `missing_fields`. They are never filled in with invented text.

## How it works

| Step | Implementation |
|---|---|
| Input | `--topic` and `--limit` from the command line, or a prompt |
| Discover | RSS requests to publisher feeds (Electrek, CleanTechnica, InsideEVs, electrive, The Verge, BBC News, NPR, The Guardian), filtered by topic keywords |
| Retrieve | `requests` with a timeout, up to 3 attempts with backoff, and a 1-second pause between article fetches |
| Robots | `robots.txt` is checked for each article host; disallowed URLs are skipped and recorded |
| Clean | HTML stripped, entities decoded, invisible characters removed, spacing before punctuation fixed; article text taken from `<article>`/`<main>` paragraphs, boilerplate and short fragments dropped |
| Filter | Items older than the lookback window are dropped; every topic keyword must appear in the title or feed description (a trailing 's' is ignored, so 'vehicles' also matches 'vehicle') |
| Deduplicate | By normalized URL and normalized title |
| Summarize | Repeated paragraphs and sentences removed, then the first 2 to 3 well-formed sentences; falls back to the feed description (flagged as `summary_basis: feed_summary`) if the page cannot be read |
| Export | JSON, CSV and Markdown |

The topic is used to filter the configured feeds, so no URLs need to change between runs. Results depend
on which feeds cover the topic.

## Design choices

- **RSS first.** Feeds are stable, public and need no keys, so they are the most dependable way to
  discover news without a browser.
- **Publisher feeds, not Google News.** Google News links are redirect wrappers whose publisher URL
  can't be recovered without calling Google's undocumented internal API. Direct feeds link to the
  real article, so the robots.txt check and text retrieval apply to the publisher's own domain.
- **Failures are isolated.** Each source and each article is handled separately, so one timeout or
  blocked page only adds a note to the output and never stops the run.
- **Honest shortfalls.** If fewer than five articles or two publishers are found, the output and the
  console say so.

## Known limitations

- Feed coverage depends on the topic. General feeds (BBC, NPR, Guardian) rarely carry niche topics, so
  most results for specialized topics come from the topic-focused publisher feeds.
- The feed list is focused on electric vehicles and general news. Topics outside those areas may return
  few or no articles, because the tool only searches the configured feeds.
- Feed URLs can change or go offline. An unavailable feed is logged and reported, and the run continues.
- Keyword relevance is lexical. It misses synonyms (for example "EV" for "electric vehicle"), so a relevant story can be skipped.
- Extractive summaries repeat the article's own opening sentences. They are faithful but not as concise
  as an abstractive summary, and an article's first sentences are not always its key point.
- The publication date depends on what the feed provides. Undated items are kept and flagged.
- Only the eight configured sources are used. Adding more means adding entries to `SOURCES` in `main.py`.

## What I would improve with more time

- Add per-publisher parsers for sites whose article pages hide text behind scripts.
- Group near-duplicate stories across publishers (title similarity or embeddings) into one entry with several links.
- Add a `--sources` option backed by a JSON config file, and an opt-in LLM summarization step that
  receives only the retrieved text.
- Add unit tests for the cleaning, filtering, deduplication and summarization functions, with recorded fixtures.
- Add date filters beyond "last N days" and a scheduled run (cron or GitHub Actions).