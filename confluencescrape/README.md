# confluencescrape — make Confluence pages searchable with docrag

Downloads Confluence pages through the REST API (read-only) and writes them in docrag's
export format, **one `.txt` file per space**. They show up in docrag as docs, next to the
other exports and the Mattermost channels.

Basic version: fetches the pages you name (and optionally everything below them) and
re-downloads them on every run.

## Setup (WSL)

Use the same venv as docrag:

```bash
source ~/docrag-venv/bin/activate
pip install requests beautifulsoup4 markdownify
```

### Authentication

```bash
export CONFLUENCE_URL='https://<your-confluence-host>'      # Cloud: https://<site>.atlassian.net/wiki
export CONFLUENCE_TOKEN='<personal access token>'           # Profile → Personal Access Tokens
```

- **Confluence Cloud**: the token is an API token (id.atlassian.com → Security → API tokens),
  and you also `export CONFLUENCE_USER='<your email>'`.
- **No tokens allowed (SAML)**: use the browser session instead. F12 → Application → Cookies
  → the Confluence domain, then
  `export CONFLUENCE_COOKIE='JSESSIONID=<value>; seraph.confluence=<value>'` (whichever of these exist).

As with mmscrape, keep tokens and cookies out of files and the repo. **TLS error?** Run
`bash ../wsl-certs/import-windows-certs.sh` once.

## Use it

```bash
python confluencescrape.py fetch 'https://<host>/pages/viewpage.action?pageId=123456'
python confluencescrape.py fetch 'https://<host>/display/ENG/Base+Images' --tree   # plus all child pages
python confluencescrape.py fetch 123456 789012
python confluencescrape.py render          # rebuild the exports from confluence_data, no network

python ../docrag/docrag.py ingest ../path/to/doc-export/*.txt ../mmscrape/mm_export/*.jsonl confluence_export/*.txt
```

`ingest` rebuilds the index from the files you give it, so always pass every export you
want searchable (docs, Mattermost, Confluence), not only the new ones. If you leave some
out, it stops and lists them instead of removing them.

Accepted page references: the page id, `…?pageId=123`, `…/spaces/KEY/pages/123/…` (Cloud) and
`…/display/KEY/Page+Title`. Short links (`/x/AbCd`) aren't supported. Use
"…" → "Page information" to get the `pageId` URL.

## What gets stored

| Where | What |
|---|---|
| `confluence_data/<page-id>.json` | title, space, breadcrumb, last update, rendered HTML |
| `confluence_export/<Space_Name>.txt` | every fetched page of that space, as markdown, in tree order |

Each page starts with a line like `Platform Engineering > Base Images · updated 2026-09-14 by Alice Martin`,
followed by the page body. The body is the rendered HTML (`export_view`, so macros are
expanded) converted to markdown: headings, tables, lists, and code blocks with their language.
Images are dropped. Attachments and comments are not downloaded.
