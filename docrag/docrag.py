#!/usr/bin/env python3
"""
docrag — local retrieval over your scraped docs, for pasting into Copilot Chat.

  python docrag.py ingest export/*.txt mm_export/*.jsonl   # (re)build the index; unchanged parts are reused
  python docrag.py ask "which base image for a Java 21 app?" --clip
  python docrag.py ask "..." --out context.md # file to attach in Copilot
  python docrag.py ask "..." --source "Team Support" --since 2026-01-01   # filter chat threads
  python docrag.py serve                      # small web page at http://localhost:8765

The page is the unit of output. Inputs are text exports where the page blocks
(=====  DOCUMENT PAGE n / Title: / Source: =====) are parsed, or .jsonl files
with one page per line (title, url, text, optional date and source), such as
mmscrape's chat threads. Results are whole pages with their title and URL. Pages too big to send
whole are cut on their headings, and only the matching parts are sent.

Search is hybrid: BM25 for exact terms (image names, properties, error codes)
plus local embeddings for meaning, merged with reciprocal rank fusion.
Matching runs on sections, so long pages are not truncated by the embedding
model. Nothing leaves your machine, and no LLM is involved.
"""
import argparse, html, json, math, os, re, subprocess, sys, tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

INDEX_DIR = Path(os.environ.get("DOCRAG_INDEX", "./docrag_index"))
DEFAULT_MODEL = "intfloat/multilingual-e5-small"   # FR+EN, runs fine on CPU
MAX_CHUNK = 1800       # chars per searchable section (the model reads ~512 tokens)
MIN_CHUNK = 250        # tiny sections get merged with their neighbour
FULL_PAGE = 8000       # pages up to this size are returned whole (if the budget allows)
MAX_CHARS = 24000      # default total size budget of one answer (~6k tokens)

# ---------------------------------------------------------------- parsing
PAGE_BLOCK = re.compile(
    r"^={10,}[ \t]*\r?\n"
    r"DOCUMENT PAGE[ \t]*(\d+)[ \t]*\r?\n"
    r"Title:[ \t]*(.*?)[ \t]*\r?\n"
    r"Source:[ \t]*(.*?)[ \t]*\r?\n"
    r"={10,}[ \t]*$", re.M)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^()\s]|\([^()]*\))+\)")


def strip_links(text):
    """[label](https://very/long/url) -> label. Saves context and keeps nav menus out of the scoring."""
    return MD_LINK.sub(r"\1", text)


def parse_pages(text, source):
    """Split one export file into pages. If no page blocks are found, the whole file is one page."""
    marks = list(PAGE_BLOCK.finditer(text))
    if not marks:
        return [{"title": Path(source).stem, "url": "", "file": source, "text": text.strip()}]
    pages = []
    for m, nxt in zip(marks, marks[1:] + [None]):
        body = text[m.end(): nxt.start() if nxt else len(text)].strip()
        title = m.group(2)
        if body:
            pages.append({"title": title, "url": m.group(3), "file": source, "text": body})
    return pages


def split_sections(text):
    """Split markdown on headings (ignoring '#' inside code fences), keeping the heading path."""
    stack, sections, buf, in_code = [], [], [], False

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append({"path": " > ".join(t for _, t in stack), "text": body})
        buf.clear()

    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2)))
            buf.append(line)                  # keep the heading in the text sent to Copilot
        else:
            buf.append(line)
    flush()
    return sections


def split_long(text, limit=MAX_CHUNK):
    """Cut long text on blank lines without breaking code fences; hard-cut only if unavoidable."""
    blocks, cur, in_code = [], [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        cur.append(line)
        if not in_code and line.strip() == "":
            blocks.append("\n".join(cur)); cur = []
    if cur:
        blocks.append("\n".join(cur))
    out, acc = [], ""
    for b in blocks:
        if len(acc) + len(b) > limit and acc.strip():
            out.append(acc.strip()); acc = ""
        while len(b) > limit * 1.5:            # one giant block (e.g. a huge code listing)
            out.append(b[:limit].strip()); b = b[limit:]
        acc += b + "\n"
    if acc.strip():
        out.append(acc.strip())
    return out


def page_sections(page_text):
    """Searchable sections of one page: split on headings, small ones merged, long ones cut."""
    parts, acc, acc_path = [], "", None
    for sec in split_sections(page_text):
        if acc and len(acc) + len(sec["text"]) > MAX_CHUNK and len(acc) >= MIN_CHUNK:
            parts.append((acc_path, acc)); acc, acc_path = "", None
        acc_path = acc_path if acc_path is not None else sec["path"]
        acc = f"{acc}\n\n{sec['text']}" if acc else sec["text"]
    if acc:
        parts.append((acc_path, acc))
    out = []
    for path, text in parts:
        for piece in split_long(text):
            out.append({"path": path or "", "text": piece})
    return out


def parse_jsonl(text, source):
    """One JSON record per line: {"title", "url", "text", optional "date" (YYYY-MM-DD…), "source"}."""
    pages = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("text", "").strip():
            pages.append({"title": r.get("title") or f"{source} #{n}", "url": r.get("url", ""),
                          "file": source, "text": r["text"].strip(),
                          "date": (r.get("date") or "")[:10], "source": r.get("source") or source})
    return pages


def build(files):
    pages, chunks = [], []
    for f in files:
        raw = Path(f).read_text(encoding="utf-8", errors="replace")
        parsed = parse_jsonl(raw, Path(f).name) if f.endswith(".jsonl") else parse_pages(raw, Path(f).name)
        for p in parsed:
            p.setdefault("date", "")
            p.setdefault("source", p["file"])
            p["text"] = strip_links(p["text"])
            p["id"] = len(pages)
            pages.append(p)
            for s in page_sections(p["text"]):
                chunks.append({"page": p["id"], "path": s["path"], "text": s["text"]})
    return pages, chunks

# ---------------------------------------------------------------- search
TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")


def tokenize(text):
    toks = []
    for t in TOKEN.findall(text.lower()):
        toks.append(t)
        if "." in t or "_" in t or "-" in t:   # also index the parts of dotted.names / rhel9-base
            toks.extend(p for p in re.split(r"[._\-]", t) if p)
    return toks


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.tfs = [{} for _ in docs]
        df = {}
        for tf, d in zip(self.tfs, docs):
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            for t in tf:
                df[t] = df.get(t, 0) + 1
        self.lens = np.array([len(d) for d in docs], dtype=float)
        self.avg = self.lens.mean() if len(docs) else 1
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, q):
        s = np.zeros(len(self.tfs))
        norm = self.k1 * (1 - self.b + self.b * self.lens / self.avg)
        for t in set(q):
            if t not in self.idf:
                continue
            tf = np.array([d.get(t, 0) for d in self.tfs], dtype=float)
            s += self.idf[t] * tf * (self.k1 + 1) / (tf + norm)
        return s


class Index:
    def __init__(self, path=INDEX_DIR):
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        self.pages = json.loads((path / "pages.json").read_text(encoding="utf-8"))
        self.chunks = json.loads((path / "chunks.json").read_text(encoding="utf-8"))
        self.emb = np.load(path / "embeddings.npy")
        self.model_name = meta["model"]
        self.page_chunks = {}
        for c in self.chunks:
            self.page_chunks[c["page"]] = self.page_chunks.get(c["page"], 0) + 1
        self.bm25 = BM25([tokenize(self.chunk_header(c) + " " + c["text"]) for c in self.chunks])
        self._model = None

    def chunk_header(self, c):
        return f"{self.pages[c['page']]['title']} {c['path']}"

    @property
    def model(self):
        if self._model is None:
            self._model = load_model(self.model_name)
        return self._model

    def search(self, question, k=5, max_chars=None, since=None, source=None):
        """Return up to k pages: whole when small enough, else only their matching sections.
        since: keep only pages dated on/after YYYY-MM-DD (undated pages are dropped).
        source: keep only pages whose source or file name contains this text."""
        max_chars = max_chars or MAX_CHARS
        allowed = np.ones(len(self.chunks), dtype=bool)
        if since or source:
            for i, c in enumerate(self.chunks):
                p = self.pages[c["page"]]
                if since and not (p.get("date") and p["date"] >= since):
                    allowed[i] = False
                elif source and source.lower() not in f"{p.get('source', '')} {p['file']}".lower():
                    allowed[i] = False
        idx = np.flatnonzero(allowed)
        if not len(idx):
            return []
        bm = self.bm25.scores(tokenize(question))[idx]
        qv = embed(self.model, self.model_name, [question], is_query=True)[0]
        vec = self.emb[idx] @ qv
        rrf = np.zeros(len(idx))               # reciprocal rank fusion of both rankings
        for scores in (bm, vec):
            order = np.argsort(-scores)
            rrf[order] += 1.0 / (60 + np.arange(1, len(idx) + 1))

        hits = {}                              # k best pages -> all their chunks, most relevant first
        for i in idx[np.argsort(-rrf)]:
            pid = self.chunks[i]["page"]
            if pid not in hits:
                if len(hits) >= k:
                    continue
                hits[pid] = []
            hits[pid].append(int(i))

        # Share the size budget: every page gets (remaining budget / remaining pages), so small
        # pages leave room for the next ones. A page bigger than its share (or than FULL_PAGE)
        # is cut down to its most relevant sections instead of being dropped.
        results, remaining = [], max_chars
        for n, (pid, cids) in enumerate(hits.items()):
            page = self.pages[pid]
            limit = min(FULL_PAGE, remaining // (len(hits) - n))
            if len(page["text"]) <= limit:
                body, whole = page["text"], True
            else:
                chosen, size = [], 0
                for c in cids:
                    t = len(self.chunks[c]["text"]) + 7
                    if chosen and size + t > limit:
                        continue
                    chosen.append(c); size += t
                whole = len(chosen) == self.page_chunks[pid]
                body = page["text"] if whole else "\n\n[…]\n\n".join(self.chunks[c]["text"] for c in sorted(chosen))
            results.append({**page, "text": body, "whole": whole})
            remaining -= len(body)
        return results

# ---------------------------------------------------------------- embeddings
def text_hash(t):
    import hashlib
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def reuse_embeddings(texts, model_name, path=INDEX_DIR):
    """Vectors from the previous index for identical text (same model); None where a new one is needed."""
    try:
        if json.loads((path / "meta.json").read_text(encoding="utf-8"))["model"] != model_name:
            return [None] * len(texts)
        old_chunks = json.loads((path / "chunks.json").read_text(encoding="utf-8"))
        old_emb = np.load(path / "embeddings.npy")
        cache = {c["hash"]: old_emb[i] for i, c in enumerate(old_chunks) if "hash" in c}
    except (OSError, KeyError, ValueError):
        return [None] * len(texts)
    return [cache.get(text_hash(t)) for t in texts]


def load_model(name):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name)


def embed(model, name, texts, is_query=False):
    if "e5" in name.lower():                   # e5 models expect these prefixes
        texts = [("query: " if is_query else "passage: ") + t for t in texts]
    v = model.encode(texts, batch_size=32, show_progress_bar=not is_query, normalize_embeddings=True)
    return np.asarray(v, dtype=np.float32)

# ---------------------------------------------------------------- output
def format_context(question, results):
    parts = ["Use only the pages below (documentation and/or chat threads) to answer. Cite the page title and URL you rely on.\n"
             f"If the pages don't contain the answer, say so.\n\nQuestion: {question}\n"]
    for n, r in enumerate(results, 1):
        note = "" if r["whole"] else " (relevant extracts)"
        date = f"\nDate: {r['date']}" if r.get("date") else ""
        parts.append(f"---\n## Page {n}: {r['title']}{note}\nSource: {r['url'] or r['file']}{date}\n\n{r['text']}\n")
    return "\n".join(parts)


def to_clipboard(text):
    if sys.platform.startswith("linux") and "microsoft" in os.uname().release.lower():   # WSL
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text); tmp = f.name
        win = subprocess.check_output(["wslpath", "-w", tmp]).decode().strip()
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"Get-Content -Raw -Encoding UTF8 '{win}' | Set-Clipboard"], check=True)
        os.unlink(tmp)
    elif sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    else:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)

# ---------------------------------------------------------------- web page
PAGE = """<!doctype html><meta charset=utf-8><title>docrag</title>
<style>body{font:15px system-ui;max-width:900px;margin:2em auto;padding:0 1em}
input{width:75%;font-size:16px;padding:6px}button{font-size:16px;padding:6px 12px}
textarea{width:100%;height:65vh;font:13px ui-monospace,monospace;margin-top:1em}</style>
<form><input name=q value="{q}" autofocus placeholder="Ask about the docs…">
<input name=k type=number value="{k}" style="width:4em" title="number of pages"><br>
<input name=size type=number value="{size}" step=1000 style="width:7em" title="total size budget (characters)">
<input name=since value="{since}" placeholder="since YYYY-MM-DD" style="width:11em">
<input name=source value="{source}" placeholder="source filter (e.g. channel)" style="width:16em">
<button>Search</button></form>
<textarea id=out>{ctx}</textarea>
<button onclick="navigator.clipboard.writeText(document.getElementById('out').value);this.textContent='Copied ✓'">Copy for Copilot</button>
<span style="color:#888;margin-left:1em">{info}</span>"""


def serve(index, port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            q, k = qs.get("q", [""])[0], int(qs.get("k", ["5"])[0] or 5)
            since, source = qs.get("since", [""])[0].strip(), qs.get("source", [""])[0].strip()
            size = int(qs.get("size", [str(MAX_CHARS)])[0] or MAX_CHARS)
            ctx, info = "", f"{len(index.pages)} pages, {len(index.chunks)} sections indexed"
            if q:
                res = index.search(q, k=k, max_chars=size, since=since or None, source=source or None)
                ctx = format_context(q, res)
                info = f"{len(res)} pages · {len(ctx):,} chars · " + " | ".join(r["title"] for r in res)
            body = PAGE.replace("{q}", html.escape(q)).replace("{k}", str(k)) \
                       .replace("{size}", str(size)).replace("{since}", html.escape(since)).replace("{source}", html.escape(source)) \
                       .replace("{ctx}", html.escape(ctx)).replace("{info}", html.escape(info))
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers(); self.wfile.write(body.encode())

        def log_message(self, *a):
            pass
    print(f"docrag on http://localhost:{port}  (Ctrl+C to stop)")
    HTTPServer(("0.0.0.0", port), H).serve_forever()

# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="build the index from export files")
    i.add_argument("files", nargs="+")
    i.add_argument("--model", default=DEFAULT_MODEL)
    a = sub.add_parser("ask", help="retrieve pages for a question")
    a.add_argument("question")
    a.add_argument("-k", type=int, default=5, help="number of pages (default 5)")
    a.add_argument("--max-chars", type=int, default=MAX_CHARS, help="total size budget (characters)")
    a.add_argument("--since", help="only pages dated on/after YYYY-MM-DD (e.g. chat threads)")
    a.add_argument("--source", help="only pages whose source/file contains this text")
    a.add_argument("--out", help="write to a file (e.g. context.md) to attach in Copilot")
    a.add_argument("--clip", action="store_true", help="copy to the Windows clipboard")
    s = sub.add_parser("serve", help="web page with a copy button")
    s.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.cmd == "ingest":
        pages, chunks = build(args.files)
        print(f"{len(pages)} pages, {len(chunks)} sections from {len(args.files)} file(s); "
              f"embedding with {args.model} …")
        titles = {p["id"]: p["title"] for p in pages}
        texts = [f"{titles[c['page']]} > {c['path']}\n{c['text']}" for c in chunks]
        emb = reuse_embeddings(texts, args.model)          # unchanged sections keep their vectors
        todo = [i for i, v in enumerate(emb) if v is None]
        print(f"{len(chunks) - len(todo)} sections unchanged, {len(todo)} to embed")
        if todo:
            new = embed(load_model(args.model), args.model, [texts[i] for i in todo])
            for i, v in zip(todo, new):
                emb[i] = v
        emb = np.vstack(emb).astype(np.float32) if emb else np.zeros((0, 1), dtype=np.float32)
        for c, t in zip(chunks, texts):
            c["hash"] = text_hash(t)
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.save(INDEX_DIR / "embeddings.npy", emb)
        (INDEX_DIR / "pages.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        (INDEX_DIR / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        (INDEX_DIR / "meta.json").write_text(json.dumps({"model": args.model}), encoding="utf-8")
        print(f"Index written to {INDEX_DIR.resolve()}")
    elif args.cmd == "ask":
        idx = Index()
        res = idx.search(args.question, args.k, args.max_chars, since=args.since, source=args.source)
        ctx = format_context(args.question, res)
        print("Pages: " + " | ".join(r["title"] for r in res), file=sys.stderr)
        if args.out:
            Path(args.out).write_text(ctx, encoding="utf-8"); print(f"Wrote {args.out} ({len(ctx):,} chars)")
        if args.clip:
            to_clipboard(ctx); print(f"Copied to clipboard ({len(ctx):,} chars)")
        if not (args.out or args.clip):
            print(ctx)
    else:
        serve(Index(), args.port)


if __name__ == "__main__":
    main()
