#!/usr/bin/env python3
"""
mmscrape — download Mattermost channels and render them for docrag.

  export MM_URL=https://mattermost.example.com
  export MM_TOKEN='<value of the MMAUTHTOKEN cookie>'

  python mmscrape.py channels                           # channels you are a member of
  python mmscrape.py sync myteam/team-support               # 1st run: full history; then only what's new
  python mmscrape.py sync https://mm.example.com/myteam/channels/team-support --since 2025-01-01
  python mmscrape.py render                             # re-render every synced channel
  python ../docrag/docrag.py ingest mm_export/*.jsonl

Raw posts are kept in ./mm_data (one JSON file per channel). Exports go to ./mm_export:
one .jsonl file per channel, one record per thread (title, permalink, channel, dates, text),
which docrag indexes directly.
Read-only: GET requests only, plus POST /users/ids to turn user ids into names.
"""
import argparse, json, os, re, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ["MM_TZ"]) if os.environ.get("MM_TZ") else None   # None: system local time
except Exception:                                   # no tz database: fall back to UTC
    TZ = timezone.utc

DATA = Path(os.environ.get("MM_DATA", "./mm_data"))
EXPORT = Path(os.environ.get("MM_EXPORT", "./mm_export"))
PER_PAGE = 200

# ---------------------------------------------------------------- API client
class ApiError(SystemExit):
    """An API failure: stops the CLI with its message, but can be caught."""
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status = status


class MM:
    def __init__(self, url, token):
        self.base = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "X-Requested-With": "XMLHttpRequest"})

    def call(self, method, path, **kw):
        server_errors = 0
        for _ in range(12):
            try:
                r = self.s.request(method, f"{self.base}/api/v4{path}", timeout=90, **kw)
            except requests.ConnectionError as e:   # network hiccup: treat like a server error
                r, err = None, str(e)
            if r is not None and r.status_code == 429:   # rate limited: wait as told, then retry
                wait = r.headers.get("Retry-After") or r.headers.get("X-Ratelimit-Reset") or 2
                time.sleep(min(max(float(wait), 1), 60))
                continue
            if r is None or r.status_code >= 500:   # transient server error: back off and retry
                server_errors += 1
                if server_errors > 4:
                    raise ApiError(f"\nServer error on {path} after 4 retries: "
                                   f"{err if r is None else r.text[:300]}", 500 if r is None else r.status_code)
                wait = 2 ** server_errors
                print(f"\n  server error ({'network' if r is None else r.status_code}), retrying in {wait}s …",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code == 401:
                sys.exit("\nHTTP 401: the session has expired or the token is wrong.\n"
                         "Copy a fresh MMAUTHTOKEN cookie from your browser into MM_TOKEN.")
            if r.status_code >= 400:
                raise ApiError(f"\nHTTP {r.status_code} on {path}: {r.text[:300]}", r.status_code)
            return r.json()
        sys.exit(f"\nStill rate limited after several retries on {path}. Try again later.")

    def get(self, path, **params):
        return self.call("GET", path, params=params)


def client():
    url, token = os.environ.get("MM_URL"), os.environ.get("MM_TOKEN")
    if not url or not token:
        sys.exit("Set MM_URL (e.g. https://mattermost.example.com) and MM_TOKEN (MMAUTHTOKEN cookie value).")
    return MM(url, token)

# ---------------------------------------------------------------- storage
def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)                               # atomic: an interrupted run can't corrupt the store


def trim(p):
    """Keep only what's needed to render a post."""
    props = p.get("props") or {}
    files = [f.get("name") for f in ((p.get("metadata") or {}).get("files") or []) if f.get("name")]
    attachments = []                                # bot / webhook message attachments
    for a in props.get("attachments") or []:
        bits = [a.get("pretext"), a.get("title"), a.get("text")]
        bits += [f"{f.get('title')}: {f.get('value')}" for f in a.get("fields") or []]
        text = "\n".join(b for b in bits if b) or a.get("fallback")
        if text:
            attachments.append(text)
    return {"id": p["id"], "create_at": p["create_at"], "update_at": p.get("update_at", 0),
            "edit_at": p.get("edit_at", 0), "user_id": p.get("user_id", ""),
            "root_id": p.get("root_id", ""), "message": p.get("message", ""),
            "files": files, "attachments": attachments,
            "override_username": props.get("override_username")}


def merge(store, posts, stats=None):
    """Merge raw API posts into the store: newest version wins, deleted and system posts are dropped.
    Counts real changes in stats: new, edited, deleted."""
    stats = stats if stats is not None else {}
    for p in posts.values():
        if p.get("delete_at"):
            if store["posts"].pop(p["id"], None):
                stats["deleted"] = stats.get("deleted", 0) + 1
        elif (p.get("type") or "").startswith("system_"):
            continue
        else:
            old = store["posts"].get(p["id"])
            if not old:
                stats["new"] = stats.get("new", 0) + 1
            elif p.get("update_at", 0) > old["update_at"]:
                stats["edited"] = stats.get("edited", 0) + 1
            else:
                continue
            store["posts"][p["id"]] = trim(p)
    return stats

# ---------------------------------------------------------------- fetching
def date_ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=TZ).timestamp() * 1000)


def resolve(mm, ref):
    """'team/channel' or a channel URL -> (team name, channel object)."""
    if ref.startswith("http"):
        path = urlparse(ref).path
        prefix = urlparse(mm.base).path.rstrip("/")  # server may live under a sub-path
        parts = path[len(prefix):].strip("/").split("/") if path.startswith(prefix) else []
        if len(parts) < 3 or parts[1] != "channels":
            sys.exit(f"Not a channel URL: {ref}  (expected …/<team>/channels/<channel>)")
        team, name = parts[0], parts[2]
    elif "/" in ref:
        team, name = ref.split("/", 1)
    else:
        sys.exit(f"Use team/channel or a channel URL, got: {ref}  (see: mmscrape.py channels)")
    return team, mm.get(f"/teams/name/{team}/channels/name/{name}")


def fetch_history(mm, channel_id, stop_at=None, since_ms=None, before=""):
    """Walk the channel from newest to oldest with the `before` cursor (or from `before`).
    Stops at posts we already have (stop_at), older than the history limit (since_ms),
    or at the start of the channel.
    Returns (posts, oldest walked post (create_at, id) or None, reached the channel start)."""
    got, oldest, reached_start = {}, None, False
    while True:
        params = {"per_page": PER_PAGE}
        if before:
            params["before"] = before
        data = mm.get(f"/channels/{channel_id}/posts", **params)
        order, posts = data.get("order") or [], data.get("posts") or {}
        done = reached_start = not order
        for pid in order:
            p = posts[pid]
            if (stop_at is not None and p["create_at"] <= stop_at) or (since_ms and p["create_at"] < since_ms):
                done = True
            else:
                got[pid] = p
                if oldest is None or p["create_at"] < oldest[0]:
                    oldest = (p["create_at"], pid)
        for pid, p in posts.items():                # thread roots the API adds outside `order`
            if pid not in order and pid not in got:
                got[pid] = p
        print(f"\r  {len(got):,} posts fetched …", end="", file=sys.stderr, flush=True)
        if done:
            break
        before = order[-1]
    print(file=sys.stderr)
    return got, oldest, reached_start


def fetch_missing_roots(mm, store, limit=1000):
    """Replies whose thread root isn't stored (e.g. root older than --since): fetch the whole thread."""
    missing = {p["root_id"] for p in store["posts"].values() if p["root_id"]} - store["posts"].keys()
    for n, rid in enumerate(sorted(missing)[:limit], 1):
        print(f"\r  completing threads {n}/{min(len(missing), limit)} …", end="", file=sys.stderr, flush=True)
        try:
            merge(store, mm.get(f"/posts/{rid}/thread").get("posts") or {})
        except SystemExit as e:                     # deleted or inaccessible root: keep the replies
            if "401" in str(e):
                raise
    if missing:
        print(file=sys.stderr)


def update_users(mm, store_posts, users):
    ids = {p["user_id"] for p in store_posts.values() if p["user_id"]} - users.keys()
    ids = sorted(ids)
    for i in range(0, len(ids), 100):
        for u in mm.call("POST", "/users/ids", json=ids[i:i + 100]):
            full = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
            users[u["id"]] = full or u.get("nickname") or u.get("username") or u["id"][:8]


def sync(mm, ref, full=False, since=None):
    team, ch = resolve(mm, ref)
    path = DATA / f"{ch['id']}.json"
    store = load_json(path, None)
    if full or not store:
        store = {"posts": {}, "newest": 0, "synced_at": 0}
    store["channel"] = {"id": ch["id"], "name": ch["name"], "display_name": ch.get("display_name") or ch["name"],
                        "team": team, "header": ch.get("header", "")}
    label = f"{team}/{ch['name']}"
    started = int(time.time() * 1000)

    since_ms = date_ms(since) if since else None
    stats = {}

    def walk_back(before=""):
        """Fetch history from `before` (or the newest post) back to --since, remember how far we got."""
        posts, oldest, reached_start = fetch_history(mm, ch["id"], since_ms=since_ms, before=before)
        merge(store, posts, stats)
        if reached_start or not since_ms:
            store["covered_from"], store["covered_from_id"] = 0, None   # whole channel history
        else:
            store["covered_from"] = since_ms
            if oldest:
                store["covered_from_id"] = oldest[1]

    if not store["posts"]:
        print(f"{label}: full download" + (f" since {since}" if since else "") +
              f" (~{ch.get('total_msg_count', '?')} messages in channel)")
        walk_back()
    else:
        print(f"{label}: incremental update")
        merge(store, fetch_history(mm, ch["id"], stop_at=store["newest"])[0], stats)
        # edits and deletions of older posts since the last run (5 min overlap)
        merge(store, mm.get(f"/channels/{ch['id']}/posts", since=store["synced_at"] - 300_000).get("posts") or {}, stats)

        covered = store.get("covered_from")     # None: store made by an older version, unknown
        if since_ms is not None and (covered is None or since_ms < covered):
            cursor = store.get("covered_from_id") if covered else None
            print(f"{label}: fetching older history back to {since}"
                  + ("" if cursor else " (walking from the newest post)"))
            try:
                walk_back(cursor or "")
            except ApiError as e:               # cursor post gone (deleted): walk from the newest
                if not cursor or e.status < 400 or e.status == 401:
                    raise
                print(f"  could not continue from the oldest stored post, walking from the newest", file=sys.stderr)
                walk_back()
        elif since_ms is not None:
            print(f"{label}: history since {since} already downloaded")

    fetch_missing_roots(mm, store)
    store["newest"] = max((p["create_at"] for p in store["posts"].values()), default=0)
    store["synced_at"] = started
    save_json(path, store)

    users = load_json(DATA / "users.json", {})
    update_users(mm, store["posts"], users)
    save_json(DATA / "users.json", users)

    out = write_export(store, users, mm.base)
    changes = ", ".join(f"{stats.get(k, 0):,} {k}" for k in ("new", "edited", "deleted"))
    span = ("whole channel" if store.get("covered_from") == 0 else
            f"since {fmt_time(store['covered_from'], '%Y-%m-%d')}" if store.get("covered_from") else "")
    print(f"{label}: {changes} -> {len(store['posts']):,} posts stored"
          + (f" ({span})" if span else "") + f" -> {out}")

# ---------------------------------------------------------------- rendering
def fmt_time(ms, fmt="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime(fmt)


def snippet(text, n=80):
    s = re.sub(r"\s+", " ", re.sub(r"[#*_`>]+", "", text)).strip()
    return (s[:n].rsplit(" ", 1)[0] + "…") if len(s) > n else (s or "(no text)")


def author(p, users):
    return p.get("override_username") or users.get(p["user_id"]) or p["user_id"][:8] or "unknown"


def render_post(p, users):
    head = f"**{author(p, users)}** · {fmt_time(p['create_at'])}" + (" (edited)" if p["edit_at"] else "")
    parts = [head]
    if p["message"].strip():
        parts.append(p["message"].strip())
    for a in p["attachments"]:
        parts.append("[attachment] " + a.strip())
    if p["files"]:
        parts.append("[files: " + ", ".join(p["files"]) + "]")
    return "\n".join(parts)


def build_records(store, users, server):
    """One record per thread (root + replies); a message without replies is its own record."""
    ch, posts = store["channel"], store["posts"]
    disp = ch["display_name"]
    link = lambda pid: f"{server}/{ch['team']}/pl/{pid}"
    by_root, roots = defaultdict(list), []
    for p in posts.values():
        (by_root[p["root_id"]] if p["root_id"] else roots).append(p)
    key = lambda p: p["create_at"]

    def record(first, msgs, root_missing=False):
        msgs = sorted(msgs, key=key)
        n = len(msgs) - (0 if root_missing else 1)
        started = fmt_time(msgs[0]["create_at"], "%Y-%m-%d")
        head = "(original message unavailable) " if root_missing else ""
        text = (f"{disp} · thread started {started} by {author(first, users)}"
                f" · {n} repl{'y' if n == 1 else 'ies'}\n\n" if n else
                f"{disp} · {started} · no replies\n\n")
        text += "\n\n".join(render_post(p, users) for p in msgs)
        return {"id": first["id"], "title": f"{disp} · {started} · {head}{snippet(first['message'])}",
                "url": link(first["id"]), "source": disp, "channel": f"{ch['team']}/{ch['name']}",
                "date": fmt_time(msgs[-1]["create_at"], "%Y-%m-%d"),   # last activity, used by --since
                "started": started, "replies": n, "text": text}

    out = [record(r, [r] + by_root.pop(r["id"], [])) for r in roots]
    out += [record(sorted(rs, key=key)[0], rs, root_missing=True) for rs in by_root.values()]
    return sorted(out, key=lambda r: (r["started"], r["id"]))


def write_export(store, users, server):
    ch = store["channel"]
    out = EXPORT / f"{ch['team']}__{ch['name']}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in build_records(store, users, server):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out

# ---------------------------------------------------------------- commands
def list_channels(mm, show_all=False):
    teams = mm.get("/users/me/teams")
    rows = []
    for t in teams:
        for c in mm.get(f"/users/me/teams/{t['id']}/channels"):
            if show_all or c.get("type") in ("O", "P"):
                rows.append((f"{t['name']}/{c['name']}", c.get("display_name") or c["name"],
                             c.get("type"), c.get("total_msg_count", 0)))
    for ref, disp, typ, cnt in sorted(rows):
        print(f"{ref:<50} {cnt:>8,}  {'private ' if typ == 'P' else ''}{disp}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("channels", help="list the channels you are a member of")
    c.add_argument("--all", action="store_true", help="include direct and group messages")
    s = sub.add_parser("sync", help="download or update channels, then render them")
    s.add_argument("channels", nargs="+", help="team/channel or channel URL")
    s.add_argument("--since", help="history start YYYY-MM-DD; an earlier date than before backfills the gap")
    s.add_argument("--full", action="store_true", help="discard the local copy and download again")
    sub.add_parser("render", help="re-render all synced channels (no network)")
    args = ap.parse_args()

    if args.cmd == "channels":
        list_channels(client(), args.all)
    elif args.cmd == "sync":
        mm = client()
        for ref in args.channels:
            sync(mm, ref, full=args.full, since=args.since)
    else:
        users = load_json(DATA / "users.json", {})
        server = os.environ.get("MM_URL", "").rstrip("/")
        for f in sorted(DATA.glob("*.json")):
            if f.name != "users.json":
                print(write_export(load_json(f, None), users, server))


if __name__ == "__main__":
    main()
