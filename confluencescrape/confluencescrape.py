#!/usr/bin/env python3
"""
confluencescrape — download Confluence pages and render them for docrag.

  export CONFLUENCE_URL=https://confluence.example.com   # Cloud: https://<site>.atlassian.net/wiki
  export CONFLUENCE_TOKEN='<personal access token>'      # Data Center PAT (Bearer)
  # Cloud API token instead: also export CONFLUENCE_USER=<your email>
  # No token possible (SAML): export CONFLUENCE_COOKIE='JSESSIONID=…; seraph.confluence=…' instead

  python confluencescrape.py fetch 'https://confluence.example.com/pages/viewpage.action?pageId=123456'
  python confluencescrape.py fetch 123456 --tree     # the page and every page below it
  python confluencescrape.py render                  # re-render everything fetched (no network)
  python ../docrag/docrag.py ingest <doc exports>/*.txt ../mmscrape/mm_export/*.jsonl confluence_export/*.txt
      # ingest rebuilds the index from the files given: always pass all of them

Raw pages are kept in ./confluence_data (one JSON file per page). Exports go to ./confluence_export:
one .txt file per space, in docrag's page-block format (Title, Source, markdown body).
Read-only: GET requests only.
"""
import argparse, json, os, re, sys, time
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

DATA = Path(os.environ.get("CONFLUENCE_DATA", "./confluence_data"))
EXPORT = Path(os.environ.get("CONFLUENCE_EXPORT", "./confluence_export"))
EXPAND = "body.export_view,version,space,ancestors"   # export_view: HTML with macros rendered

# ---------------------------------------------------------------- API client
class Confluence:
    def __init__(self, url, token=None, user=None, cookie=None):
        url = url.rstrip("/")
        if urlparse(url).hostname.endswith(".atlassian.net") and not url.endswith("/wiki"):
            url += "/wiki"                          # Cloud serves Confluence under /wiki
        self.base = url
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"
        if cookie:
            self.s.headers["Cookie"] = cookie
        elif user:
            self.s.auth = (user, token)             # Cloud: email + API token
        else:
            self.s.headers["Authorization"] = f"Bearer {token}"   # Data Center personal access token

    def get(self, path, **params):
        """GET base + path (path may already carry a query string, e.g. a `next` link)."""
        server_errors = 0
        for _ in range(12):
            try:
                r = self.s.get(f"{self.base}{path}", params=params, timeout=90, allow_redirects=False)
            except requests.ConnectionError as e:   # network hiccup: treat like a server error
                r, err = None, str(e)
            if r is not None and r.status_code == 429:   # rate limited: wait as told, then retry
                time.sleep(min(max(float(r.headers.get("Retry-After") or 2), 1), 60))
                continue
            if r is None or r.status_code >= 500:   # transient server error: back off and retry
                server_errors += 1
                if server_errors > 4:
                    sys.exit(f"\nServer error on {path} after 4 retries: {err if r is None else r.text[:300]}")
                wait = 2 ** server_errors
                print(f"\n  server error ({'network' if r is None else r.status_code}), retrying in {wait}s …",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code in (401, 403) or 300 <= r.status_code < 400:   # SSO redirects to a login page
                sys.exit(f"\nHTTP {r.status_code} on {path}: not logged in, or no access to this page.\n"
                         "Check CONFLUENCE_TOKEN (or copy a fresh session cookie into CONFLUENCE_COOKIE).")
            if r.status_code >= 400:
                sys.exit(f"\nHTTP {r.status_code} on {path}: {r.text[:300]}")
            if "json" not in r.headers.get("Content-Type", ""):
                sys.exit(f"\n{path} returned {r.headers.get('Content-Type')} instead of JSON: "
                         "probably a login page. Check CONFLUENCE_URL and your credentials.")
            return r.json()
        sys.exit(f"\nStill rate limited after several retries on {path}. Try again later.")


def client():
    url, token = os.environ.get("CONFLUENCE_URL"), os.environ.get("CONFLUENCE_TOKEN")
    cookie = os.environ.get("CONFLUENCE_COOKIE")
    if not url or not (token or cookie):
        sys.exit("Set CONFLUENCE_URL (e.g. https://confluence.example.com) and CONFLUENCE_TOKEN "
                 "(personal access token), or CONFLUENCE_COOKIE (browser session cookie).")
    return Confluence(url, token, os.environ.get("CONFLUENCE_USER"), cookie)

# ---------------------------------------------------------------- fetching
def page_id(confluence, ref):
    """Page id, or a page URL in any of the usual forms -> page id."""
    if ref.isdigit():
        return ref
    u = urlparse(ref)
    if "pageId" in parse_qs(u.query):                       # …/viewpage.action?pageId=123
        return parse_qs(u.query)["pageId"][0]
    m = re.search(r"/pages/(\d+)", u.path)                  # …/spaces/KEY/pages/123/Title
    if m:
        return m.group(1)
    m = re.search(r"/display/([^/]+)/([^/?#]+)", u.path)    # …/display/KEY/Page+Title
    if m:
        space, title = m.group(1), unquote_plus(m.group(2))
        found = confluence.get("/rest/api/content", spaceKey=space, title=title, type="page").get("results") or []
        if not found:
            sys.exit(f"No page titled {title!r} in space {space}.")
        return found[0]["id"]
    sys.exit(f"Can't find a page id in: {ref}\n"
             "Use the page id, or the URL from the page's '…' menu → 'Page information' (has ?pageId=).")


def child_ids(confluence, pid):
    ids, path, params = [], f"/rest/api/content/{pid}/child/page", {"limit": 100}
    while path:
        data = confluence.get(path, **params)
        ids += [c["id"] for c in data.get("results") or []]
        path, params = (data.get("_links") or {}).get("next"), {}   # next link carries its own query
    return ids


def trim(p, base):
    """Keep only what's needed to render a page."""
    links, version = p.get("_links") or {}, p.get("version") or {}
    return {"id": p["id"], "title": p["title"],
            "space_key": (p.get("space") or {}).get("key", ""),
            "space": (p.get("space") or {}).get("name") or (p.get("space") or {}).get("key", ""),
            "url": (links.get("base") or base) + links.get("webui", f"/pages/viewpage.action?pageId={p['id']}"),
            "ancestors": [a["title"] for a in p.get("ancestors") or []],
            "updated": (version.get("when") or "")[:10],
            "updated_by": (version.get("by") or {}).get("displayName", ""),
            "version": version.get("number", 0),
            "html": ((p.get("body") or {}).get("export_view") or {}).get("value", "")}


def fetch(confluence, refs, tree=False):
    todo, seen = [page_id(confluence, r) for r in refs], set()
    while todo:
        pid = todo.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        page = trim(confluence.get(f"/rest/api/content/{pid}", expand=EXPAND), confluence.base)
        save_json(DATA / f"{pid}.json", page)
        print(f"  {page['space_key']} · {' > '.join(page['ancestors'] + [page['title']])}", file=sys.stderr)
        if tree:
            todo += child_ids(confluence, pid)
    print(f"{len(seen)} page{'s' if len(seen) != 1 else ''} fetched", file=sys.stderr)

# ---------------------------------------------------------------- storage
def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)                               # atomic: an interrupted run can't corrupt the store

# ---------------------------------------------------------------- rendering
def code_language(el):
    """Language of a Confluence code macro (Data Center: data-syntaxhighlighter-params="brush: java; …")."""
    m = re.search(r"brush:\s*([\w+#-]+)", el.get("data-syntaxhighlighter-params") or "")
    return m.group(1) if m else ""


def to_markdown(html):
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "img"]):
        el.decompose()
    md = MarkdownConverter(heading_style="ATX", bullets="-", code_language_callback=code_language).convert_soup(soup)
    md = md.replace("\xa0", " ")
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def render_page(p):
    crumbs = " > ".join([p["space"]] + p["ancestors"])
    head = f"{crumbs} · updated {p['updated']}" + (f" by {p['updated_by']}" if p["updated_by"] else "")
    return f"{head}\n\n{to_markdown(p['html'])}"


def export_name(space):
    """'Platform Engineering' -> 'Platform_Engineering.txt' (docrag shows it as 'Platform Engineering')."""
    return (re.sub(r"[^\w]+", "_", space).strip("_") or "confluence") + ".txt"


def render():
    """One docrag export file per space, pages in tree order."""
    by_space = {}
    for f in sorted(DATA.glob("*.json")):
        p = load_json(f)
        by_space.setdefault(p["space"], []).append(p)
    EXPORT.mkdir(parents=True, exist_ok=True)
    for space, pages in sorted(by_space.items()):
        pages.sort(key=lambda p: [t.lower() for t in p["ancestors"] + [p["title"]]])
        out = EXPORT / export_name(space)
        with out.open("w", encoding="utf-8") as f:
            for n, p in enumerate(pages, 1):
                f.write(f"{'=' * 32}\nDOCUMENT PAGE {n}\nTitle: {p['title']}\nSource: {p['url']}\n"
                        f"{'=' * 32}\n{render_page(p)}\n\n")
        print(f"{out}  ({len(pages)} page{'s' if len(pages) != 1 else ''})")

# ---------------------------------------------------------------- commands
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download pages, then render")
    f.add_argument("pages", nargs="+", help="page id or page URL")
    f.add_argument("--tree", action="store_true", help="also every page below them")
    sub.add_parser("render", help="re-render all fetched pages (no network)")
    args = ap.parse_args()

    if args.cmd == "fetch":
        fetch(client(), args.pages, tree=args.tree)
    render()


if __name__ == "__main__":
    main()
