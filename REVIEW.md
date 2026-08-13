# Review rules for this repository

Review rules on top of the reviewer's default focus. Three things:
which findings are blocking here, which classes to report that the
default focus would otherwise skip, and which are noise. The reasoning
behind the rules lives in `.github/copilot-instructions.md` (its
numbered focus items are cited below) and `CLAUDE.md`, which the
reviewer also receives.

## Always blocking

- **A public API change with no compatibility note in the pull request
  description (§1).** `zapi_lib/__init__.py`'s `__all__` is the
  contract, and downstream repositories pin this package across minor
  bumps. A changed method signature, return shape or raised-exception
  type belongs here. `ZapiClient._call()` counts as public surface
  despite the underscore: its *behavior* is the error contract below.
- **A raw exception escaping a public method (§2).** `_call()`
  normalizes every failure into `ZapiError` or `ZapiAuthError`, and
  consumers catch exactly those two — not `httpx.HTTPError`. An
  `httpx` exception, a `KeyError` on an unexpected response shape, or
  any other raw exception reaching a caller silently breaks every
  consumer's `except ZapiError`. Wrapping in `ZapiError` directly is
  fine, as `count_problems` does for a non-numeric `countOutput`.
- **Sending `tags` where the code deliberately omits it, or omitting it
  where the code deliberately sends it (§3).** Zabbix's `*.update`
  replaces the entire tag set whenever `tags` is present at all —
  even `[]`. The two existing strategies are **opposites**: `update_item`
  preserves tags by never sending the key, and `update_host` sends it
  only under an `if tags:` guard; adding an unconditional `tags: []` to
  either silently wipes a host's tags. `set_host_tag` is the reverse
  and must stay that way — it always sends a fully rebuilt, non-empty
  list, and re-sends only the writable `{tag, value}` keys, stripping
  Zabbix 6.4+'s read-only `automatic` field that `host.get` returns but
  `host.update` rejects. Passing fetched tags straight through, or
  converting `set_host_tag` to the omit-`tags` pattern, is the finding.
- **ORing the close bit (1) into `acknowledge_problem`'s action
  bitmask (§3).** It is built as `2 | (4 if message)` on purpose:
  acknowledge, add a message only when non-empty, and never close. That
  is what keeps it safe when triggers disallow manual close, and it is
  exposed as an MCP tool downstream. Setting the close bit turns a safe
  write into a destructive one.
- **Weakening `_login`'s credential-error guard (§4).** It retries with
  the alternate parameter name only when the first failure does *not*
  look like a credential error, so a genuinely bad password costs
  exactly one login attempt. Removing or reordering the guard, or
  broadening the retry to every `ZapiAuthError`, doubles failed logins
  against production Zabbix and the account-lockout and audit pressure
  with them.
- **A credential reaching a log line (§4).** The parsed `config.ini`
  `[zabbix]` values, the `password` field, or the auth payload built in
  `_call()`. Today's logging covers version strings and
  maintenance-window names only.
- **A request built by string concatenation into a URL or raw query
  (§4).** Every value goes to Zabbix as a JSON-RPC `params` dict entry.

## Report even though the default focus would not

- **A new write helper that creates without first checking for an
  existing object (§3).** `ensure_group` / `_group_list` and
  `set_maintenance` are check-then-create, which is what makes them
  safe to call repeatedly.
- **A new provisioner step that is not idempotent placed ahead of a
  write (§3)**, as advisory, unless the pull request calls it out.
  `create_host` / `update_host` already accept a non-transactional
  window — a succeeded group create followed by a failed host call
  leaves the group behind — and that is tolerated only because group
  creation is itself safe to retry, not because it is transactional. A
  step that would double up on retry breaks that reasoning.
- **A new public method that can raise, in a diff that also touches
  `tests/` without a dedicated error-path test (§5)**, as advisory.
  The suite asserts the specific `ZapiError` / `ZapiAuthError` message
  for each core failure mode. Judge it from the diff only — you receive
  changed files, so a pull request that leaves `tests/` alone may well
  be covered by tests you were not given.
- **A test that hand-mocks instead of using the shared fake (§5)**, as
  advisory. HTTP-level tests go through `respx` against
  `tests/conftest.py::make_router()`, which emulates Zabbix's
  `countOutput`, `limit` and severity filtering, not `unittest.mock`.

## Never report

- **MCP, stdio, FastMCP or tool-envelope advice.** This library has no
  MCP transport code, no stdout protocol channel and no
  tool-decorated functions. That review shape belongs to the MCP server
  that depends on this package, not here — a comment that would only
  make sense for an MCP server does not apply.
- A request to bump `zapi_lib/__init__.py`'s `__version__` by hand.
  It carries the `x-release-please-version` marker, is touched only by
  an automated release pull request, and is verified against the git
  tag in `release.yml` before publishing.
- Anything `ruff check .` or `ruff format --check .` already fails the
  build on. Both gate this repository, so restating one costs a round
  trip and no information. Note this is only about *restating* an
  enforced finding: unlike some sibling repositories, formatting here
  is genuinely enforced rather than opted out of. It never applies to a
  rule listed under **Always blocking** above, even if a lint rule
  happens to fire on the same line.
