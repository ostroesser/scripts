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

A file without page blocks is treated as a single page. Re-run `ingest` whenever you re-scrape.
The index goes to `./docrag_index` (change it with `DOCRAG_INDEX=/some/dir`).

The first run downloads the embedding model (~470 MB, `intfloat/multilingual-e5-small`, FR+EN).
**Behind a corporate proxy?** If the download is blocked, get the model folder once
from Hugging Face and pass its local path: `--model /path/to/multilingual-e5-small`.

## Use it

```bash
python docrag.py ask "which base image for a Spring Boot app?" --clip     # → Windows clipboard
python docrag.py ask "how do hardened images differ?" --out context.md    # → file to attach in Copilot
python docrag.py ask "..." -k 8 --max-chars 40000                         # more / bigger context
python docrag.py serve     # browser page at http://localhost:8765 with a "Copy for Copilot" button
```

The output is a short instruction ("answer only from these pages, cite title and URL"),
then the question, then the pages. Paste it as-is.

## How it works

- **The page is the unit of output.** Pages up to ~8,000 characters are sent whole. Longer
  pages are sent as their 3 best-matching sections, marked "(relevant extracts)".
- **Search runs on sections, not whole pages.** The embedding model only reads ~512 tokens, so
  embedding a whole page would ignore most of it. Each page is cut on its headings for
  matching, and the hits are grouped back by page.
- **Hybrid search.** Exact terms (image names like `rhel9-base`, property keys, error codes) are
  matched by keywords, and paraphrased questions by meaning.
- **Markdown links are reduced to their label** (`[Java Images](https://…)` → `Java Images`).
  This saves a lot of context and keeps navigation menus from polluting results.
  Each page's source URL is kept.

Tunables at the top of the script: `FULL_PAGE`, `MAX_CHUNK`, `MIN_CHUNK`.
