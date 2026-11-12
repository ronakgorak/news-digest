# Submission note: Automated News Discovery & Summarization

**Sources and collection:** The script pulls RSS feeds from Electrek, CleanTechnica, InsideEVs, electrive, The Verge, BBC News, NPR, and The Guardian. Each entry is filtered by topic keywords and by publication date (last 7 days by default), then deduplicated by URL and title. For each article, the script checks robots.txt, fetches the publisher page, and extracts text from its paragraphs. No LLM, chatbot, or AI search tool is used to find, select, or fetch articles.

**Summarization:** Summaries are extractive and do not use an LLM. The script takes the first two or three well-formed sentences of the retrieved article text, removing repeated sentences. Nothing is generated, so every sentence comes from the source article.

**Sample run:** `sample_output/electric-vehicles-20261112-103725/` contains `digest.md`, `results.json`, and `results.csv`, produced by `python main.py --topic "electric vehicles" --limit 5`. The run returned five articles from two publishers (Electrek and electrive).

**Known limitations:**
- Summaries use the article's opening sentences, which sometimes lead with background rather than the main news.
- Relevance uses keyword matching, so synonyms (such as "EV" for "electric vehicle") can be missed.
- Coverage depends on the feeds. General publishers rarely carry niche topics, so most results come from topic-focused sites.
- Dates come from feed metadata and can differ from the date in an article's URL.
- The feed list is focused on electric vehicles and general news. Topics outside those areas may return few or no articles, because the tool only searches the configured feeds.

**What I would improve with more time:**
- Rank sentences or add an LLM step on retrieved text to pick the most informative sentences.
- Add synonym matching and group near-duplicate stories across publishers.
- Add per-publisher parsers for sites that hide article text behind scripts.
- Add automated tests with recorded feed and page fixtures.
- Support scheduled runs.
