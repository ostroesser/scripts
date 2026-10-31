#!/usr/bin/env python3
"""
docrag — local retrieval over your scraped docs, for pasting into Copilot Chat.

  python docrag.py ingest export/*.txt mm_export/*.jsonl   # (re)build the index; unchanged parts are reused
                                               # pass every file: --replace to drop ones already indexed
  python docrag.py ask "which base image for a Java 21 app?" --clip
  python docrag.py ask "..." --out context.md # file to attach in Copilot
  python docrag.py ask "ERR-4012 on rhel9-base" --keywords    # also match exact words
  python docrag.py ask "..." --source "Team Support" --since 2026-01-01   # filter chat threads
  python docrag.py ask "which driver for this DB?" --query "orders-db JDBC driver" --query "how to connect to orders-db"
  python docrag.py brief "which driver for this DB?" --clip   # prompt asking Copilot to write those queries
  python docrag.py serve                      # small web page at http://localhost:8765

The page is the unit of output. Inputs are text exports where the page blocks
(=====  DOCUMENT PAGE n / Title: / Source: =====) are parsed, or .jsonl files
with one page per line (title, url, text, optional date and source), such as
mmscrape's chat threads. Results are whole pages with their title and URL. Pages too big to send
whole are cut on their headings, and only the matching parts are sent.

Search ranks by meaning (local embeddings). With --keywords, BM25 also ranks exact terms
(image names, properties, error codes) and both are merged with reciprocal rank fusion.
Matching runs on sections, so long pages are not truncated by the embedding
model. Nothing leaves your machine, and no LLM is involved.
"""
import argparse, html, json, math, os, re, subprocess, sys, tempfile, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

INDEX_DIR = Path(os.environ.get("DOCRAG_INDEX", "./docrag_index"))
# Small models: fast on a laptop CPU
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"           # 33M params, fast, good English retrieval
DEFAULT_RERANKER = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # 22M params, ~1 s per search
RERANK_CANDIDATES = 20                             # sections the reranker re-reads per question
MAX_QUERIES = 8                                    # search queries run per search; extra ones are left out
RERANK_TOTAL = 40                                  # with several queries: sections re-read, shared between them
RERANK_MIN_PER_QUERY = 5
FOUND_BY_DEPTH = 10                                # a query "found" a page if it's in its top max(k, this) pages
MAX_TOKENS = 512                                   # sections are short; caps CPU time per section
MAX_CHUNK = 1800       # chars per searchable section (the model reads ~512 tokens)
MIN_CHUNK = 250        # tiny sections get merged with their neighbour
MAX_CHARS = 24000      # default total size budget of one answer (~6k tokens)

# The prompt behind "Brief for Copilot" and `brief`. Edit freely; {question} is replaced by the question.
BRIEF_TEMPLATE = """\
I need search queries for a documentation and chat search tool called docrag. Don't answer the question yet.

My question:
{question}

I may have attached a screenshot; treat it as part of the question.

How docrag searches: it combines exact keyword matching with meaning-based matching. It works best with short, single-topic queries that use the exact names found in the docs.

Write 3 to 5 search queries:
- Copy every exact identifier visible in the screenshot or the question word for word: product and tool names, driver names and versions, URL schemes, database aliases, error messages, field labels.
- Write one query per sub-question.
- Mix keyword-style queries with queries phrased like a question a colleague would post in a support channel.
- Use no quotes and no boolean operators.
- Keep each query under 15 words.

Output only the queries, one per line, with no numbering and no commentary.
"""


def brief_text(question):
    return BRIEF_TEMPLATE.replace("{question}", question.strip())


def clean_queries(queries):
    """Usable search queries: blanks and duplicates dropped, capped at MAX_QUERIES.
    Returns (kept, how many were left out by the cap)."""
    kept, seen = [], set()
    for q in queries or ():
        q = " ".join(q.split())
        if q and q.lower() not in seen:
            seen.add(q.lower())
            kept.append(q)
    return kept[:MAX_QUERIES], max(0, len(kept) - MAX_QUERIES)


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
    def __init__(self, path=INDEX_DIR, reranker=DEFAULT_RERANKER):
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
        self.reranker_name = reranker             # None: no reranking
        self._reranker = None
        self.rerank_error = None
        self.last_reranked = False
        self.last_queries = []                    # the queries the last search ran
        self.last_left_out = 0                    # queries beyond MAX_QUERIES
        self.last_unused = []                     # queries none of the returned pages came from
        NEEDED_MODELS.add(self.model_name)
        if reranker:
            NEEDED_MODELS.add(reranker)

    def query_notices(self):
        """What the user should know about the queries of the last search."""
        out = []
        if self.last_left_out:
            out.append(f"Only the first {MAX_QUERIES} queries ran; {self.last_left_out} left out.")
        if self.last_unused:
            nums = ", ".join(str(n + 1) for n in self.last_unused)
            out.append(f"Quer{'ies' if len(self.last_unused) > 1 else 'y'} {nums} contributed no page; "
                       "raise Pages to include more.")
        return out

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

    def reranker(self):
        """The reranker, loaded on first use; None if it can't be loaded (search still works)."""
        if self._reranker is None and self.rerank_error is None and self.reranker_name:
            try:
                self._reranker = Reranker(self.reranker_name)
            except (Exception, SystemExit) as e:
                self.rerank_error = f"reranker {self.reranker_name} unavailable ({type(e).__name__}: {e})"
                print("warning:", self.rerank_error, file=sys.stderr)
        return self._reranker

    def rank(self, query, qv, idx, keywords, rerank_n):
        """Allowed sections (idx) for one query, best first. rerank_n: sections the reranker re-reads."""
        vec = self.emb[idx] @ qv
        if keywords:                           # reciprocal rank fusion of meaning and exact words
            rrf = np.zeros(len(idx))
            for scores in (self.bm25.scores(tokenize(query))[idx], vec):
                order = np.argsort(-scores)
                rrf[order] += 1.0 / (60 + np.arange(1, len(idx) + 1))
            ranked = idx[np.argsort(-rrf)]
        else:
            ranked = idx[np.argsort(-vec)]
        if rerank_n and len(ranked) > 1:       # re-read the best candidates and reorder them
            rr = self.reranker()
            if rr is not None:
                head = ranked[:rerank_n]
                scores = rr.score(query, [f"{self.chunk_header(self.chunks[i])}\n{self.chunks[i]['text']}"
                                          for i in head])
                # Blend the reranker's order with the first one instead of replacing it: the
                # reranker (trained on web keyword queries) refines, the meaning ranking still counts.
                steps = 1.0 / (60 + np.arange(1, len(head) + 1))
                fused = steps.copy()
                fused[np.argsort(-scores, kind="stable")] += steps
                ranked = np.concatenate([head[np.argsort(-fused, kind="stable")], ranked[rerank_n:]])
                self.last_reranked = True
        return ranked

    def search(self, question, k=5, max_chars=None, since=None, source=None, groups=None, rerank=False,
               keywords=False, queries=None):
        """Return up to k pages: whole when small enough, else only their matching sections.
        queries: search with these instead of the question. Each is ranked (and reranked) on its own,
                then their page rankings are merged, so a page found by several ranks higher.
                Each result says which queries found it in "found_by" (0-based positions in last_queries).
        keywords: also rank by exact words (BM25), fused with the meaning ranking. Off: meaning only.
        since:  chat threads only: keep those active on/after YYYY-MM-DD (docs are not affected).
        source: keep pages whose source, label or file name contains this text.
        groups: keep pages of these sources ("docs:<file>", "chat:<team/channel>", or
                "docs:*" / "chat:*" for all of a kind); several are combined with OR."""
        max_chars = max_chars or MAX_CHARS
        groups = set(groups or ())
        queries, self.last_left_out = clean_queries(queries)
        queries = queries or [question]
        self.last_queries, self.last_unused, self.last_reranked = queries, [], False
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
        # Enough candidates for the pages asked for. Several queries share a total, so that
        # reranking costs about twice a single search rather than one more per query.
        if not rerank:
            rerank_n = 0
        elif len(queries) == 1:
            rerank_n = max(RERANK_CANDIDATES, k * 3)
        else:
            rerank_n = max(RERANK_MIN_PER_QUERY, max(RERANK_TOTAL, k * 3) // len(queries))
        qvs = embed(self.model, self.model_name, queries, is_query=True)
        rankings = [self.rank(q, qv, idx, keywords, rerank_n) for q, qv in zip(queries, qvs)]

        # Reciprocal rank fusion of the queries' page rankings (reranker scores of different queries
        # can't be compared, ranks can). Only a query's top pages count: every query ranks every page.
        depth, page_score, found_by = max(k, FOUND_BY_DEPTH), {}, {}
        for qn, ranked in enumerate(rankings):
            seen = set()
            for i in ranked:
                pid = self.chunks[i]["page"]
                if pid not in seen:
                    seen.add(pid)
                    page_score[pid] = page_score.get(pid, 0) + 1.0 / (60 + len(seen))
                    found_by.setdefault(pid, []).append(qn)
                    if len(seen) >= depth:
                        break
        best = sorted(page_score, key=page_score.get, reverse=True)[:k]   # stable: ties keep query order

        # The k best pages -> all their chunks, most relevant first (for the queries that found the page)
        chunk_score = {}
        for qn, ranked in enumerate(rankings):
            wanted = {pid for pid in best if qn in found_by[pid]}
            for pos, i in enumerate(ranked, 1):
                if self.chunks[i]["page"] in wanted:
                    chunk_score[int(i)] = chunk_score.get(int(i), 0) + 1.0 / (60 + pos)
        hits = {pid: [] for pid in best}
        for i in sorted(chunk_score, key=chunk_score.get, reverse=True):
            hits[self.chunks[i]["page"]].append(i)
        used = {qn for pid in best for qn in found_by[pid]}
        self.last_unused = [qn for qn in range(len(queries)) if qn not in used]

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
        results = [{**self.pages[pid], "text": bodies[pid][0], "whole": bodies[pid][1],
                    "found_by": found_by[pid]} for pid in hits]
        return results

# ---------------------------------------------------------------- embeddings
def text_hash(t):
    import hashlib
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


PARTIAL = "partial"     # sub-folder of an index: blocks embedded by an unfinished ingest


def save_partial(index_dir, model_name, n, hashes, vectors):
    d = Path(index_dir) / PARTIAL
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.txt").write_text(model_name, encoding="utf-8")
    np.save(d / f"{n:06d}.npy", vectors)
    (d / f"{n:06d}.json").write_text(json.dumps(hashes), encoding="utf-8")


def load_partial(index_dir, model_name):
    d, cache = Path(index_dir) / PARTIAL, {}
    try:
        if (d / "model.txt").read_text(encoding="utf-8") != model_name:
            return cache
        for f in sorted(d.glob("*.json")):
            vectors = np.load(f.with_suffix(".npy"))
            cache.update(zip(json.loads(f.read_text(encoding="utf-8")), vectors))
    except (OSError, ValueError):
        pass
    return cache


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
    for path in paths:                          # blocks left by an interrupted ingest
        for h, v in load_partial(path, model_name).items():
            cache.setdefault(h, v)
    return [cache.get(text_hash(t)) for t in texts]


def dropped_files(files, index_dir=INDEX_DIR):
    """Export files in the existing index that this ingest doesn't include (it would remove them)."""
    try:
        old = {p["file"] for p in json.loads((Path(index_dir) / "pages.json").read_text(encoding="utf-8"))}
    except (OSError, KeyError, ValueError):
        return []
    return sorted(old - {Path(f).name for f in files})


def model_is_cached(name):
    """True if the model is a local folder or already in the Hugging Face cache."""
    if Path(name).is_dir():
        return True
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"), "hub")
    snapshots = Path(hub) / ("models--" + name.replace("/", "--")) / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


NEEDED_MODELS = set()      # every model this run will load (embedding + reranker)


def prepare_hf(name):
    """Decide offline/online once, before the Hugging Face libraries are imported (they read the
    setting at import): offline when every model this run needs is on disk, else download."""
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    NEEDED_MODELS.add(name)
    cached = model_is_cached(name)
    if "transformers" not in sys.modules and all(model_is_cached(n) for n in NEEDED_MODELS):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    if not cached:
        if os.environ.get("DOCRAG_OFFLINE") == "1":
            sys.exit(f"Model {name} is not downloaded and DOCRAG_OFFLINE=1. "
                     "Run once without it, or pass a local model folder.")
        print(f"Model {name} not found locally, downloading it from Hugging Face …", file=sys.stderr)
    return cached


def load_model(name):
    """Load the embedding model, without touching the network when it's already on disk."""
    cached = prepare_hf(name)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(name, local_files_only=cached)
    if getattr(model, "max_seq_length", None):
        model.max_seq_length = min(model.max_seq_length, MAX_TOKENS)
    return model


def prompts_for(name):
    """Query/passage prefixes some model families expect."""
    n = name.lower()
    if "e5" in n:
        return "query: ", "passage: "
    if "bge-" in n and "en-v1.5" in n:
        return "Represent this sentence for searching relevant passages: ", ""
    return "", ""


def embed(model, name, texts, is_query=False, progress=None):
    query_prefix, passage_prefix = prompts_for(name)
    prefix = query_prefix if is_query else passage_prefix
    if prefix:
        texts = [prefix + t for t in texts]
    v = model.encode(texts, batch_size=16, normalize_embeddings=True,
                     show_progress_bar=(not is_query) if progress is None else progress)
    return np.asarray(v, dtype=np.float32)


class Reranker:
    """Scores (question, section) pairs with a cross-encoder; higher is more relevant."""

    def __init__(self, name):
        self.name = name
        cached = prepare_hf(name)
        from sentence_transformers import CrossEncoder
        self.ce = CrossEncoder(name, max_length=MAX_TOKENS, local_files_only=cached)

    def score(self, question, passages):
        if not passages:
            return np.zeros(0)
        return np.asarray(self.ce.predict([(question, p) for p in passages], batch_size=16), dtype=float)


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
      GET /api/search?q=&query=&query=&k=&size=&since=&keywords=0|1&rerank=0|1&src=<key>&src=<key>
                                        -> {"header": str, "queries": [str], "notices": [str],
                                            "results": [{..., "block": str, "found_by": [int]}]}
    q is the question (first line of the copy). It is also the search, unless query= params are
    given; those then imply keywords=1 unless keywords= says otherwise."""
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
                                           "sections": len(index.chunks), "model": index.model_name,
                                           "reranker": index.reranker_name, "brief_template": BRIEF_TEMPLATE})
                if u.path == "/api/search":
                    q = arg("q")
                    if not q:
                        return self.send_json({"error": "empty question"}, 400)
                    queries = clean_queries(qs.get("query"))[0]
                    t0 = time.time()
                    want_rerank = arg("rerank", "1") != "0" and bool(index.reranker_name)
                    res = index.search(q, k=max(1, min(int(arg("k", "5") or 5), 50)),
                                       max_chars=max(1000, int(arg("size", str(MAX_CHARS)) or MAX_CHARS)),
                                       since=arg("since") or None, groups=qs.get("src") or None,
                                       rerank=want_rerank, keywords=arg("keywords", "1" if queries else "0") == "1",
                                       queries=qs.get("query"))
                    return self.send_json({
                        "header": context_header(q), "seconds": round(time.time() - t0, 2),
                        "queries": index.last_queries, "notices": index.query_notices(),
                        "reranked": index.last_reranked, "rerank_available": bool(index.reranker_name),
                        "warning": index.rerank_error if want_rerank and not index.last_reranked else None,
                        "results": [
                        {"title": r["title"], "url": r["url"], "date": r.get("date", ""), "kind": r["kind"],
                         "label": r["label"], "whole": r["whole"], "chars": len(r["text"]),
                         "text": r["text"], "block": result_block(n, r), "found_by": r["found_by"]}
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
    i.add_argument("--replace", action="store_true",
                   help="allow removing files that are in the current index but not in this ingest")
    a = sub.add_parser("ask", help="retrieve pages for a question")
    a.add_argument("question")
    a.add_argument("--query", action="append", default=[], metavar="TEXT",
                   help="search with this instead of the question (repeatable); results are merged")
    a.add_argument("--queries-file", metavar="FILE", help="more search queries, one per line")
    a.add_argument("-k", type=int, default=5, help="number of pages (default 5)")
    a.add_argument("--max-chars", type=int, default=MAX_CHARS, help="total size budget (characters)")
    a.add_argument("--since", help="chat threads active on/after YYYY-MM-DD (docs are not affected)")
    a.add_argument("--source", help="only pages whose source/doc name/file contains this text")
    a.add_argument("--out", help="write to a file (e.g. context.md) to attach in Copilot")
    a.add_argument("--clip", action="store_true", help="copy to the Windows clipboard")
    a.add_argument("--keywords", action=argparse.BooleanOptionalAction, default=None,
                   help="also match exact words (identifiers, error codes), not only the meaning "
                        "(default: on with search queries, off without)")
    a.add_argument("--no-rerank", action="store_true", help="skip the reranker (faster, less precise)")
    a.add_argument("--reranker", default=DEFAULT_RERANKER, help="reranker model (default %(default)s)")
    b = sub.add_parser("brief", help="print a prompt asking Copilot to write search queries for a question")
    b.add_argument("question")
    b.add_argument("--clip", action="store_true", help="copy to the Windows clipboard")
    s = sub.add_parser("serve", help="web page with a copy button")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1", help="listen address (default: this machine only)")
    s.add_argument("--no-rerank", action="store_true", help="never load the reranker")
    s.add_argument("--reranker", default=DEFAULT_RERANKER, help="reranker model (default %(default)s)")
    args = ap.parse_args()

    if args.cmd == "ingest":
        dropped = dropped_files(args.files)
        if dropped and not args.replace:
            sys.exit(f"The index at {INDEX_DIR.resolve()} also holds these files, which are not in this ingest:\n"
                     + "".join(f"  {f}\n" for f in dropped) +
                     "ingest rebuilds the index from the files given, so they would be removed.\n"
                     "Add them to the command, or pass --replace to remove them.")
        pages, chunks = build(args.files)
        print(f"{len(pages)} pages, {len(chunks)} sections from {len(args.files)} file(s); "
              f"embedding with {args.model} …")
        titles = {p["id"]: p["title"] for p in pages}
        texts = [f"{titles[c['page']]} > {c['path']}\n{c['text']}" for c in chunks]
        emb = reuse_embeddings(texts, args.model, [INDEX_DIR] + args.reuse)   # unchanged sections keep their vectors
        todo = [i for i, v in enumerate(emb) if v is None]
        print(f"{len(chunks) - len(todo)} sections unchanged, {len(todo)} to embed")
        if todo:
            model, block, t0 = load_model(args.model), 256, time.time()
            start_n = len(list((INDEX_DIR / PARTIAL).glob("*.json"))) if (INDEX_DIR / PARTIAL).is_dir() else 0
            print(f"  embedding in blocks of {block}; speed and time left appear after the first block …",
                  file=sys.stderr, flush=True)
            for b, s0 in enumerate(range(0, len(todo), block)):
                ids = todo[s0:s0 + block]
                new = embed(model, args.model, [texts[i] for i in ids], progress=False)
                for i, v in zip(ids, new):
                    emb[i] = v
                save_partial(INDEX_DIR, args.model, start_n + b, [text_hash(texts[i]) for i in ids], new)
                done, elapsed = s0 + len(ids), time.time() - t0
                left = elapsed / done * (len(todo) - done)
                print(f"\r  {done:,}/{len(todo):,} sections embedded, {done / elapsed:.1f}/s, "
                      f"about {int(left // 3600)}h{int(left % 3600 // 60):02d} left "
                      f"(safe to stop: done blocks are kept)  ", end="", file=sys.stderr, flush=True)
            print(file=sys.stderr)
        emb = np.vstack(emb).astype(np.float32) if emb else np.zeros((0, 1), dtype=np.float32)
        for c, t in zip(chunks, texts):
            c["hash"] = text_hash(t)
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.save(INDEX_DIR / "embeddings.npy", emb)
        (INDEX_DIR / "pages.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        (INDEX_DIR / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        (INDEX_DIR / "meta.json").write_text(json.dumps({"model": args.model}), encoding="utf-8")
        import shutil
        shutil.rmtree(INDEX_DIR / PARTIAL, ignore_errors=True)   # all saved in the index now
        print(f"Index written to {INDEX_DIR.resolve()}")
    elif args.cmd == "ask":
        queries = list(args.query)
        if args.queries_file:
            try:
                queries += Path(args.queries_file).read_text(encoding="utf-8").split("\n")
            except OSError as e:
                sys.exit(f"Can't read the queries file: {e}")
        keywords = bool(clean_queries(queries)[0]) if args.keywords is None else args.keywords
        idx = Index(reranker=None if args.no_rerank else args.reranker)
        res = idx.search(args.question, args.k, args.max_chars, since=args.since, source=args.source,
                         rerank=not args.no_rerank, keywords=keywords, queries=queries)
        if idx.rerank_error:
            print("warning:", idx.rerank_error, "(results without reranking)", file=sys.stderr)
        ctx = format_context(args.question, res)
        several = len(idx.last_queries) > 1
        print("Pages: " + " | ".join(
            r["title"] + (" [" + ",".join(str(n + 1) for n in r["found_by"]) + "]" if several else "")
            for r in res), file=sys.stderr)
        for note in idx.query_notices():
            print("note:", note, file=sys.stderr)
        if args.out:
            Path(args.out).write_text(ctx, encoding="utf-8"); print(f"Wrote {args.out} ({len(ctx):,} chars)")
        if args.clip:
            to_clipboard(ctx); print(f"Copied to clipboard ({len(ctx):,} chars)")
        if not (args.out or args.clip):
            print(ctx)
    elif args.cmd == "brief":
        text = brief_text(args.question)
        if args.clip:
            to_clipboard(text); print(f"Copied to clipboard ({len(text):,} chars)")
        else:
            print(text)
    else:
        serve(Index(reranker=None if args.no_rerank else args.reranker), args.port, args.host)


if __name__ == "__main__":
    main()
