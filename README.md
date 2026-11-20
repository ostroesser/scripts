# scripts

General-purpose utility scripts.

| Folder | What it does |
|---|---|
| [confluencescrape](confluencescrape/) | Downloads Confluence pages (or whole page trees) as markdown, searchable with docrag |
| [db2q](db2q/) | Read-only SQL command a coding agent can run itself: any JDBC driver, optional SSH tunnel, row cap |
| [docrag](docrag/) | Local hybrid search (BM25 + embeddings) over scraped documentation pages, producing paste-ready context for a chat LLM such as Copilot |
| [mmscrape](mmscrape/) | Downloads Mattermost channels (incremental, read-only) into one record per thread, searchable with docrag |
| [wsl-certs](wsl-certs/) | Makes WSL trust the Windows/corporate certificate authorities (Python, pip, Hugging Face, git, curl) |
