# db2q — read-only SQL for a coding agent

A one-file command a coding agent (e.g. Copilot agent mode) can run in the terminal to
query a database by itself, when MCP isn't available. Written for DB2 on z/OS through the
DataDirect JDBC driver and an SSH jump host, but any JDBC driver works.

- Opens its own SSH tunnel per call (password, keyboard-interactive or key), or connects directly.
- Read-only connection; only `SELECT` / `WITH` / `VALUES`, one statement at a time.
  This is a safety net: use a database user with SELECT grants only.
- Row cap (200 by default) and query timeout; prints tab-separated rows, compact for an LLM.
- `--tables` and `--describe` so the agent can explore the schema.

## Setup (WSL)

Java 17+ and [jbang](https://www.jbang.dev/) (the SSH library comes from Maven Central on
first run; behind a proxy, point `~/.m2/settings.xml` at your internal repository).

```bash
mkdir -p ~/.db2q && cp db2q.env.example ~/.db2q/db2q.env && chmod 600 ~/.db2q/db2q.env
# fill in hosts, users, passwords and the driver jar path (/mnt/c/... for a Windows jar)
jbang db2q.java --check
```

Keep the real config outside the workspace, so the agent never reads the passwords.
If internal host names don't resolve in WSL2, try `networkingMode=mirrored` under `[wsl2]`
in `%USERPROFILE%\.wslconfig`, then `wsl --shutdown`.

## Configuration

`KEY=VALUE` file, found at `--env FILE`, else `$DB2Q_ENV`, else `./db2q.env`, else
`~/.db2q/db2q.env`. `DB2Q_*` environment variables override it, and `--set KEY=VALUE`
overrides both. See [db2q.env.example](db2q.env.example) for every key.

| Key | Meaning |
|---|---|
| `DB2Q_DRIVER_JAR`, `DB2Q_DRIVER` | driver jar and class |
| `DB2Q_URL_TEMPLATE` | JDBC URL with `{host}`, `{port}`, `{database}` |
| `DB2Q_DB_HOST`, `DB2Q_DB_PORT`, `DB2Q_DB_NAME` | database, as seen from the SSH host |
| `DB2Q_USER`, `DB2Q_PASSWORD` | database credentials |
| `DB2Q_DEFAULT_SCHEMA` | schema for `--tables` / `--describe` without one |
| `DB2Q_PROP_<name>` | any extra driver property, passed as-is |
| `DB2Q_SSH_HOST` (empty = no tunnel), `_PORT`, `_USER`, `_PASSWORD`, `_KEY` | jump host |
| `DB2Q_SSH_STRICT` | `yes` to accept only hosts in `~/.ssh/known_hosts` |
| `DB2Q_MAX_ROWS`, `DB2Q_TIMEOUT` | row cap and query timeout (seconds) |

## Use it

```bash
jbang db2q.java --check
jbang db2q.java --tables                 # default schema
jbang db2q.java --describe MYTABLE
jbang db2q.java "SELECT * FROM MYTABLE FETCH FIRST 10 ROWS ONLY WITH UR"
echo "SELECT ..." | jbang db2q.java -
jbang db2q.java --max 1000 "SELECT ..."
```

## Hook it up to the agent

In the project's agent instructions (e.g. `.github/copilot-instructions.md`):

> To query the database, run `jbang <path>/db2q.java "<SQL>"`. Explore with `--tables` and
> `--describe TABLE` before writing queries. Always add `FETCH FIRST n ROWS ONLY` and `WITH UR`.
> Read-only: never attempt writes.

In VS Code, add only this command to the terminal auto-approve list
(`chat.tools.terminal.autoApprove`, e.g. `"/^jbang \\S*db2q\\.java/": true`) so the agent
doesn't ask before each query.
