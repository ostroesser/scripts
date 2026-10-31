"""Tests for several-query search and the brief, on invented data with a fake embedder (no models).

    python -m unittest test_docrag -v
"""
import hashlib, io, json, os, re, socket, sys, tempfile, threading, time, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np

TMP = tempfile.mkdtemp()
os.environ["DOCRAG_INDEX"] = f"{TMP}/index"
sys.path.insert(0, str(Path(__file__).parent))
import docrag  # noqa: E402

DIM = 256


def fake_vector(text):
    """Bag of hashed words: texts sharing words are close, like a very dumb embedding model."""
    v = np.zeros(DIM, dtype=np.float32)
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
    n = np.linalg.norm(v)
    return v / n if n else v


def fake_embed(model, name, texts, is_query=False, progress=None):
    return np.array([fake_vector(t) for t in texts], dtype=np.float32)


class FakeReranker:
    """Counts how much it is asked to read; scores by shared words."""
    def __init__(self):
        self.calls = []

    def score(self, question, passages):
        self.calls.append((question, len(passages)))
        words = set(question.lower().split())
        return np.array([len(words & set(p.lower().split())) for p in passages], dtype=float)


PAGES = [
    ("Driver Guide", "guide.txt", "", "Use the zephyr driver version 7 for the orders database."),
    ("Connection Types", "guide.txt", "", "Pick the tunnel connection type for the orders database."),
    ("Both Topics", "guide.txt", "", "zephyr driver and tunnel connection for orders."),
    ("Gardening", "misc.txt", "", "Tomatoes need sun and water every morning."),
    ("Old Thread", "support.jsonl", "2024-02-01", "Someone asked about the zephyr driver long ago."),
    ("New Thread", "support.jsonl", "2026-05-01", "A colleague asked which tunnel connection to pick."),
]


def write_index(path, pages=PAGES):
    """An index with one section per page, or several when a page's text is a list."""
    page_rows, chunks = [], []
    for pid, (title, file, date, text) in enumerate(pages):
        parts = text if isinstance(text, list) else [text]
        page_rows.append({"id": pid, "title": title, "url": f"https://docs.example.test/{pid}", "file": file,
                          "date": date, "source": file, "text": "\n\n".join(parts)})
        chunks += [{"page": pid, "path": "", "text": part} for part in parts]
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({"model": "fake"}), encoding="utf-8")
    (path / "pages.json").write_text(json.dumps(page_rows), encoding="utf-8")
    (path / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
    np.save(path / "embeddings.npy", np.array([fake_vector(c["text"]) for c in chunks], dtype=np.float32))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(docrag, "embed", fake_embed),
                  mock.patch.object(docrag.Index, "model", new_callable=mock.PropertyMock, return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def index(self, pages=PAGES):
        d = tempfile.mkdtemp(dir=TMP)
        return docrag.Index(write_index(d, pages), reranker=None)


def titles(results):
    return [r["title"] for r in results]


class CleanQueries(unittest.TestCase):
    def test_blanks_and_duplicates_dropped(self):
        self.assertEqual(docrag.clean_queries(["a  b", "", "  ", "A B", "c"]), (["a b", "c"], 0))

    def test_cap(self):
        kept, left = docrag.clean_queries([f"q{i}" for i in range(docrag.MAX_QUERIES + 3)])
        self.assertEqual((len(kept), left), (docrag.MAX_QUERIES, 3))

    def test_none(self):
        self.assertEqual(docrag.clean_queries(None), ([], 0))


class Search(Base):
    def test_question_alone_is_the_only_query(self):
        idx = self.index()
        res = idx.search("zephyr driver", k=2)
        self.assertEqual(idx.last_queries, ["zephyr driver"])
        self.assertTrue(all(r["found_by"] == [0] for r in res))
        self.assertEqual(idx.query_notices(), [])

    def test_empty_queries_mean_the_question(self):
        idx = self.index()
        self.assertEqual(titles(idx.search("zephyr driver", k=3, queries=["", "  "])),
                         titles(idx.search("zephyr driver", k=3)))

    def test_question_is_not_searched_when_queries_are_given(self):
        idx = self.index()
        res = idx.search("tomatoes sun water", k=1, queries=["zephyr driver"])
        self.assertNotEqual(titles(res), ["Gardening"])

    def test_page_found_by_several_queries_ranks_first(self):
        res = self.index().search("q", k=3, queries=["zephyr driver version", "tunnel connection type"])
        self.assertEqual(res[0]["title"], "Both Topics")
        self.assertEqual(res[0]["found_by"], [0, 1])

    def test_pages_are_deduplicated(self):
        res = self.index().search("q", k=6, queries=["zephyr driver", "zephyr driver orders", "tunnel"])
        self.assertEqual(len(titles(res)), len(set(titles(res))))

    def test_k_applies_to_the_merged_list(self):
        self.assertEqual(len(self.index().search("q", k=2, queries=["zephyr", "tunnel", "tomatoes"])), 2)

    def test_size_budget_applies_to_the_merged_list(self):
        pages = [(f"Long {i}", "guide.txt", "", [f"zephyr tunnel part {j} " + "word " * 300 for j in range(6)])
                 for i in range(4)]
        res = self.index(pages).search("q", k=4, max_chars=8000, queries=["zephyr", "tunnel"])
        self.assertEqual(len(res), 4)
        self.assertLessEqual(sum(len(r["text"]) for r in res), 8000)
        self.assertTrue(all(not r["whole"] for r in res))

    def test_extracts_cover_each_query(self):
        page = ("Big", "guide.txt", "", ["zephyr driver " + "alpha " * 300, "filler " * 300, "more filler " * 150,
                                         "tunnel connection " + "beta " * 300])
        text = self.index([page]).search("q", k=1, max_chars=4000, queries=["zephyr driver", "tunnel connection"])[0]["text"]
        self.assertIn("zephyr driver", text)
        self.assertIn("tunnel connection", text)

    def test_found_by_has_a_cutoff(self):
        # every query ranks every page, but only its top ones count as found by it
        pages = [(f"Page {i}", "guide.txt", "", f"{'alpha' if i < 15 else 'beta'} {i}") for i in range(30)]
        res = self.index(pages).search("q", k=3, queries=["alpha", "beta"])
        self.assertTrue(all(len(r["found_by"]) == 1 for r in res))
        res = self.index(pages).search("q", k=30, queries=["alpha", "beta"])   # k widens the cutoff
        self.assertTrue(len(res) == 30 and all(r["found_by"] == [0, 1] for r in res))

    def test_filters_apply_to_every_query(self):
        res = self.index().search("q", k=6, since="2026-01-01", queries=["zephyr", "tunnel"])
        self.assertNotIn("Old Thread", titles(res))
        self.assertIn("Driver Guide", titles(res))        # docs aren't affected by the date
        res = self.index().search("q", k=6, groups=["chat:*"], queries=["zephyr", "tomatoes"])
        self.assertTrue(set(titles(res)) <= {"Old Thread", "New Thread"})

    def test_no_allowed_pages(self):
        self.assertEqual(self.index().search("q", source="absent", queries=["zephyr", "tunnel"]), [])

    def test_query_without_a_page_is_reported(self):
        pages = [(f"Shared {i}", "guide.txt", "", "alpha beta") for i in range(12)] + \
                [(f"Other {i}", "guide.txt", "", f"gamma {i}") for i in range(12)]
        idx = self.index(pages)
        res = idx.search("q", k=2, queries=["alpha", "beta", "gamma"])
        self.assertEqual(len(res), 2)
        self.assertEqual(idx.last_unused, [2])
        self.assertIn("Query 3 contributed no page", idx.query_notices()[0])

    def test_left_out_queries_are_reported(self):
        idx = self.index()
        idx.search("q", queries=[f"zephyr {i}" for i in range(docrag.MAX_QUERIES + 2)])
        self.assertEqual(len(idx.last_queries), docrag.MAX_QUERIES)
        self.assertIn("2 left out", idx.query_notices()[0])

    def test_rerank_per_query_with_its_own_query_and_a_shared_total(self):
        pages = [(f"Page {i}", "guide.txt", "", f"zephyr {i}") for i in range(60)]
        idx = self.index(pages)
        idx.reranker_name, idx._reranker = "fake", FakeReranker()
        idx.search("question", k=5, rerank=True)
        self.assertEqual(idx._reranker.calls, [("question", 20)])          # one query: as before
        for n, each in ((2, 20), (3, 13), (5, 8), (8, 5)):
            idx._reranker.calls.clear()
            queries = [f"zephyr {i}" for i in range(n)]
            idx.search("question", k=5, rerank=True, queries=queries)
            self.assertEqual(idx._reranker.calls, [(q, each) for q in queries])
            self.assertTrue(idx.last_reranked)
        idx._reranker.calls.clear()
        idx.search("question", k=50, rerank=True, queries=[f"zephyr {i}" for i in range(5)])
        self.assertEqual(sum(n for _, n in idx._reranker.calls), 150)      # high k: shared, not 150 each


class Brief(unittest.TestCase):
    def test_brief_text(self):
        text = docrag.brief_text("  which {driver} for ERR-42?  ")
        self.assertIn("which {driver} for ERR-42?", text)
        for part in ("screenshot", "exact keyword matching", "meaning-based", "3 to 5", "word for word",
                     "URL schemes", "database aliases", "error messages", "field labels", "sub-question",
                     "support channel", "no quotes", "boolean", "under 15 words", "one per line"):
            self.assertIn(part, text)
        self.assertNotIn("{question}", text)

    def test_cli_needs_no_index(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["docrag", "brief", "which driver?"]), redirect_stdout(out), \
             mock.patch.object(docrag, "Index", side_effect=AssertionError("loaded the index")):
            docrag.main()
        self.assertIn("which driver?", out.getvalue())


class Cli(Base):
    def run_ask(self, *args):
        out, err = io.StringIO(), io.StringIO()
        path = write_index(tempfile.mkdtemp(dir=TMP))
        real = docrag.Index
        with mock.patch.object(docrag, "Index", lambda reranker=None: real(path, reranker=None)), \
             mock.patch.object(sys, "argv", ["docrag", "ask", "my real question?", "--no-rerank", *args]), \
             mock.patch.object(real, "search", autospec=True, side_effect=real.search) as search, \
             redirect_stdout(out), redirect_stderr(err):
            docrag.main()
        return out.getvalue(), err.getvalue(), search.call_args.kwargs

    def test_ask_without_query_is_unchanged(self):
        out, err, kw = self.run_ask()
        self.assertTrue(out.startswith("my real question?\n"))
        self.assertFalse(kw["keywords"])
        self.assertNotIn("[", err)

    def test_query_flags_merge_and_imply_keywords(self):
        f = Path(tempfile.mkdtemp(dir=TMP)) / "q.txt"
        f.write_text("tunnel connection\n\n", encoding="utf-8")
        out, err, kw = self.run_ask("--query", "zephyr driver", "--queries-file", str(f))
        self.assertEqual(kw["queries"][:2], ["zephyr driver", "tunnel connection"])
        self.assertTrue(kw["keywords"])
        self.assertTrue(out.startswith("my real question?\n"))    # the copy opens on the question
        self.assertIn("Both Topics [1,2]", err)

    def test_no_keywords_wins(self):
        self.assertFalse(self.run_ask("--query", "zephyr", "--no-keywords")[2]["keywords"])

    def test_keywords_flag_without_queries(self):
        self.assertTrue(self.run_ask("--keywords")[2]["keywords"])

    def test_missing_queries_file(self):
        with self.assertRaises(SystemExit) as e:
            self.run_ask("--queries-file", f"{TMP}/absent.txt")
        self.assertIn("Can't read", str(e.exception))


class Api(Base):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        with mock.patch.object(docrag, "embed", fake_embed):
            cls.idx = docrag.Index(write_index(tempfile.mkdtemp(dir=TMP)), reranker=None)
        cls.idx._model = object()
        threading.Thread(target=lambda: docrag.serve(cls.idx, cls.port), daemon=True).start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)

    def get(self, path, params=()):
        url = f"http://127.0.0.1:{self.port}{path}?{urlencode(list(params))}"
        with urlopen(url) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_old_url_is_unchanged(self):
        data = self.get("/api/search", [("q", "zephyr driver"), ("k", "3"), ("keywords", "0"), ("rerank", "0")])
        self.assertEqual(data["queries"], ["zephyr driver"])
        self.assertTrue(data["header"].startswith("zephyr driver\n"))
        self.assertEqual(titles(data["results"]), titles(self.idx.search("zephyr driver", k=3)))
        for key in ("title", "url", "date", "kind", "label", "whole", "chars", "text", "block"):
            self.assertIn(key, data["results"][0])

    def test_repeated_query_params(self):
        data = self.get("/api/search", [("q", "my real question?"), ("query", "zephyr driver"),
                                        ("query", "tunnel connection")])
        self.assertEqual(data["queries"], ["zephyr driver", "tunnel connection"])
        self.assertTrue(data["header"].startswith("my real question?\n"))
        self.assertEqual(data["results"][0]["found_by"], [0, 1])

    def test_queries_imply_keywords_unless_told_otherwise(self):
        with mock.patch.object(self.idx, "search", wraps=self.idx.search) as s:
            self.get("/api/search", [("q", "x"), ("query", "zephyr")])
            self.assertTrue(s.call_args.kwargs["keywords"])
            self.get("/api/search", [("q", "x"), ("query", "zephyr"), ("keywords", "0")])
            self.assertFalse(s.call_args.kwargs["keywords"])
            self.get("/api/search", [("q", "x")])
            self.assertFalse(s.call_args.kwargs["keywords"])

    def test_empty_question_is_still_refused(self):
        with self.assertRaises(HTTPError) as e:
            self.get("/api/search", [("query", "zephyr")])
        self.assertEqual(e.exception.code, 400)

    def test_sources_carry_the_brief_template(self):
        self.assertIn("{question}", self.get("/api/sources")["brief_template"])


if __name__ == "__main__":
    unittest.main()
