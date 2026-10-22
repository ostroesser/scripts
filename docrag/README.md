# docrag — find the right doc pages for Copilot

Local index over your documentation export. You ask a question, and it returns the most
relevant **documentation pages** (title + source URL), ready to paste or attach in Copilot Chat.
No LLM, nothing leaves your machine.

## Setup (WSL, once)

```bash
python3 -m venv ~/docrag-venv && source ~/docrag-venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU-only torch, much smaller
pip install -U "sentence-transformers>=2.7" "transformers>=4.51" numpy
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

To merge existing indexes into one without recomputing, ingest all the files into the new
index and point `--reuse` at the old ones:

```bash
DOCRAG_INDEX=./idx-all python docrag.py ingest docs/*.txt mm_export/*.jsonl --reuse ./idx-docs --reuse ./idx-chat
```

The index goes to `./docrag_index` (change it with `DOCRAG_INDEX=/some/dir`).

## Models

| Role | Default | Why |
|---|---|---|
| Embedding (`--model` at ingest) | `Qwen/Qwen3-Embedding-0.6B` | Best open model under 1B parameters for English retrieval (MTEB v2 retrieval 61.8, vs ~54 for the best small models). Apache 2.0. |
| Reranker (`--reranker`) | `Qwen/Qwen3-Reranker-0.6B` | Re-reads the 20 best sections of each search and reorders them. Apache 2.0. |

- **Indexing is CPU-heavy** with this model: the first full ingest of a large chat history can
  take an hour or more on a laptop. Later ingests only embed new or changed sections.
- **An existing index keeps the model it was built with**, so changing the model means a full
  re-ingest (vectors from different models can't be mixed). To stay on a lighter model:
  `ingest --model BAAI/bge-small-en-v1.5` (fast, English) or `--model intfloat/multilingual-e5-small`.
- **Reranking** adds a few seconds per search on CPU. Turn it off per search with the
  **Ranking** switch on the web page (Fast/Precise) or `ask --no-rerank`, or entirely with
  `serve --no-rerank`. If the reranker can't be loaded, searches still work, with a warning.
- Models are downloaded on first use (about 1.2 GB each). After that they load from the local
  cache in offline mode, without contacting Hugging Face. `DOCRAG_OFFLINE=1` forbids downloads.
**Certificate errors** during `pip install` or the model download: run
`bash ../wsl-certs/import-windows-certs.sh` once. **Still blocked?** If the download is refused, get the model folder once
from Hugging Face and pass its local path: `--model /path/to/model-folder` (same for `--reranker`).

## Use it

```bash
python docrag.py ask "which base image for a Spring Boot app?" --clip     # → Windows clipboard
python docrag.py ask "how do hardened images differ?" --out context.md    # → file to attach in Copilot
python docrag.py ask "..." -k 8 --max-chars 40000                         # more pages / bigger budget
python docrag.py ask "..." --source "Team Support" --since 2026-01-01   # only that channel, recent threads
python docrag.py serve     # web page at http://localhost:8765 (see below)
```

The output starts with your question (so Copilot uses it as the conversation title), then a
short instruction ("answer only from these pages, cite title and URL"), then the pages.
Paste it as-is.

Filters on the command line:
- `--source TEXT`: keep pages whose channel, doc name or file name contains TEXT.
- `--since YYYY-MM-DD`: keep chat threads active on or after that day. Docs are not affected.

## The web page

`python docrag.py serve`, then open http://localhost:8765. It only listens on this machine
(`--host 0.0.0.0` to change that).

- **Sources**: type to find a doc (named after its export file) or a Mattermost channel (by
  name or `team/channel`; accents and case don't matter), Enter or click to add it. Pick
  several to combine them, or "All documentation" / "All Mattermost channels". Backspace
  removes the last one.
- **Chat since**: only chat threads with activity on or after that date. Docs always pass.
- **Pages** and **Size**: how many pages, and the total size budget they share.
- **Results**: untick a result to leave it out of the copy. The bar above shows how full the
  copy is, one segment per page (blue for docs, ochre for chat). "Preview" shows exactly
  the text that will be copied.
- **Copy for Copilot** (or Ctrl+Enter) copies the question, the instruction and the ticked pages.
- Filters are remembered, and the address bar holds the whole search, so you can bookmark one.
  Light and dark follow your Windows theme.

The page talks to two endpoints you can also script against: `GET /api/sources` and
`GET /api/search?q=…&k=5&size=24000&since=YYYY-MM-DD&src=docs:<file>&src=chat:<team/channel>`
(`src=docs:*` / `src=chat:*` for all of a kind).

## How it works

- **The page is the unit of output**, and you always get `-k` pages. They share the size budget
  (`--max-chars`, or the size field on the web page, default 24,000 characters ≈ 6k tokens).
  Small pages get what they need first, and the space left goes to the bigger ones. A page that fits its share is sent whole. Otherwise only its most relevant sections
  are sent, marked "(relevant extracts)". Raise the budget to get long chat threads whole.
- **Search runs on sections, not whole pages.** Sections are capped at 512 tokens, so
  embedding a whole page would ignore most of it. Each page is cut on its headings for
  matching, and the hits are grouped back by page.
- **Hybrid search.** Exact terms (image names like `rhel9-base`, property keys, error codes) are
  matched by keywords, and paraphrased questions by meaning. The reranker then re-reads the
  20 best sections with the question and puts the ones that actually answer it first.
- **Doc titles lose the site name** that doc sites append to every page ("Page - Site"),
  when every page of an export has the same suffix.
- **Markdown links are reduced to their label** (`[Java Images](https://…)` → `Java Images`).
  This saves a lot of context and keeps navigation menus from polluting results.
  Each page's source URL is kept.

Tunables at the top of the script: `MAX_CHARS` (default budget), `MAX_CHUNK`, `MIN_CHUNK`.
