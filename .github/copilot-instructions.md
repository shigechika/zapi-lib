# Repository overview

`zapi-lib` is a minimal Zabbix JSON-RPC API client for Python — a thin,
`httpx`-only wrapper over the single `/api_jsonrpc.php` endpoint
(`zapi_lib/client.py`). It is a **library**, not an application or an MCP
server: it has no CLI entry point and no stdio/transport code of its own.
It is consumed by at least two sibling repos — `zapi-mcp` (an MCP server)
and `speedtest-z` (a speedtest-to-Zabbix reporting tool) — both of which pin
a `zapi-lib` version. Changes here ripple to both without those repos'
maintainers seeing the diff, so review this repo with an "API contract for
someone else's code" mindset.

See `CLAUDE.md` for the authoritative command list and architecture notes.

# Build & validate

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

This mirrors `.github/workflows/ci.yml`: a dedicated lint job runs
`ruff check` + `ruff format --check` (unlike some sibling repos, lint/format
**is** enforced here — formatting nits are legitimate to flag), and a test
job runs `pytest` across Python 3.10/3.12/3.13 on Linux plus one Windows
3.12 job.

# What to focus review on in this repo

## 1. Public API stability — consumers pin this package's version

`zapi_lib/__init__.py`'s `__all__` is the contract: `ZapiClient`,
`ZapiProvisioner`, `ZapiError`, `ZapiAuthError`, `tag_filter`,
`TAG_OP_EQUAL`, `TAG_OP_EXISTS`, `DEFAULT_CONFIG_SECTION`. Flag a diff that
changes a public method's signature, return shape, or raised-exception type
without a clear compatibility note in the PR description — `zapi-mcp` and
`speedtest-z` depend on these staying stable across a minor bump. A
signature change to an underscore-prefixed helper (`_login`, `_tags`,
`_group_list`, …) doesn't break the public API by itself — but `_call` is
the exception: its *behavior* (not just signature) is the error contract
described in the next section, so treat behavior changes to it as
public-surface changes even though the name is private.

Don't ask a PR to bump `zapi_lib/__init__.py`'s `__version__` by hand —
that field is release-please-managed (see the `x-release-please-version`
marker) and is only ever touched by an automated `chore(main): release`
PR, verified against the git tag in `release.yml` before publishing.

## 2. Error contract: every failure is `ZapiError` or `ZapiAuthError`

`ZapiClient._call()` is the single place that talks to Zabbix, and it
normalizes every failure mode into one of two exceptions: an HTTP-status or
connection failure raises `ZapiError`, a Zabbix-side JSON-RPC error on
`user.login` raises `ZapiAuthError`, and a Zabbix-side error on any other
method raises `ZapiError`. Consumers catch these two types, not raw
`httpx.HTTPError`/`httpx.HTTPStatusError`. Flag a new code path that lets an
`httpx` exception, a `KeyError` on an unexpected response shape, or any
other raw exception escape a public method instead of going through
`_call()` (or being wrapped in `ZapiError` itself, as `count_problems` does
for a non-numeric `countOutput` result). This contract change would
silently break every consumer's `except ZapiError` handling.

## 3. Write paths: idempotency, partial failure, and safe defaults

`ZapiProvisioner` creates/updates real Zabbix objects (hosts, items,
maintenance windows), the higher-stakes surface — but `ZapiClient` is not
purely read-only either: `set_host_tag`, `acknowledge_problem`, and the
`create_group`/`ensure_group` group-creation path (all defined on
`ZapiClient`) also write. Review both:

- `ensure_group` / `_group_list` and `set_maintenance` are check-then-create
  (read for an existing group/window by name, create only if absent) —
  that's what makes them safe to call repeatedly. A new write helper that
  creates without first checking for an existing object breaks this
  established idempotency pattern; flag it.
- `create_host`/`update_host` call `_group_list()` (which may create a host
  group) and then `host.create`/`host.update` as two separate JSON-RPC
  calls with no rollback — if the group create succeeds and the host call
  then fails, the group is left behind. This is accepted as low-risk today
  because group creation is itself idempotent and safe to retry, not
  because it's transactional. Don't let a new provisioner method add a
  *non*-idempotent step ahead of a write (e.g. something that would double
  up on retry) without calling it out.
- Two different strategies keep `*.update` from wiping tags, and they are
  **opposites** — don't conflate them. `update_item` (and `update_host`)
  preserve tags by *omitting* the `tags` key, guarded by `if tags:`, because
  Zabbix's `*.update` replaces the entire tag set whenever `tags` is present
  at all — even `[]` — so adding an unconditional `tags: []` will clear/wipe
  existing tags. Conversely, the current `if tags:` guard means `update_host`
  cannot clear tags when no tags are provided. But `ZapiClient.set_host_tag` does the reverse: it *always*
  sends a fully-rebuilt, non-empty `tags` list — fetch the host's current
  tags, drop the same-named one, re-append the upsert — so its sent list is
  never empty and the "omit tags" reasoning does not apply to it. When
  rebuilding, it re-sends only the writable `{tag, value}` keys, deliberately
  stripping Zabbix 6.4+'s read-only `automatic` field that `host.get` returns
  but `host.update` rejects. A "simplification" that passes the fetched tags
  straight through to `host.update`, or that converts `set_host_tag` to the
  omit-`tags` pattern, is a bug — flag it.
- `acknowledge_problem` builds its action bitmask as `2 | (4 if message)` —
  acknowledge, plus add-message only when the message is non-empty (Zabbix
  rejects an empty message when bit 4 is set). It deliberately never sets the
  close bit (1), so it can acknowledge/comment but never *close* a problem
  (safe even when triggers disallow manual close; `zapi-mcp` exposes it as an
  MCP tool). A diff that ORs in the close bit silently turns a safe write into
  a destructive one — flag it.

## 4. Credentials handling

- `ZapiProvisioner.from_config()` reads Zabbix credentials from a
  `config.ini` `[zabbix]` section (`id`/`pw` or `user`/`password` aliases).
  Flag any diff that logs the parsed config values, the `password` field
  sent in `user.login`, or the session token/`Authorization: Bearer` header
  built in `_call()`. Today's logging (`self.logger.info(...)` in
  `ZapiProvisioner`) only logs version strings and maintenance-window
  names — keep it that way.
- Every current method passes values to Zabbix as JSON-RPC parameter dict
  entries via `httpx`, never string-concatenated into a URL or raw query.
  Flag a new code path that builds a request by string concatenation
  instead of adding to the `params` dict.
- `_login` retries with the alternate param name (`user` ↔ `username`) only
  when the first failure does *not* look like a credential error — it
  substring-matches `"incorrect"`/`"password"`/`"no permissions"` and
  re-raises those without a second attempt, so a genuine bad password costs
  exactly one login (no doubled lockout / audit pressure). A diff that
  removes or reorders this guard, or broadens the retry to every
  `ZapiAuthError`, doubles failed-login attempts against production Zabbix —
  flag it. (The substring match is also brittle across Zabbix
  versions/locales; changes to it warrant scrutiny.)

## 5. Test conventions

- HTTP-level tests use `respx` against the shared fake endpoint in
  `tests/conftest.py::make_router()` (dispatches by JSON-RPC `method`,
  emulates Zabbix's `countOutput`/`limit`/severity filtering), not
  `unittest.mock`.
- Nearly every method has a success-path test. On top of that, the core
  failure modes — `_call`'s auth/HTTP/API-error normalization, the
  non-numeric `countOutput` guard, missing host/group, blank config
  credentials — each have a dedicated error-path test asserting the
  specific `ZapiError`/`ZapiAuthError` message (e.g.
  `test_count_problems_raises_on_non_numeric_result`,
  `test_create_host_raises_without_any_group`). A new public method that
  can raise should get one of these too, following that pattern.

# Out of scope for review comments

- MCP/stdio/FastMCP-flavored advice: this library has no MCP transport
  code, no stdout/stdio protocol channel, and no tool-decorated functions.
  That review shape belongs to `zapi-mcp`, not here.
- Formatting/style nits are **in scope** here (see Build & validate above) —
  this is the opposite convention from some sibling repos, don't assume
  lint isn't enforced.
