# docrag — find the right doc passages for Copilot

Local index over your scraped markdown. You ask a question, it returns the ~8 most relevant
sections (with their heading path), ready to paste or attach in Copilot Chat.
No LLM, nothing leaves your machine.

## Setup (WSL, once)

```bash
python3 -m venv ~/docrag-venv && source ~/docrag-venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU-only torch, much smaller
pip install sentence-transformers numpy
```

## Build the index

```bash
python docrag.py ingest /path/to/scraped/*.md
```

The first run downloads the embedding model (~470 MB, `intfloat/multilingual-e5-small`, FR+EN).
Re-run `ingest` whenever you re-scrape. The index goes to `./docrag_index`
(change it with `DOCRAG_INDEX=/some/dir`).

**Behind a corporate proxy?** If the model download is blocked, download the model folder once
elsewhere (from Hugging Face) and pass its local path: `--model /path/to/multilingual-e5-small`.

## Use it

```bash
python docrag.py ask "what does error PGW-4012 mean?" --clip        # → Windows clipboard
python docrag.py ask "how is mutual TLS configured?" --out context.md  # → file to attach in Copilot
python docrag.py ask "..." -k 12 --max-chars 30000                  # more / bigger context
python docrag.py serve        # browser page at http://localhost:8765 with a "Copy for Copilot" button
```

The output starts with a short instruction ("answer only from these excerpts, cite the section"),
then the question, then the excerpts. Paste it as-is.

## Tips

- Your scraper's markdown headings (`#`, `##`, `###`) drive the chunking, so keep them.
  Code blocks are never split on `#` comments.
- Search is hybrid: exact terms (class names, property keys like `gateway.retry.maxAttempts`,
  error codes) are matched by keywords, and paraphrased questions by meaning. Mixing both in
  your question works best.
- If an answer seems to miss context, raise `-k`, or ask a narrower follow-up question.
