# CLAUDE.md

## Overview

`zapi-lib` is a minimal Zabbix JSON-RPC API client for Python — a thin,
`httpx`-only wrapper over the single `/api_jsonrpc.php` endpoint. It was spun
out of `zapi-mcp` so that consumers that only need the API client (e.g.
`speedtest-z`) can depend on it without pulling in the MCP server stack. This
repo is a **library**, not an application or MCP server: no CLI entry point,
no stdio/transport concerns, just an importable package.

## Commands

- Install dev deps: `uv sync --dev`
- Run tests: `uv run pytest -v`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Single test file: `uv run pytest -v tests/test_client.py`

## Architecture

- `zapi_lib/client.py` — the whole library lives in one module:
  - `ZapiClient` — version-adaptive login (`user`+`auth` field for Zabbix
    <=6.2, `username`+`Authorization: Bearer` for 6.4/7.0, degrading to the
    other param name only when the first failure is *not* a credential
    error — a genuine bad password is never retried, to avoid doubling
    lockout/audit pressure). Read helpers (`get_hosts`, `get_items`,
    `get_problems`, `count_problems`, `get_events`, `get_group_id`,
    `get_host_ids`/`get_host_ids_by_tag`/`get_item_ids`); write helpers
    (`set_host_tag`, `acknowledge_problem`, and the group-creation path
    `create_group`/`ensure_group`); and `call()` as an escape hatch for
    JSON-RPC methods without a dedicated wrapper. Construction itself hits
    the network (`apiinfo.version` + `user.login`) and closes the httpx
    client if that raises (since `__enter__`/`__exit__` never run for a
    failed constructor).
  - `ZapiProvisioner(ZapiClient)` — config-driven (`config.ini` `[zabbix]`
    section via `from_config`) provisioning: `create_host`/`update_host`/
    `create_item`/`update_item`/`set_maintenance` (the group/id helpers
    above are inherited from `ZapiClient`, not defined here). Auto-creates
    trapper hosts/items tagged with a managed-by marker; the default group
    is resolved read-only at construction and created lazily on the first
    host write, so constructing a provisioner has no write side effect.
  - `ZapiError` (base) / `ZapiAuthError` (login failures) — the only
    exceptions the library raises; consumers catch these, not raw `httpx`
    errors.
- `zapi_lib/__init__.py` re-exports the public surface via `__all__`:
  `ZapiClient`, `ZapiProvisioner`, `ZapiError`, `ZapiAuthError`, `tag_filter`,
  `TAG_OP_EQUAL`, `TAG_OP_EXISTS`, `DEFAULT_CONFIG_SECTION`. Anything not
  listed there is internal.
- `tests/conftest.py` provides `make_router()`, a `respx`-mocked fake
  JSON-RPC endpoint that dispatches by the `method` field and emulates
  Zabbix's `countOutput`/`limit`/severity-filtering behavior server-side.

## Conventions

- Python >=3.10; `X | Y` union syntax is used directly (no
  `from __future__ import annotations`).
- Single runtime dependency: `httpx`. Avoid adding another one without
  discussion — that's the reason this package exists apart from `zapi-mcp`.
- HTTP-level tests use `respx` against `conftest.make_router()`, not
  `unittest.mock`.
- `zapi_lib/__init__.py`'s `__version__` is release-please-managed (checked
  against the git tag in `.github/workflows/release.yml`); don't hand-edit it.
- Commit messages: Conventional Commits (`feat:`, `fix:`, `docs:`, …),
  English.
