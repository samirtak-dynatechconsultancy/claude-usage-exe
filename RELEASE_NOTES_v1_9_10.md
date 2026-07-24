# v1.9.10 — Team Activity tolerates Postman-style cookie pastes

Some cookies get pasted into `config.json` in a Postman/Insomnia header-export
shape instead of a raw Cookie header, e.g.

```
[{"key":"Cookie","value":"ion-vk=...; sessionKey=...","enabled":true}]
```

The collector now detects that (a JSON array or object) and extracts the real
Cookie value automatically, so Team Activity collection no longer fails for an
org whose cookie was pasted that way. A plain `name=value; ...` cookie still
works unchanged.

Tip: a raw Cookie header is still the cleanest thing to paste — this is just a
safety net.
