# v1.9.2 — Team Activity setup accepts multi-line (wrapped) cookies

## The problem

On the v1.9.1 "Team Activity" setup page, pasting a real claude.ai Cookie header
could fail validation with:

> Team Activity line 2 is not in the format: ORG_UUID | Label | Cookie

Cookie headers are long and often arrive with hard line breaks when pasted, so a
single organization's entry spilled onto a second physical line — and the
validator treated that continuation as a malformed new entry.

## The fix

The setup page now **stitches wrapped cookies back together**. A line that
contains the `ORG_UUID | Label |` structure (two or more `|`) starts a new
organization; any following line without it is treated as a continuation of the
current organization's cookie and joined onto it. Cookies never contain `|`, so
this is unambiguous.

So you can paste one organization per line as before —

```
ORG_UUID | Label | Cookie
```

— and it no longer matters if the cookie wraps across several lines. The cookie
is reconstructed exactly (verified against a real header containing quotes and
`g_state={…}`), JSON-escaped, and written to `config.json`'s `analytics_orgs`.
The Team Activity box also now word-wraps for easier reading.

No changes to the collector or dashboard behavior — this is a setup-wizard fix
only.
