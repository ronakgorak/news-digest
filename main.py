#!/usr/bin/env python3
"""
Automated News Discovery & Summarization.
 
Pipeline (no LLM involved in finding or fetching articles):
  1. Accept a topic and a result limit.
  2. Discover candidate articles from RSS sources (Google News search + publisher feeds).
  3. Retrieve article pages over HTTP, respecting robots.txt, with timeouts and retries.
  4. Clean text, filter by relevance and recency, and deduplicate by URL and title.
  5. Summarize each article extractively from the retrieved text (grounded, no invented facts).
  6. Export JSON, CSV and a Markdown digest.
 
Usage:
    python main.py --topic "cybersecurity" --limit 5
"""
 
import argparse
import csv
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse, urlunparse
from urllib.robotparser import RobotFileParser
 
import feedparser
import requests
from bs4 import BeautifulSoup
 
log = logging.getLogger("news_digest")
 
USER_AGENT = "NewsDigestBot/1.0 (educational project; contact: set NEWS_DIGEST_CONTACT)"
CONTACT = os.environ.get("NEWS_DIGEST_CONTACT", "")
REQUEST_TIMEOUT = 15          # seconds per HTTP request
MAX_RETRIES = 3               # attempts per URL before giving up
REQUEST_DELAY = 1.0           # polite pause between article fetches (seconds)
MIN_RELEVANCE = 1.0           # every topic keyword must appear in the title or feed description
MIN_TARGET_ARTICLES = 5       # coverage goal from the assignment
MIN_PUBLISHERS = 2            # distinct publishers required
EXCERPT_CHARS = 2000          # max stored article text per record
SUMMARY_MAX_SENTENCES = 3
 
# Sources are fixed. Only the topic changes between runs, so no article URLs are hard-coded.
# {query} is replaced with the URL-encoded topic.
SOURCES = [
    # Publisher RSS feeds: each entry links directly to the article, so robots.txt
    # and text retrieval apply to the publisher's own domain.
    # Feeds are filtered by topic keywords, so the same list works for any topic.
    # EV-focused publishers: most topic matches come from these.
    {"name": "Electrek", "url": "https://electrek.co/feed/", "is_search": False},
    {"name": "CleanTechnica", "url": "https://cleantechnica.com/feed/", "is_search": False},
    {"name": "InsideEVs", "url": "https://insideevs.com/rss/news/all/", "is_search": False},
    {"name": "electrive", "url": "https://www.electrive.com/feed/", "is_search": False},
    # General publishers: included for broader topics.
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "is_search": False},
    {"name": "BBC News", "url": "https://feeds.bbci.co.uk/news/rss.xml", "is_search": False},
    {"name": "NPR", "url": "https://feeds.npr.org/1001/rss.xml", "is_search": False},
    {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "is_search": False},
]
 
STOPWORDS = {"the", "and", "for", "with", "of", "in", "on", "a", "an", "to", "news", "latest"}
 
 
# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
 
def now_utc() -> datetime:
    return datetime.now(timezone.utc)
 
 
def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None
 
 
def clean(text: str) -> str:
    """Strip HTML tags, unescape entities and collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\u200b-\u200d\u2060\ufeff\u00ad]", "", text)  # zero-width and soft hyphens
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,;:!?)\]%])", r"\1", text)  # no space before punctuation
    return text.strip()
 
 
def topic_keywords(topic: str):
    """Content words of the topic. A trailing 's' is dropped so 'vehicles' also matches 'vehicle'."""
    words = [w for w in re.findall(r"[a-z0-9]+", topic.lower()) if w not in STOPWORDS]
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]
 
 
def relevance(keywords, text: str) -> float:
    """Share of topic keywords found (as whole or inflected words) in the text."""
    if not keywords:
        return 1.0
    low = text.lower()
    hits = sum(1 for k in keywords if re.search(rf"\b{re.escape(k)}(s|es|ed|ing)?\b", low))
    return hits / len(keywords)
 
 
def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return None
 
 
def normalize_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", "", ""))
 
 
def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())
 
 
def split_sentences(text: str):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text) if s.strip()]
 
 
# ----------------------------------------------------------------------------
# Network
# ----------------------------------------------------------------------------
 
def http_get(session: requests.Session, url: str) -> requests.Response:
    """GET with a timeout and retries. Raises the last error if every attempt fails."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_error = exc
            log.debug("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 1.5)
    raise last_error
 
 
_robots_cache: dict = {}
 
 
def allowed_by_robots(session: requests.Session, url: str) -> bool:
    """Check robots.txt for the URL's host. If robots.txt is unreachable, default to allowed."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        parser = RobotFileParser()
        try:
            resp = session.get(base + "/robots.txt", timeout=REQUEST_TIMEOUT)
            parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except requests.RequestException:
            parser.parse([])
        _robots_cache[base] = parser
    return _robots_cache[base].can_fetch(USER_AGENT, url)
 
 
# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------
 
def discover(topic: str, days: int, session: requests.Session):
    """Pull every configured RSS source and return candidate entries plus per-source status."""
    keywords = topic_keywords(topic)
    cutoff = now_utc() - timedelta(days=days)
    candidates, status = [], []
 
    for src in SOURCES:
        url = src["url"].format(query=quote_plus(topic))
        try:
            resp = http_get(session, url)
        except requests.RequestException as exc:
            msg = f"unavailable ({type(exc).__name__})"
            log.error("Source '%s' %s. Continuing with other sources.", src["name"], msg)
            status.append({"source": src["name"], "status": msg, "entries_seen": 0, "entries_kept": 0})
            continue
 
        feed = feedparser.parse(resp.content)
        kept = 0
        for entry in feed.entries:
            raw_title = clean(entry.get("title", ""))
            link = entry.get("link", "")
            summary = clean(entry.get("summary", ""))
            published = entry_datetime(entry)
 
            # Google News titles look like "Headline - Publisher"; use the entry's source tag when present.
            source_info = entry.get("source") or {}
            publisher = clean(source_info.get("title", "")) if src["is_search"] else src["name"]
            if src["is_search"] and publisher:
                suffix = f" - {publisher}"
                if raw_title.endswith(suffix):
                    raw_title = raw_title[: -len(suffix)]
            if not publisher:
                publisher = src["name"]
 
            if not link or not raw_title:
                continue
            if published and published < cutoff:
                continue  # outside the lookback window
            if relevance(keywords, f"{raw_title} {summary}") < MIN_RELEVANCE:
                continue  # off-topic
 
            kept += 1
            candidates.append({
                "title": raw_title,
                "source": publisher,
                "url": link,
                "published": published,
                "feed_summary": summary,
            })
 
        status.append({
            "source": src["name"],
            "status": "ok",
            "entries_seen": len(feed.entries),
            "entries_kept": kept,
        })
        log.info("Source '%s': %d entries seen, %d relevant and recent", src["name"], len(feed.entries), kept)
 
    return candidates, status
 
 
# ----------------------------------------------------------------------------
# Retrieval, cleaning and summarization
# ----------------------------------------------------------------------------
 
def extract_article_text(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "figure"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs, seen = [], set()
    for p in root.find_all("p"):
        text = clean(p.get_text(" "))
        key = normalize_title(text)  # ignores case and punctuation
        if len(text) >= 60 and key not in seen:  # skip captions, menus, boilerplate and repeats
            seen.add(key)
            paragraphs.append(text)
    return " ".join(paragraphs)
 
 
def extractive_summary(text: str) -> str | None:
    """Take the first few well-formed sentences from the retrieved text. Nothing is generated."""
    sentences, seen = [], set()
    for s in split_sentences(text):
        key = normalize_title(s)
        if 40 <= len(s) <= 400 and key not in seen:
            seen.add(key)
            sentences.append(s)
    if not sentences:
        return None
    return " ".join(sentences[:SUMMARY_MAX_SENTENCES])
 
 
def build_record(candidate: dict, session: requests.Session) -> dict:
    missing = []
    retrieved_text, extraction_note = "", None
 
    url = candidate["url"]
    if not allowed_by_robots(session, url):
        extraction_note = "skipped: robots.txt disallows fetching this URL"
    else:
        try:
            resp = http_get(session, url)
            retrieved_text = extract_article_text(resp.text)
            if not retrieved_text:
                extraction_note = "no article text found on page"
        except requests.RequestException as exc:
            extraction_note = f"article fetch failed ({type(exc).__name__})"
        time.sleep(REQUEST_DELAY)
 
    if retrieved_text:
        excerpt = retrieved_text[:EXCERPT_CHARS]
        summary_basis = "article_text"
    else:
        excerpt = candidate["feed_summary"][:EXCERPT_CHARS]
        summary_basis = "feed_summary" if excerpt else None
        missing.append("article_text")
 
    summary = extractive_summary(excerpt) if excerpt else None
    if summary is None:
        missing.append("summary")
    if candidate["published"] is None:
        missing.append("published_at")
    if not excerpt:
        missing.append("excerpt")
 
    return {
        "title": candidate["title"],
        "source": candidate["source"],
        "url": candidate["url"],
        "published_at": iso(candidate["published"]),
        "retrieved_at": iso(now_utc()),
        "excerpt": excerpt,
        "summary": summary,
        "summary_basis": summary_basis,
        "extraction_note": extraction_note,
        "missing_fields": missing,
    }
 
 
# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
 
def run(topic: str, limit: int, days: int, session: requests.Session) -> dict:
    candidates, source_status = discover(topic, days, session)
 
    # Newest first; undated items go last so flagged records never displace dated ones.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    candidates.sort(key=lambda c: c["published"] or epoch, reverse=True)
 
    articles, seen_urls, seen_titles = [], set(), set()
    for cand in candidates:
        if len(articles) >= limit:
            break
        url_key = normalize_url(cand["url"])
        title_key = normalize_title(cand["title"])
        if url_key in seen_urls or title_key in seen_titles:
            log.debug("Duplicate skipped: %s", cand["title"])
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        articles.append(build_record(cand, session))
 
    publishers = sorted({a["source"] for a in articles})
    shortfall = []
    if len(articles) < min(limit, MIN_TARGET_ARTICLES):
        shortfall.append(
            f"Only {len(articles)} unique article(s) found within the last {days} days "
            f"(target: {min(limit, MIN_TARGET_ARTICLES)})."
        )
    if len(publishers) < MIN_PUBLISHERS and articles:
        shortfall.append(f"Articles came from only {len(publishers)} publisher(s); target is {MIN_PUBLISHERS}.")
    failed = [s["source"] for s in source_status if s["status"] != "ok"]
    if failed:
        shortfall.append("Sources unavailable this run: " + ", ".join(failed) + ".")
 
    return {
        "topic": topic,
        "generated_at": iso(now_utc()),
        "lookback_days": days,
        "requested_limit": limit,
        "sources": source_status,
        "publishers_found": publishers,
        "shortfall": shortfall,
        "articles": articles,
    }
 
 
# ----------------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------------
 
def write_outputs(result: dict, out_dir: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", result["topic"].lower()).strip("-") or "topic"
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(out_dir, f"{slug}-{stamp}")
    os.makedirs(run_dir, exist_ok=True)
 
    with open(os.path.join(run_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
 
    fields = ["title", "source", "url", "published_at", "retrieved_at",
              "summary", "summary_basis", "extraction_note", "missing_fields", "excerpt"]
    with open(os.path.join(run_dir, "results.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for a in result["articles"]:
            row = {k: a.get(k) for k in fields}
            row["missing_fields"] = ";".join(a["missing_fields"])
            writer.writerow(row)
 
    with open(os.path.join(run_dir, "digest.md"), "w", encoding="utf-8") as f:
        f.write(render_digest(result))
 
    return run_dir
 
 
def render_digest(result: dict) -> str:
    lines = [
        f"# News digest: {result['topic']}",
        "",
        f"Generated {result['generated_at']} · lookback {result['lookback_days']} days · "
        f"{len(result['articles'])} article(s)",
        "",
    ]
    if result["shortfall"]:
        lines += ["> **Notes on this run**"] + [f"> - {s}" for s in result["shortfall"]] + [""]
    if not result["articles"]:
        lines += ["No articles matched this topic in the lookback window."]
        return "\n".join(lines) + "\n"
 
    for i, a in enumerate(result["articles"], 1):
        date = a["published_at"][:10] if a["published_at"] else "date not available"
        lines += [
            f"## {i}. {a['title']}",
            f"**Source:** {a['source']} · **Published:** {date} · **Retrieved:** {a['retrieved_at']}",
            f"**Link:** {a['url']}",
            "",
            a["summary"] or "_No summary available: article text could not be retrieved._",
            "",
        ]
        if a["missing_fields"]:
            lines += [f"_Missing: {', '.join(a['missing_fields'])}_", ""]
        if a["extraction_note"]:
            lines += [f"_Note: {a['extraction_note']}_", ""]
    return "\n".join(lines) + "\n"
 
 
# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
 
def main() -> int:
    parser = argparse.ArgumentParser(description="Discover, retrieve and summarize recent news on a topic.")
    parser.add_argument("--topic", help="Topic to search for, e.g. 'electric vehicles'. Prompted if omitted.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum number of articles (default: 5).")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7).")
    parser.add_argument("--out-dir", default="output", help="Directory for results (default: ./output).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logging.")
    args = parser.parse_args()
 
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")
 
    topic = (args.topic or input("Topic: ")).strip()
    if not topic:
        print("A topic is required.", file=sys.stderr)
        return 1
    if args.limit < 1 or args.days < 1:
        print("--limit and --days must be at least 1.", file=sys.stderr)
        return 1
 
    session = requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}
    if CONTACT:
        headers["From"] = CONTACT
    session.headers.update(headers)
 
    print(f"Searching for '{topic}' (limit {args.limit}, last {args.days} days)...")
    result = run(topic, args.limit, args.days, session)
    run_dir = write_outputs(result, args.out_dir)
 
    print(f"\nFound {len(result['articles'])} article(s) from {len(result['publishers_found'])} publisher(s).")
    for note in result["shortfall"]:
        print(f"  ! {note}")
    print(f"Outputs written to: {run_dir}/ (results.json, results.csv, digest.md)")
 
    return 0 if result["articles"] else 2
 
 
if __name__ == "__main__":
    sys.exit(main())
 