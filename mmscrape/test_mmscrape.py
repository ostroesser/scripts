"""Tests for mmscrape against a fake Mattermost API v4 (no real server needed).

    python -m unittest test_mmscrape -v
"""
import io, json, os, sys, tempfile, threading, unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TMP = tempfile.mkdtemp()
os.environ.update(MM_DATA=f"{TMP}/data", MM_EXPORT=f"{TMP}/export", MM_TZ="UTC")
sys.path.insert(0, str(Path(__file__).parent))
import mmscrape  # noqa: E402

mmscrape.time.sleep = lambda s: None       # no real waiting in retries

TOKEN = "good-token"
DAY = 86_400_000
T0 = 1_767_225_600_000                     # 2026-01-01 00:00 UTC


class Fake:
    """In-memory Mattermost: one team, one channel, users, posts."""
    def __init__(self):
        self.posts, self.calls, self.rate_limit_once, self.fail_before = {}, [], True, 0
        self.users = {"u1": ("Alice", "Martin"), "u2": ("Bob", "Dupont"), "u3": ("", "")}

    def add(self, pid, day, user, msg, root="", minute=0, **extra):
        t = T0 + day * DAY + minute * 60_000
        self.posts[pid] = {"id": pid, "create_at": t, "update_at": t, "edit_at": 0, "delete_at": 0,
                           "user_id": user, "root_id": root, "message": msg, "type": "", "props": {}, **extra}

    def live(self, posts=None):
        posts = self.posts if posts is None else posts
        return sorted((p for p in posts.values() if not p["delete_at"]), key=lambda p: -p["create_at"])


FAKE = Fake()
HISTORY = {}                               # second channel: 100 posts a day, days 3 to 9
for _d in range(3, 10):
    for _i in range(100):
        _t = T0 + _d * DAY + _i * 60_000
        HISTORY[f"h{_d}-{_i:03d}"] = {"id": f"h{_d}-{_i:03d}", "create_at": _t, "update_at": _t, "edit_at": 0,
                                      "delete_at": 0, "user_id": "u1", "root_id": "",
                                      "message": f"history day {_d} #{_i}", "type": "", "props": {}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def route(self, method):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.reply(401, {"message": "Invalid or expired session"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.removeprefix("/api/v4")
        FAKE.calls.append((method, path, q))
        if path == "/teams/name/team1/channels/name/team-support":
            return self.reply(200, {"id": "c1", "name": "team-support", "display_name": "Team Support",
                                    "type": "O", "total_msg_count": len(FAKE.posts)})
        if path == "/teams/name/team1/channels/name/history":
            return self.reply(200, {"id": "c2", "name": "history", "display_name": "History",
                                    "type": "O", "total_msg_count": len(HISTORY)})
        if path == "/channels/c2/posts":
            if q.get("before") and FAKE.fail_before:
                FAKE.fail_before -= 1
                return self.reply(500, {"id": "app.post.get_posts_around.get.app_error"})
            if "since" in q:
                ch = {p["id"]: p for p in HISTORY.values() if p["update_at"] >= int(q["since"])}
                return self.reply(200, {"order": list(ch), "posts": ch})
            live, per = FAKE.live(HISTORY), int(q.get("per_page", 60))
            start = [p["id"] for p in live].index(q["before"]) + 1 if q.get("before") else 0
            page = live[start:start + per]
            return self.reply(200, {"order": [p["id"] for p in page], "posts": {p["id"]: p for p in page}})
        if path == "/users/me/teams":
            return self.reply(200, [{"id": "t1", "name": "team1"}])
        if path == "/users/me/teams/t1/channels":
            return self.reply(200, [{"id": "c1", "name": "team-support", "display_name": "Team Support", "type": "O",
                                     "total_msg_count": 42},
                                    {"id": "d1", "name": "u1__u2", "display_name": "", "type": "D"}])
        if path == "/channels/c1/posts":
            if FAKE.rate_limit_once:
                FAKE.rate_limit_once = False
                return self.reply(429, {"message": "slow down"}, {"Retry-After": "0"})
            if q.get("before") and FAKE.fail_before:   # transient DB error, like a real 500
                FAKE.fail_before -= 1
                return self.reply(500, {"id": "app.post.get_posts_around.get.app_error"})
            if "since" in q:                   # changed posts, deleted ones included
                ch = {p["id"]: p for p in FAKE.posts.values() if p["update_at"] >= int(q["since"])}
                return self.reply(200, {"order": list(ch), "posts": ch})
            live, per = FAKE.live(), int(q.get("per_page", 60))
            start = [p["id"] for p in live].index(q["before"]) + 1 if q.get("before") else 0
            page = live[start:start + per]
            posts = {p["id"]: p for p in page}
            for p in page:                     # like the real API: thread roots added outside `order`
                if p["root_id"] and p["root_id"] in FAKE.posts and not FAKE.posts[p["root_id"]]["delete_at"]:
                    posts.setdefault(p["root_id"], FAKE.posts[p["root_id"]])
            return self.reply(200, {"order": [p["id"] for p in page], "posts": posts})
        if path.startswith("/posts/") and path.endswith("/thread"):
            rid = path.split("/")[2]
            th = {p["id"]: p for p in FAKE.live() if p["id"] == rid or p["root_id"] == rid}
            return self.reply(200, {"order": list(th), "posts": th}) if rid in th else self.reply(404, {})
        if path == "/users/ids" and method == "POST":
            ids = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            return self.reply(200, [{"id": i, "first_name": FAKE.users[i][0], "last_name": FAKE.users[i][1],
                                     "username": f"user-{i}"} for i in ids if i in FAKE.users])
        self.reply(404, {"message": f"no route {path}"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")


def run(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        fn(*a, **kw)
    return out.getvalue()


def export():
    f = Path(TMP, "export", "team1__team-support.jsonl")
    return {r["id"]: r for r in map(json.loads, f.read_text(encoding="utf-8").splitlines())}


class MMScrapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"
        f = FAKE
        f.add("old", -30, "u1", "Old question from December about **ingress**")          # before --since
        f.add("oldreply", 2, "u2", "Late answer to the December question", root="old")
        for i in range(450):                                                            # > 2 pages of 200
            f.add(f"s{i:03d}", 3 + i // 100, "u3", f"Standalone message {i}", minute=i % 100)
        f.add("q1", 10, "u1", "# Deploy stuck\nIt gets stuck on the deploy stage of my service",
              props={"attachments": [{"title": "Pipeline", "text": "build 1234 failed"}]},
              metadata={"files": [{"name": "log.txt"}]})
        f.add("q1r1", 10, "u2", "Wipe the pod and retry", root="q1", minute=5)
        f.add("q1r2", 10, "u1", "Worked, thanks", root="q1", minute=9)
        f.add("sys", 10, "u1", "u1 joined the channel", type="system_join_channel", minute=1)
        f.add("bot", 11, "u3", "Alert", props={"override_username": "Deploy-Bot"})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_1_full_then_incremental(self):
        mm = mmscrape.MM(self.url, TOKEN)
        run(mmscrape.sync, mm, "team1/team-support", since="2026-01-01")
        recs = export()

        # paging + rate-limit retry: every post since the limit is there, system post dropped
        self.assertEqual(sum(1 for r in recs if r.startswith("s")), 450)
        self.assertNotIn("sys", recs)
        # root older than --since is still fetched because a reply is recent
        self.assertIn("old", recs)
        self.assertIn("Late answer", recs["old"]["text"])
        self.assertEqual(recs["old"]["date"], "2026-01-03")                 # last activity
        # thread rendering
        q1 = recs["q1"]
        self.assertEqual(q1["replies"], 2)
        self.assertEqual(q1["url"], f"{self.url}/team1/pl/q1")
        self.assertEqual(q1["source"], "Team Support")
        self.assertIn("Deploy stuck It gets stuck", q1["title"])
        self.assertIn("**Alice Martin** · 2026-01-11 00:00", q1["text"])
        self.assertIn("[attachment] Pipeline\nbuild 1234 failed", q1["text"])
        self.assertIn("[files: log.txt]", q1["text"])
        self.assertLess(q1["text"].index("Wipe the pod"), q1["text"].index("Worked, thanks"))
        self.assertNotIn("q1r1", recs)                                       # replies live inside the thread
        self.assertIn("**Deploy-Bot**", recs["bot"]["text"])
        self.assertIn("**user-u3**", recs["s000"]["text"])                   # no full name -> username

        # --- incremental: new reply on an old thread, new post, an edit, a deletion
        n_calls = len(FAKE.calls)
        FAKE.add("q1r3", 20, "u2", "Also check the helm chart", root="q1")
        FAKE.add("new", 20, "u1", "Brand new question", minute=3)
        FAKE.posts["s001"].update(message="Standalone message 1 (fixed)", update_at=T0 + 20 * DAY, edit_at=T0 + 20 * DAY)
        FAKE.posts["s002"].update(delete_at=T0 + 20 * DAY, update_at=T0 + 20 * DAY)
        store_file = Path(TMP, "data", "c1.json")
        st = json.loads(store_file.read_text())
        st["synced_at"] = T0 + 19 * DAY                                      # pretend last run was day 19
        store_file.write_text(json.dumps(st))

        run(mmscrape.sync, mm, "https://ignored-host/team1/channels/team-support".replace("https://ignored-host", self.url))
        recs = export()
        self.assertEqual(recs["q1"]["replies"], 3)
        self.assertEqual(recs["q1"]["date"], "2026-01-21")
        self.assertIn("Brand new question", recs["new"]["text"])
        self.assertIn("(fixed)", recs["s001"]["text"])
        self.assertIn("(edited)", recs["s001"]["text"])
        self.assertNotIn("s002", recs)
        pages = [c for c in FAKE.calls[n_calls:] if c[1] == "/channels/c1/posts" and "since" not in c[2]]
        self.assertEqual(len(pages), 1, "incremental run should stop after the first page")

    def test_5_since_backfills_older_history(self):
        mm = mmscrape.MM(self.url, TOKEN)
        store_file = Path(TMP, "data", "c2.json")
        def sync(**kw):
            return run(mmscrape.sync, mm, "team1/history", **kw)

        FAKE.fail_before = 1                                                # one 500 on a cursor call
        out = sync(since="2026-01-08")                                      # day index 7
        st = json.loads(store_file.read_text())
        days = sorted({(p["create_at"] - T0) // DAY for p in st["posts"].values()})
        self.assertEqual(days, [7, 8, 9], "nothing before --since")
        self.assertEqual(st["covered_from"], mmscrape.date_ms("2026-01-08"))
        self.assertIn("new", out)
        n_first = len(st["posts"])

        out = sync()                                                        # nothing changed
        self.assertIn("0 new, 0 edited, 0 deleted", out)

        out = sync(since="2026-01-08")                                      # same date: nothing to do
        self.assertIn("already downloaded", out)

        n_calls, cursor = len(FAKE.calls), st["covered_from_id"]
        out = sync(since="2026-01-06")                                      # earlier: backfill the gap
        st = json.loads(store_file.read_text())
        days = sorted({(p["create_at"] - T0) // DAY for p in st["posts"].values()})
        self.assertEqual(days, [5, 6, 7, 8, 9])
        self.assertEqual(len(st["posts"]), n_first + 200)                   # 2 more days, 100 posts each
        self.assertIn("200 new", out)
        cursor_calls = [c for c in FAKE.calls[n_calls:] if c[1] == "/channels/c2/posts" and "before" in c[2]]
        self.assertEqual(cursor_calls[0][2]["before"], cursor, "backfill starts from the oldest stored post")
        self.assertLessEqual(len(cursor_calls), 2, "and doesn't walk the recent history again")

        del st["covered_from"]                                              # store from an older version
        store_file.write_text(json.dumps(st))
        out = sync(since="2026-01-05")
        st = json.loads(store_file.read_text())
        self.assertEqual(sorted({(p["create_at"] - T0) // DAY for p in st["posts"].values()})[0], 4)
        self.assertIn("100 new", out)
        self.assertIn("walking from the newest post", out)

        out = sync(since="2025-01-01")                                      # beyond the channel start
        st = json.loads(store_file.read_text())
        self.assertEqual(st["covered_from"], 0)
        self.assertEqual(len(st["posts"]), len(HISTORY))
        self.assertIn("whole channel", out)

    def test_6_server_error_gives_up_cleanly(self):
        FAKE.fail_before = 10
        with self.assertRaises(SystemExit) as e:
            run(mmscrape.sync, mmscrape.MM(self.url, TOKEN), "team1/history", since="2020-01-01", full=True)
        self.assertIn("after 4 retries", str(e.exception))
        FAKE.fail_before = 0

    def test_2_channel_list(self):
        out = run(mmscrape.list_channels, mmscrape.MM(self.url, TOKEN))
        self.assertIn("team1/team-support", out)
        self.assertNotIn("u1__u2", out)

    def test_3_expired_session(self):
        with self.assertRaises(SystemExit) as e:
            run(mmscrape.sync, mmscrape.MM(self.url, "expired"), "team1/team-support")
        self.assertIn("MMAUTHTOKEN", str(e.exception))

    def test_4_bad_ref(self):
        with self.assertRaises(SystemExit):
            mmscrape.resolve(mmscrape.MM(self.url, TOKEN), f"{self.url}/team1/pl/abc")


if __name__ == "__main__":
    unittest.main()
