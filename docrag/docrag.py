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
    # Doc sites append their name to every title ("Page - Site"): drop a suffix shared by all pages.
    if len(pages) >= 2:
        sfx = re.search(r"\s+[-|\u2013\u2014]\s+[^-|\u2013\u2014]+$", pages[0]["title"])
        if sfx and all(p["title"].endswith(sfx.group(0)) and p["title"] != sfx.group(0).strip() for p in pages):
            for p in pages:
                p["title"] = p["title"][: -len(sfx.group(0))]
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
    # split on "\n" only: splitlines() also breaks on U+2028/U+2029/U+0085, which JSON keeps
    # unescaped inside strings (e.g. text pasted from Word/Outlook)
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"warning: {source} line {n} skipped, not valid JSON ({e.msg})", file=sys.stderr)
            continue
        if r.get("text", "").strip():
            pages.append({"title": r.get("title") or f"{source} #{n}", "url": r.get("url", ""),
                          "file": source, "text": r["text"].strip(),
                          "date": (r.get("date") or "")[:10], "source": r.get("source") or source,
                          "channel": r.get("channel") or ""})
    return pages


def readable(file_name):
    """'BaseContainerImages.txt' -> 'Base Container Images', 'my_product-docs.txt' -> 'my product docs'."""
    stem = Path(file_name).stem
    stem = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", stem)
    return re.sub(r"[_\-]+", " ", stem).strip() or file_name


def page_meta(p):
    """Fill in what the source filter needs: kind (docs/chat), group and its display label.
    Docs are grouped by export file, chat threads by channel."""
    if p.get("kind"):
        return p
    if p["file"].endswith(".jsonl"):
        p["kind"] = "chat"
        p["group"] = p.get("channel") or p.get("source") or Path(p["file"]).stem
        p["label"] = p.get("source") or p["group"]
    else:
        p["kind"] = "docs"
        p["group"] = Path(p["file"]).stem
        p["label"] = readable(p["file"])
    return p


def build(files):
    pages, chunks = [], []
    for f in files:
        raw = Path(f).read_text(encoding="utf-8", errors="replace")
        parsed = parse_jsonl(raw, Path(f).name) if f.endswith(".jsonl") else parse_pages(raw, Path(f).name)
        for p in parsed:
            p.setdefault("date", "")
            p.setdefault("source", p["file"])
            page_meta(p)
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
        self.pages = [page_meta(p) for p in json.loads((path / "pages.json").read_text(encoding="utf-8"))]
        self.chunks = json.loads((path / "chunks.json").read_text(encoding="utf-8"))
        self.emb = np.load(path / "embeddings.npy")
        self.model_name = meta["model"]
        self.page_chunks = {}
        for c in self.chunks:
            self.page_chunks[c["page"]] = self.page_chunks.get(c["page"], 0) + 1
        self.bm25 = BM25([tokenize(self.chunk_header(c) + " " + c["text"]) for c in self.chunks])
        self._model = None

    def sources(self):
        """Filterable sources: one entry per docs export file and per chat channel."""
        out = {}
        for p in self.pages:
            key = f"{p['kind']}:{p['group']}"
            if key not in out:
                out[key] = {"key": key, "kind": p["kind"], "group": p["group"], "label": p["label"], "count": 0}
            out[key]["count"] += 1
        return sorted(out.values(), key=lambda x: (x["kind"] != "docs", x["label"].lower()))

    def chunk_header(self, c):
        return f"{self.pages[c['page']]['title']} {c['path']}"

    @property
    def model(self):
        if self._model is None:
            self._model = load_model(self.model_name)
        return self._model

    def search(self, question, k=5, max_chars=None, since=None, source=None, groups=None):
        """Return up to k pages: whole when small enough, else only their matching sections.
        since:  chat threads only: keep those active on/after YYYY-MM-DD (docs are not affected).
        source: keep pages whose source, label or file name contains this text.
        groups: keep pages of these sources ("docs:<file>", "chat:<team/channel>", or
                "docs:*" / "chat:*" for all of a kind); several are combined with OR."""
        max_chars = max_chars or MAX_CHARS
        groups = set(groups or ())
        page_ok = []
        for p in self.pages:
            ok = True
            if since and p["kind"] == "chat" and not (p.get("date") and p["date"] >= since):
                ok = False
            elif source and source.lower() not in f"{p.get('source', '')} {p['label']} {p['file']}".lower():
                ok = False
            elif groups and f"{p['kind']}:{p['group']}" not in groups and f"{p['kind']}:*" not in groups:
                ok = False
            page_ok.append(ok)
        allowed = np.array([page_ok[c["page"]] for c in self.chunks], dtype=bool)
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

        # Share the size budget: pages are filled smallest first, each getting an equal share of
        # what's left, so unused space flows on to the bigger ones. A page bigger than its share
        # is cut down to its most relevant sections instead of being dropped.
        # Results keep their relevance order.
        bodies, remaining = {}, max_chars
        by_size = sorted(hits, key=lambda pid: len(self.pages[pid]["text"]))
        for n, pid in enumerate(by_size):
            page, limit = self.pages[pid], remaining // (len(by_size) - n)
            if len(page["text"]) <= limit:
                bodies[pid] = (page["text"], True)
            else:
                chosen, size = [], 0
                for c in hits[pid]:
                    t = len(self.chunks[c]["text"]) + 7
                    if chosen and size + t > limit:
                        continue
                    chosen.append(c); size += t
                whole = len(chosen) == self.page_chunks[pid]
                bodies[pid] = (page["text"] if whole else
                               "\n\n[…]\n\n".join(self.chunks[c]["text"] for c in sorted(chosen)), whole)
            remaining -= len(bodies[pid][0])
        results = [{**self.pages[pid], "text": bodies[pid][0], "whole": bodies[pid][1]} for pid in hits]
        return results

# ---------------------------------------------------------------- embeddings
def text_hash(t):
    import hashlib
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def reuse_embeddings(texts, model_name, paths=(INDEX_DIR,)):
    """Vectors for identical text from existing indexes (same model only); None where a new one is
    needed. `paths`: the index being rebuilt, plus any other index to take vectors from."""
    cache = {}
    for path in paths:
        path = Path(path)
        try:
            if json.loads((path / "meta.json").read_text(encoding="utf-8"))["model"] != model_name:
                continue
            old_chunks = json.loads((path / "chunks.json").read_text(encoding="utf-8"))
            old_emb = np.load(path / "embeddings.npy")
        except (OSError, KeyError, ValueError):
            continue
        for i, c in enumerate(old_chunks):
            if "hash" in c:
                cache.setdefault(c["hash"], old_emb[i])
    return [cache.get(text_hash(t)) for t in texts]


def model_is_cached(name):
    """True if the model is a local folder or already in the Hugging Face cache."""
    if Path(name).is_dir():
        return True
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"), "hub")
    snapshots = Path(hub) / ("models--" + name.replace("/", "--")) / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


def load_model(name):
    """Load the embedding model without touching the network when it's already on disk.
    Downloads it only if missing, unless DOCRAG_OFFLINE=1."""
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    cached = model_is_cached(name)
    if cached:                                  # must be set before the HF libraries are imported
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    elif os.environ.get("DOCRAG_OFFLINE") == "1":
        sys.exit(f"Model {name} is not downloaded and DOCRAG_OFFLINE=1. "
                 "Run once without it, or pass --model /path/to/a/local/model/folder.")
    else:
        print(f"Model {name} not found locally, downloading it from Hugging Face …", file=sys.stderr)
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name, local_files_only=cached)


def embed(model, name, texts, is_query=False):
    if "e5" in name.lower():                   # e5 models expect these prefixes
        texts = [("query: " if is_query else "passage: ") + t for t in texts]
    v = model.encode(texts, batch_size=32, show_progress_bar=not is_query, normalize_embeddings=True)
    return np.asarray(v, dtype=np.float32)

# ---------------------------------------------------------------- output
def context_header(question):
    # The question comes first, on its own line: Copilot uses the start of the message
    # as the conversation title in its sidebar.
    return (f"{question.strip()}\n\n"
            "Use only the pages below (documentation and/or chat threads) to answer the question above. "
            "Cite the page title and URL you rely on.\nIf the pages don't contain the answer, say so.\n")


def result_block(n, r):
    note = "" if r["whole"] else " (relevant extracts)"
    date = f"\nDate: {r['date']}" if r.get("date") else ""
    return f"---\n## Page {n}: {r['title']}{note}\nSource: {r['url'] or r['file']}{date}\n\n{r['text']}\n"


def format_context(question, results):
    return "\n".join([context_header(question)] + [result_block(n, r) for n, r in enumerate(results, 1)])


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
UI_DIR = Path(__file__).resolve().parent / "ui"
STATIC = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
          "/app.js": ("app.js", "application/javascript")}


def serve(index, port, host="127.0.0.1"):
    """Web UI + JSON API:
      GET /api/sources                  -> {"sources": [...], "pages": n, "sections": n}
      GET /api/search?q=&k=&size=&since=&src=<key>&src=<key>
                                        -> {"header": str, "results": [{..., "block": str}]}"""
    class H(BaseHTTPRequestHandler):
        def send(self, code, body, ctype):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, obj, code=200):
            self.send(code, json.dumps(obj, ensure_ascii=False), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            arg = lambda name, default="": (qs.get(name) or [default])[0].strip()
            try:
                if u.path == "/api/sources":
                    return self.send_json({"sources": index.sources(), "pages": len(index.pages),
                                           "sections": len(index.chunks)})
                if u.path == "/api/search":
                    q = arg("q")
                    if not q:
                        return self.send_json({"error": "empty question"}, 400)
                    res = index.search(q, k=max(1, min(int(arg("k", "5") or 5), 50)),
                                       max_chars=max(1000, int(arg("size", str(MAX_CHARS)) or MAX_CHARS)),
                                       since=arg("since") or None, groups=qs.get("src") or None)
                    return self.send_json({"header": context_header(q), "results": [
                        {"title": r["title"], "url": r["url"], "date": r.get("date", ""), "kind": r["kind"],
                         "label": r["label"], "whole": r["whole"], "chars": len(r["text"]),
                         "text": r["text"], "block": result_block(n, r)}
                        for n, r in enumerate(res, 1)]})
                if u.path in STATIC:
                    name, ctype = STATIC[u.path]
                    return self.send(200, (UI_DIR / name).read_bytes(), ctype)
                self.send(404, "not found", "text/plain")
            except Exception as e:                 # show the error in the page instead of a dead request
                self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        def log_message(self, *a):
            pass
    print(f"docrag on http://localhost:{port}  ({len(index.pages)} pages)  Ctrl+C to stop")
    HTTPServer((host, port), H).serve_forever()

# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="build the index from export files")
    i.add_argument("files", nargs="+")
    i.add_argument("--model", default=DEFAULT_MODEL)
    i.add_argument("--reuse", action="append", default=[], metavar="INDEX_DIR",
                   help="also take vectors from this index (repeatable), e.g. to merge indexes")
    a = sub.add_parser("ask", help="retrieve pages for a question")
    a.add_argument("question")
    a.add_argument("-k", type=int, default=5, help="number of pages (default 5)")
    a.add_argument("--max-chars", type=int, default=MAX_CHARS, help="total size budget (characters)")
    a.add_argument("--since", help="chat threads active on/after YYYY-MM-DD (docs are not affected)")
    a.add_argument("--source", help="only pages whose source/doc name/file contains this text")
    a.add_argument("--out", help="write to a file (e.g. context.md) to attach in Copilot")
    a.add_argument("--clip", action="store_true", help="copy to the Windows clipboard")
    s = sub.add_parser("serve", help="web page with a copy button")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1", help="listen address (default: this machine only)")
    args = ap.parse_args()

    if args.cmd == "ingest":
        pages, chunks = build(args.files)
        print(f"{len(pages)} pages, {len(chunks)} sections from {len(args.files)} file(s); "
              f"embedding with {args.model} …")
        titles = {p["id"]: p["title"] for p in pages}
        texts = [f"{titles[c['page']]} > {c['path']}\n{c['text']}" for c in chunks]
        emb = reuse_embeddings(texts, args.model, [INDEX_DIR] + args.reuse)   # unchanged sections keep their vectors
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
        serve(Index(), args.port, args.host)


if __name__ == "__main__":
    main()
