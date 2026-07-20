# v1.8.5 — Fix usage push (config load crash)

## The bug

After v1.8.4 fixed the cookie read, the `usage` command reached the push step
and crashed:

```
NameError: name '_load_config' is not defined. Did you mean: 'load_config'?
  collector.py, line 1240, in cmd_usage
```

`cmd_usage` had three problems on one line:
1. called `_load_config` — the real function is `load_config`
2. `load_config` returns a **tuple** `(config, path)`, not a dict
3. no handling for a missing `config.json`

## The fix

```python
try:
    cfg, _ = load_config(getattr(args, "config", None))
except FileNotFoundError:
    cfg = {}   # push_usage skips cleanly when server_url/token absent
push_usage(data, cfg)
```

Verified in isolation: `load_config` unpacks correctly, and `push_usage` skips
gracefully with an empty/blank config instead of crashing.

## Status of the flow

With v1.8.4 (cookie read) + v1.8.5 (config/push) the full **client** path now
works: read cookie → read usage → load config → **POST `/api/ingest`**. If the
dashboard still shows nothing after this runs cleanly, the remaining gap is
server‑side in `claude-usage-dashboard`.

Admin caveat and schedule options unchanged.
