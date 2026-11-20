---
name: db2-query
description: Query the DB2 for z/OS test database read-only with the db2q command. Use when a task needs real data, table or column definitions, or to check what a program reads or writes in DB2 (SQL, DB2 tables, schema, catalog, "what's in the database").
---

# Querying DB2 for z/OS with db2q

`db2q` runs one read-only SQL statement against the configured DB2 test database and prints
tab-separated rows. Connection, SSH tunnel and credentials are already configured: never ask
for them, never read or print the config file (`~/.db2q/`), never put credentials in commands.

Always run it through the wrapper next to this file:

```bash
~/.copilot/skills/db2-query/db2q.sh <arguments>
```

## Commands

| Command | Use |
|---|---|
| `db2q.sh --check` | connection test (server, SQLID, schema). Run first if anything fails. |
| `db2q.sh --tables [SCHEMA] [PATTERN]` | list tables/views; PATTERN uses `%`, e.g. `--tables MYSCHEMA 'PAY%'` |
| `db2q.sh --describe [SCHEMA.]TABLE` | columns, types, nullability |
| `db2q.sh "SELECT ..."` | run a query (single-quote-safe: prefer double quotes outside, single quotes for SQL literals) |
| `db2q.sh --max N "SELECT ..."` | raise the 200-row cap, only when really needed |

Without a schema, the configured default schema is used.

## Workflow

1. **Explore before querying.** Find the table with `--tables`, check its columns with
   `--describe`. Never guess column names.
2. **Count before fetching** when the size is unknown: `SELECT COUNT(*) ... WITH UR`.
3. **Query narrowly.** Name the columns you need, filter, and always end with
   `FETCH FIRST n ROWS ONLY WITH UR` (n ≤ 50 unless the task needs more).
4. **Report** the SQL you ran and summarise the result; quote only the rows that matter.

## Rules

- Read-only. Only `SELECT`, `WITH`, `VALUES`; one statement, no trailing `;`. The tool refuses
  anything else; don't try to work around it. If the user wants data changed, write the SQL
  for them to review and run themselves.
- This is a mainframe: every query costs CPU. No `SELECT *` on wide tables, no unfiltered joins
  of large tables, no repeated polling. Prefer filters on indexed / key columns.
- `WITH UR` on every query so it never waits on or takes locks.
- Output ending in `(TRUNCATED at N ...)` means there were more rows: refine the query rather
  than raising `--max`.

## DB2 for z/OS SQL reminders

- Row limit: `FETCH FIRST 10 ROWS ONLY`, not `LIMIT`. Paging: `OFFSET n ROWS` before it.
- Constant query: `SELECT CURRENT TIMESTAMP FROM SYSIBM.SYSDUMMY1`.
- Concatenation `||`; `CHAR` columns are blank-padded (use `RTRIM` in output and `LIKE`).
- Dates: `CURRENT DATE`, `DATE(ts)`, `ts - 7 DAYS`, `VARCHAR_FORMAT(ts, 'YYYY-MM-DD HH24:MI:SS')`.
- Sort order is EBCDIC: lowercase < uppercase < digits.
- Catalog, when `--tables` / `--describe` aren't enough:
  - tables: `SYSIBM.SYSTABLES` (`CREATOR`, `NAME`, `TYPE`, `CARDF` = row estimate, `REMARKS`)
  - columns: `SYSIBM.SYSCOLUMNS` (`TBCREATOR`, `TBNAME`, `NAME`, `COLNO`, `COLTYPE`, `LENGTH`, `SCALE`, `NULLS`)
  - indexes / keys: `SYSIBM.SYSINDEXES` (`TBCREATOR`, `TBNAME`, `NAME`, `UNIQUERULE`) joined to
    `SYSIBM.SYSKEYS` (`IXCREATOR`, `IXNAME`, `COLNAME`, `COLSEQ`)
  - foreign keys: `SYSIBM.SYSRELS`, `SYSIBM.SYSFOREIGNKEYS`

## Errors

| Message | Meaning / what to do |
|---|---|
| `db2q: refused: ...` | statement isn't a single read; rewrite it as one SELECT |
| SQLCODE -204 | table/view not found: wrong name or schema, check with `--tables` |
| SQLCODE -206 | unknown column: check with `--describe` |
| SQLCODE -551 / -552 | no authority: tell the user, don't look for another table to bypass it |
| SQLCODE -904, -911, -913 | resource unavailable, deadlock or timeout: wait, retry once, then report |
| SQLCODE -805 | driver package problem on the server: report to the user, don't retry |
| `SSH tunnel failed` / connection errors | run `--check` once; if it still fails, stop and report (VPN, password, host down) |
| `missing config ...` | setup problem: report it, never invent values |
