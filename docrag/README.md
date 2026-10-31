# docrag — find the right doc pages for Copilot

Local index over your documentation export. You ask a question, and it returns the most
relevant **documentation pages** (title + source URL), ready to paste or attach in Copilot Chat.
No LLM, nothing leaves your machine.

## Setup (WSL, once)

```bash
python3 -m venv ~/docrag-venv && source ~/docrag-venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU-only torch, much smaller
pip install sentence-transformers numpy
```

## Build the index

```bash
python docrag.py ingest /path/to/export/*.txt
```

The input is the export format with page blocks:

```
================================
DOCUMENT PAGE 2
Title: Getting Started - Example Docs
Source: https://…/getting-started/index.html
================================
# Getting Started
…
```

A file without page blocks is treated as a single page.

`.jsonl` files are also accepted, with one page per line:
`{"title", "url", "text", "date" (optional, YYYY-MM-DD), "source" (optional)}`.
That's what [mmscrape](../mmscrape/) produces for Mattermost threads. Docs and chat can go in
the same index:

```bash
python docrag.py ingest export/*.txt ../mmscrape/mm_export/*.jsonl
```

Re-run `ingest` whenever you re-scrape. Sections whose text didn't change keep their vectors,
so only new content is embedded.

`ingest` rebuilds the index from the files you give it, so always pass all of them, not only
the new ones. If the index holds files the command leaves out, ingest stops and lists them
rather than removing them. Add `--replace` when removing them is what you want.

To merge existing indexes into one without recomputing, ingest all the files into the new
index and point `--reuse` at the old ones:

```bash
DOCRAG_INDEX=./idx-all python docrag.py ingest docs/*.txt mm_export/*.jsonl --reuse ./idx-docs --reuse ./idx-chat
```

The index goes to `./docrag_index` (change it with `DOCRAG_INDEX=/some/dir`).

## Models

| Role | Model | Size |
|---|---|---|
| Search (`ingest --model`) | `BAAI/bge-small-en-v1.5` | 33M parameters, ~130 MB, fast on a laptop CPU |
| Reranker (`serve`/`ask --reranker`) | `cross-encoder/ms-marco-MiniLM-L-6-v2` | 22M parameters, ~90 MB, a few seconds per search on CPU |

- **An index keeps the model it was built with**, so changing the model means a full re-ingest
  (vectors from different models can't be mixed). Ingest shows its speed and time left, and can
  be stopped (Ctrl+C) and resumed: finished blocks of 256 sections are kept.
- **Reranking** re-reads the 20 best sections with the question (more when you ask for over 6
  pages) and refines their order: its ranking is blended with the first one, not substituted
  for it. Turn it off per search with the **Ranking** switch on the web page or `ask --no-rerank`,
  or entirely with `serve --no-rerank`. If the reranker can't be loaded, searches still work,
  with a warning.
- Models are downloaded on first use, then loaded from the local cache in offline mode, without
  contacting Hugging Face. `DOCRAG_OFFLINE=1` forbids downloads.
**Certificate errors** during `pip install` or the model download: run
`bash ../wsl-certs/import-windows-certs.sh` once. **Still blocked?** If the download is refused, get the model folder once
from Hugging Face and pass its local path: `--model /path/to/model-folder` (same for `--reranker`).

## Use it

```bash
python docrag.py ask "which base image for a Spring Boot app?" --clip     # → Windows clipboard
python docrag.py ask "how do hardened images differ?" --out context.md    # → file to attach in Copilot
python docrag.py ask "..." -k 8 --max-chars 40000                         # more pages / bigger budget
python docrag.py ask "ERR-4012 on rhel9-base" --keywords                 # also match exact words
python docrag.py ask "..." --source "Team Support" --since 2026-01-01   # only that channel, recent threads
python docrag.py serve     # web page at http://localhost:8765 (see below)
```

### Several queries for a mixed question

Search works best with short, single-topic queries that use the exact names from the docs, and
real questions often mix several sub-questions, sometimes about a screenshot. So:

1. **Get queries from Copilot.** `python docrag.py brief "which driver for this DB connection?" --clip`
   (or **Brief for Copilot** on the web page) gives a prompt that includes your question and asks
   for 3 to 5 queries. Paste it into Copilot, with your screenshot if you have one.
2. **Run them.** Paste them into **Search queries** on the web page, or:
   ```bash
   python docrag.py ask "which driver for this DB connection?" --query "orders-db JDBC driver" --query "how do I connect to orders-db?" --clip
   python docrag.py ask "which driver for this DB connection?" --queries-file queries.txt --clip   # one per line
   ```
3. **Copy the result back to Copilot.** It opens with your question, not the queries.

The prompt is `BRIEF_TEMPLATE` at the top of `docrag.py`; edit it there, and both the button and
`brief` follow.

With queries, the question itself isn't searched, and keyword matching turns on, since exact names
are the point (`--no-keywords` or the **Match** switch to turn it off). Without them, `ask` works
as before. `ask` lists which queries found each page on stderr, like `Page title [1,3]`.

The output starts with your question (so Copilot uses it as the conversation title), then a
short instruction ("answer only from these pages, cite title and URL"), then the pages.
Paste it as-is.

Filters on the command line:
- `--source TEXT`: keep pages whose channel, doc name or file name contains TEXT.
- `--since YYYY-MM-DD`: keep chat threads active on or after that day. Docs are not affected.

## The web page

`python docrag.py serve`, then open http://localhost:8765. It only listens on this machine
(`--host 0.0.0.0` to change that).

- **Question**: what you want Copilot to answer, and the first line of the copy. Enter searches.
- **Brief for Copilot** copies a prompt asking Copilot to write search queries for your question.
- **Search queries**: optional, one per line. Enter searches, Shift+Enter starts a new line. Empty
  means the question is the search. The button shows how many will run. Filling this field turns
  **Match** on; **Clear** empties it and restores **Match** if that was the only reason it was on.
- **Sources**: type to find a doc (named after its export file) or a Mattermost channel (by
  name or `team/channel`; accents and case don't matter), Enter or click to add it. Pick
  several to combine them, or "All documentation" / "All Mattermost channels". Backspace
  removes the last one.
- **Chat since**: only chat threads with activity on or after that date. Docs always pass.
- **Match**: "Meaning" (default) ranks by what the question means. "Meaning + keywords" also
  ranks by the exact words, for identifiers, error codes or image names.
- **Pages** and **Size**: how many pages, and the total size budget they share.
- **Results**: with several queries, the numbered badges on a result show which queries found it
  (listed above the results, full text on hover). A notice says when a query contributed no page.
  Untick a result to leave it out of the copy. The bar above shows how full the
  copy is, one segment per page (blue for docs, ochre for chat). "Preview" shows exactly
  the text that will be copied.
- **Copy for Copilot** (or Ctrl+Enter) copies the question, the instruction and the ticked pages.
- Filters are remembered, and the address bar holds the whole search, so you can bookmark one.
  Queries are remembered together with their question, and a URL with a search in it takes priority.
  Light and dark follow your Windows theme.

The page talks to two endpoints you can also script against: `GET /api/sources` and
`GET /api/search?q=…&query=…&query=…&k=5&size=24000&since=YYYY-MM-DD&keywords=0|1&rerank=0|1&src=docs:<file>&src=chat:<team/channel>`
(`src=docs:*` / `src=chat:*` for all of a kind). `q` is the question and is required. `query` is
optional and repeatable, and implies `keywords=1` unless `keywords` is given. The response lists
the `queries` that ran and any `notices`, and each result has `found_by` (0-based query positions).

## How it works

- **The page is the unit of output**, and you always get `-k` pages. They share the size budget
  (`--max-chars`, or the size field on the web page, default 24,000 characters ≈ 6k tokens).
  Small pages get what they need first, and the space left goes to the bigger ones. A page that fits its share is sent whole. Otherwise only its most relevant sections
  are sent, marked "(relevant extracts)". Raise the budget to get long chat threads whole.
- **Search runs on sections, not whole pages.** Sections are capped at 512 tokens, so
  embedding a whole page would ignore most of it. Each page is cut on its headings for
  matching, and the hits are grouped back by page.
- **Meaning first.** By default sections are ranked by embedding similarity only, which follows
  what a question means rather than which words it uses. With keywords on (`--keywords`, or the
  **Match** switch), BM25 also ranks them on exact terms (image names like `rhel9-base`,
  property keys, error codes) and both rankings are merged. On a public benchmark of natural
  questions over Q&A threads, keyword matching and the keyword-trained reranker both pulled
  results away from the meaning, hence this default.
- **Reranking** then re-reads the 20 best sections with the question. Its order is blended
  with the first one, so it refines the ranking rather than overriding it.
- **Several queries are ranked separately and then merged.** Each query gets its own ranking and
  reranking with the same filters, because reranker scores can't be compared across queries.
  Pages are merged by reciprocal rank fusion and deduplicated, then `-k` and the size budget apply
  to the merged list, so a page found by several queries ranks higher. "Found by" means the page is
  in that query's top 10 pages (or top `k`, if higher), not that it matched exactly, and in a small
  index most pages qualify. A sub-question can lose all its pages to pages shared by other queries:
  the notice tells you when, and raising **Pages** brings them back.
- **Several queries share the reranking.** Sections re-read per query: 20 for 1 or 2 queries, 13 for
  3, 8 for 5, 5 for 8 (at least `k * 3` in total), so the cost stays near two single searches.
  At most 8 queries run; extra ones are left out, with a notice.
- **Doc titles lose the site name** that doc sites append to every page ("Page - Site"),
  when every page of an export has the same suffix.
- **Markdown links are reduced to their label** (`[Java Images](https://…)` → `Java Images`).
  This saves a lot of context and keeps navigation menus from polluting results.
  Each page's source URL is kept.

Tunables at the top of the script: `MAX_CHARS` (default budget), `MAX_CHUNK`, `MIN_CHUNK`,
`MAX_QUERIES`, `RERANK_TOTAL`, `RERANK_MIN_PER_QUERY`, `FOUND_BY_DEPTH`.

Tests (no models needed): `python -m unittest test_docrag`.
