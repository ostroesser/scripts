#!/usr/bin/env python3
"""
docrag — local retrieval over your scraped markdown docs, for pasting into Copilot Chat.

  python docrag.py ingest docs/*.md          # build the index (once, or after re-scraping)
  python docrag.py ask "how do I configure the retry policy?" --clip
  python docrag.py ask "..." --out context.md # file to attach in Copilot
  python docrag.py serve                      # small web page at http://localhost:8765

Hybrid search: BM25 (exact terms: class names, error codes, properties)
+ local embeddings (meaning), merged with reciprocal rank fusion.
Nothing leaves your machine; no LLM is involved.
"""
import argparse, html, json, math, os, re, subprocess, sys, tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

INDEX_DIR = Path(os.environ.get("DOCRAG_INDEX", "./docrag_index"))
DEFAULT_MODEL = "intfloat/multilingual-e5-small"   # FR+EN, runs fine on CPU
MAX_CHUNK = 1800       # chars per chunk
MIN_CHUNK = 250        # tiny sections get merged into the next one

# ---------------------------------------------------------------- chunking
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def split_sections(text, source):
    """Split markdown on headings (ignoring '#' inside code fences), keeping the heading path."""
    stack, sections, buf, in_code = [], [], [], False

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append({"source": source, "path": " > ".join(t for _, t in stack), "text": body})
        buf.clear()

    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
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
        while len(b) > limit * 1.5:            # one giant block (e.g. huge code listing)
            out.append(b[:limit].strip()); b = b[limit:]
        acc += b + "\n"
    if acc.strip():
        out.append(acc.strip())
    return out


def make_chunks(files):
    chunks, pending = [], None
    for f in files:
        text = Path(f).read_text(encoding="utf-8", errors="replace")
        for sec in split_sections(text, Path(f).name):
            if pending:                       # merge a too-small previous section into this one
                sec["text"] = f"[{pending['path']}]\n{pending['text']}\n\n[{sec['path']}]\n{sec['text']}"
                a, b = pending["path"].split(" > "), sec["path"].split(" > ")
                common = [x for x, y in zip(a, b) if x == y]
                sec["path"] = " > ".join(common) if common else sec["path"]
                pending = None
            if len(sec["text"]) < MIN_CHUNK:
                pending = sec; continue
            for part in split_long(sec["text"]):
                chunks.append({**sec, "text": part})
    if pending:
        chunks.append(pending)
    for i, c in enumerate(chunks):
        c["id"] = i
    return chunks

# ---------------------------------------------------------------- search
TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")


def tokenize(text):
    toks = []
    for t in TOKEN.findall(text.lower()):
        toks.append(t)
        if "." in t or "_" in t or "-" in t:   # also index the parts of dotted.names / snake_case
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
        self.chunks = json.loads((path / "chunks.json").read_text(encoding="utf-8"))
        self.emb = np.load(path / "embeddings.npy")
        self.model_name = meta["model"]
        self.bm25 = BM25([tokenize(c["path"] + " " + c["text"]) for c in self.chunks])
        self._model = None

    @property
    def model(self):
        if self._model is None:
            self._model = load_model(self.model_name)
        return self._model

    def search(self, question, k=8, max_chars=20000):
        n = len(self.chunks)
        bm = self.bm25.scores(tokenize(question))
        qv = embed(self.model, self.model_name, [question], is_query=True)[0]
        vec = self.emb @ qv
        # reciprocal rank fusion
        rrf = np.zeros(n)
        for scores in (bm, vec):
            order = np.argsort(-scores)
            rrf[order] += 1.0 / (60 + np.arange(1, n + 1))
        results, total = [], 0
        for i in np.argsort(-rrf)[: k * 3]:
            c = self.chunks[i]
            if total + len(c["text"]) > max_chars and results:
                continue
            results.append(c); total += len(c["text"])
            if len(results) >= k:
                break
        return results

# ---------------------------------------------------------------- embeddings
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
    parts = [f"Use only the documentation excerpts below to answer. Cite the section path you rely on.\n"
             f"If the excerpts don't contain the answer, say so.\n\nQuestion: {question}\n"]
    for n, c in enumerate(results, 1):
        parts.append(f"---\n### Excerpt {n} — {c['source']} › {c['path'] or '(top)'}\n\n{c['text']}\n")
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
<input name=k type=number value="{k}" style="width:4em" title="passages"> <button>Search</button></form>
<textarea id=out>{ctx}</textarea>
<button onclick="navigator.clipboard.writeText(document.getElementById('out').value);this.textContent='Copied ✓'">Copy for Copilot</button>
<span style="color:#888;margin-left:1em">{info}</span>"""


def serve(index, port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            q, k = qs.get("q", [""])[0], int(qs.get("k", ["8"])[0] or 8)
            ctx, info = "", f"{len(index.chunks)} chunks indexed"
            if q:
                res = index.search(q, k=k)
                ctx = format_context(q, res)
                info = f"{len(res)} passages · {len(ctx):,} chars"
            body = PAGE.replace("{q}", html.escape(q)).replace("{k}", str(k)) \
                       .replace("{ctx}", html.escape(ctx)).replace("{info}", info)
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
    i = sub.add_parser("ingest", help="build the index from markdown files")
    i.add_argument("files", nargs="+")
    i.add_argument("--model", default=DEFAULT_MODEL)
    a = sub.add_parser("ask", help="retrieve passages for a question")
    a.add_argument("question")
    a.add_argument("-k", type=int, default=8, help="number of passages (default 8)")
    a.add_argument("--max-chars", type=int, default=20000, help="total size budget")
    a.add_argument("--out", help="write to a file (e.g. context.md) to attach in Copilot")
    a.add_argument("--clip", action="store_true", help="copy to the Windows clipboard")
    s = sub.add_parser("serve", help="web page with a copy button")
    s.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.cmd == "ingest":
        chunks = make_chunks(args.files)
        print(f"{len(chunks)} chunks from {len(args.files)} file(s); embedding with {args.model} …")
        model = load_model(args.model)
        emb = embed(model, args.model, [f"{c['path']}\n{c['text']}" for c in chunks])
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.save(INDEX_DIR / "embeddings.npy", emb)
        (INDEX_DIR / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        (INDEX_DIR / "meta.json").write_text(json.dumps({"model": args.model}), encoding="utf-8")
        print(f"Index written to {INDEX_DIR.resolve()}")
    elif args.cmd == "ask":
        idx = Index()
        ctx = format_context(args.question, idx.search(args.question, args.k, args.max_chars))
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
