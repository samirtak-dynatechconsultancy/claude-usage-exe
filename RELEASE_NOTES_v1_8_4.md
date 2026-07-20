# v1.8.4 — Fix "unable to open database file" in usage reader

## The bug

The `usage` command crashed before it could read anything:

```
sqlite3.OperationalError: unable to open database file
  desktop_usage.py, line 193, in get_session_cookie
```

`get_session_cookie` opened the copied cookie DB with:

```python
sqlite3.connect(f"{db_copy}?immutable=1", uri=True)
```

With `uri=True`, SQLite only honours `?immutable=1` when the string is a proper
`file:` URI. `db_copy` is a raw Windows path, so SQLite treated the entire
string — including `?immutable=1` — as a literal filename and failed to open it.

Because this happened *before* the read, the usage data never reached
`/api/ingest`, so the dashboard stayed empty.

## The fix

Build a real file URI:

```python
db_uri = Path(db_copy).as_uri() + "?immutable=1"   # file:///C:/.../tmp.db?immutable=1
sqlite3.connect(db_uri, uri=True)
```

Verified: the old form reproduces the exact error; the new form opens the DB and
returns the `sessionKey` row.

## Flow reminder

collector reads usage → POSTs to `{server_url}/api/ingest` → dashboard API writes
to Supabase. This release fixes the *read* so the POST can happen. If the
dashboard still shows nothing after upgrading, the next place to look is the
server (`/api/ingest` handler + UI) in `claude-usage-dashboard`.

No config or schedule changes. Admin caveat unchanged.
