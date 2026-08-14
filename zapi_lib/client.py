"""Zabbix JSON-RPC API client.

All requests target a single ``/api_jsonrpc.php`` endpoint; the called method is
carried in the request body. Authentication is version-adaptive but always
degrades to the proven ``user`` + ``auth``-field path used by older Zabbix
(<= 6.2), so the client works against current production while staying
forward-compatible with 6.4 / 7.0 (``username`` + ``Authorization: Bearer``).
"""

import configparser
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime

import httpx

DEFAULT_TIMEOUT = 30
DEFAULT_CONFIG_SECTION = "zabbix"

_logger = logging.getLogger(__name__)

# Zabbix tag-filter operators (host.get / problem.get / event.get)
TAG_OP_EQUAL = "1"
TAG_OP_EXISTS = "4"


class ZapiError(Exception):
    """Base error for Zabbix API failures."""


class ZapiAuthError(ZapiError):
    """Raised when authentication (user.login) fails."""


def tag_filter(tag: str, value: str | None = None) -> dict:
    """Build a Zabbix tag filter: Equal when a value is given, else Exists."""
    if value:
        return {"tag": tag, "value": value, "operator": TAG_OP_EQUAL}
    return {"tag": tag, "operator": TAG_OP_EXISTS}


def _epoch_or_none(value: object) -> int | None:
    """Coerce a Zabbix epoch field to int, or None when it can't be read.

    Zabbix returns epoch seconds as strings. Returning None instead of raising
    keeps a malformed or absent field from escaping a public method as a raw
    KeyError/ValueError; callers treat None as "not equal", which errs toward
    writing the value the caller asked for.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ZapiClient:
    """Minimal Zabbix API client using JSON-RPC over a single endpoint."""

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        *,
        logger: logging.Logger | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        # Every instance gets a usable self.logger, not just ZapiProvisioner
        # (whose __init__ used to be the only place this was set -- a plain
        # ZapiClient hit AttributeError as soon as any code path here logged,
        # e.g. the maintenance-window idempotent-return path below).
        self.logger = logger or _logger
        base = url.rstrip("/")
        if not base.endswith("/api_jsonrpc.php"):
            base += "/api_jsonrpc.php"
        self._url = base
        self._http = httpx.Client(timeout=timeout, headers={"Content-Type": "application/json"})
        self._token: str | None = None
        self._bearer = False  # use Authorization: Bearer header instead of `auth` field
        # api_version()/_login() touch the network and may raise; __enter__/
        # __exit__ do not run when the constructor itself raises, so close the
        # http client here to avoid leaking it on a failed connection/login.
        try:
            self.version = self.api_version()
            self._token = self._login(user, password)
        except BaseException:
            self._http.close()
            raise

    # ------------------------------------------------------------------
    # Low-level call
    # ------------------------------------------------------------------
    def _call(self, method: str, params: dict, *, auth: bool = True) -> object:
        data: dict = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        headers: dict = {}
        if auth and self._token:
            if self._bearer:
                headers["Authorization"] = f"Bearer {self._token}"
            else:
                data["auth"] = self._token
        try:
            resp = self._http.post(self._url, json=data, headers=headers or None)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as e:
            raise ZapiError(f"HTTP {e.response.status_code}: {method}") from e
        except httpx.HTTPError as e:
            raise ZapiError(f"Connection error calling {method}: {e}") from e
        if err := body.get("error"):
            if method == "user.login":
                raise ZapiAuthError(f"Authentication failed: {err}")
            raise ZapiError(f"{method} failed: {err}")
        return body["result"]

    # ------------------------------------------------------------------
    # Version detection & auth
    # ------------------------------------------------------------------
    def api_version(self) -> str:
        return self._call("apiinfo.version", {}, auth=False)

    @staticmethod
    def _version_tuple(version: str) -> tuple[int, int]:
        try:
            parts = version.split(".")
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return (0, 0)

    def _login(self, user: str, password: str) -> str:
        """Log in, choosing the param name by version and degrading to proven `user`.

        Zabbix 6.4 renamed the login parameter ``user`` -> ``username`` and added
        Bearer-header auth. We pick by detected version, then fall back to the
        other param name if the first attempt errors (so a misdetected version
        still authenticates).
        """
        modern = self._version_tuple(self.version) >= (6, 4)
        self._bearer = modern
        primary = "username" if modern else "user"
        fallback = "user" if modern else "username"
        try:
            return self._call("user.login", {primary: user, "password": password}, auth=False)
        except ZapiAuthError as e:
            # A genuine credential failure must not trigger a second login
            # attempt (avoid doubling lockout / audit pressure).
            msg = str(e).lower()
            if "incorrect" in msg or "password" in msg or "no permissions" in msg:
                raise
            # Otherwise the param name was likely wrong for this version: retry
            # with the other name and degrade to the proven `auth` field.
            self._bearer = False
            return self._call("user.login", {fallback: user, "password": password}, auth=False)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ZapiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Host groups
    # ------------------------------------------------------------------
    def _get_group_ids(self, group: str) -> list[str]:
        result = self._call("hostgroup.get", {"output": "groupid", "filter": {"name": [group]}})
        return [r["groupid"] for r in result]

    # ------------------------------------------------------------------
    # Hosts
    # ------------------------------------------------------------------
    def get_hosts(
        self,
        *,
        tags: list[dict] | None = None,
        group: str | None = None,
        host: str | None = None,
    ) -> list[dict]:
        """Return hosts, optionally filtered by tags, group name, or exact host."""
        params: dict = {
            "output": ["hostid", "host", "name", "status"],
            "selectTags": "extend",
            "selectInterfaces": ["ip"],
        }
        if tags:
            params["tags"] = tags
        if group:
            params["groupids"] = self._get_group_ids(group)
        if host:
            params["filter"] = {"host": host}
        return self._call("host.get", params)

    # ------------------------------------------------------------------
    # Host tags (write)
    # ------------------------------------------------------------------
    def set_host_tag(self, host: str, tag: str, value: str) -> dict:
        """Upsert one host tag by name, preserving the host's other tags.

        Zabbix ``host.update`` replaces the entire tag set, so the host's
        current tags are fetched first and merged: a tag with the same name is
        replaced, every other tag is kept. Raises ``ZapiError`` when the host
        is not found. Returns the ``host.update`` result.
        """
        hosts = self.get_hosts(host=host)
        if not hosts:
            raise ZapiError(f"host not found: {host}")
        target = hosts[0]
        # host.update accepts only {tag, value} per tag; host.get with
        # selectTags=extend also returns a read-only "automatic" field on
        # Zabbix 6.4+, which host.update rejects. Rebuild preserved tags with
        # the writable keys only, dropping the same-named tag (replaced below).
        tags = [{"tag": t["tag"], "value": t.get("value", "")} for t in target.get("tags", []) if t.get("tag") != tag]
        tags.append({"tag": tag, "value": value})
        return self._call("host.update", {"hostid": target["hostid"], "tags": tags})

    # ------------------------------------------------------------------
    # Items (current values)
    # ------------------------------------------------------------------
    def get_items(
        self,
        host_ids: list[str],
        *,
        key: str | None = None,
        key_search: str | None = None,
        name_search: str | None = None,
    ) -> list[dict]:
        """Return items with last value for given hosts.

        ``key`` filters by exact item key (key_); ``key_search`` does a substring
        match on the key (e.g. ".usage" to catch ``pool.node0.usage``);
        ``name_search`` does a substring match on the item name.
        """
        params: dict = {
            "output": ["itemid", "hostid", "name", "key_", "lastvalue", "units", "lastclock"],
            "hostids": host_ids,
            "selectTags": "extend",
        }
        if key:
            params["filter"] = {"key_": key}
        search = {}
        if key_search:
            search["key_"] = key_search
        if name_search:
            search["name"] = name_search
        if search:
            params["search"] = search
        return self._call("item.get", params)

    # ------------------------------------------------------------------
    # Problems
    # ------------------------------------------------------------------
    def get_problems(
        self,
        *,
        severities: list[int] | None = None,
        tags: list[dict] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return active problems, optionally filtered by severity and tags.

        Output includes ``eventid`` so callers can acknowledge problems.
        """
        params: dict = {
            "output": "extend",
            "selectAcknowledges": "count",
            "selectTags": "extend",
            # problem.get only permits "eventid" as a sortfield; callers that
            # need severity ordering re-bucket in Python.
            "sortfield": "eventid",
            "sortorder": "DESC",
            "limit": limit,
            "suppressed": False,
        }
        if severities:
            params["severities"] = severities
        if tags:
            params["tags"] = tags
        return self._call("problem.get", params)

    def count_problems(
        self,
        *,
        severities: list[int] | None = None,
        tags: list[dict] | None = None,
    ) -> int:
        """Return the total count of active problems matching the filters.

        Uses Zabbix ``countOutput`` so callers can report an accurate total even
        when ``get_problems`` is capped by ``limit`` (avoids silent truncation).
        """
        params: dict = {"countOutput": True, "suppressed": False}
        if severities:
            params["severities"] = severities
        if tags:
            params["tags"] = tags
        result = self._call("problem.get", params)
        try:
            return int(result)  # countOutput returns the count as a numeric string
        except (TypeError, ValueError) as e:
            # A genuine API failure already raised in _call; an unexpected shape
            # here is a contract violation worth surfacing, not masking as 0.
            raise ZapiError(f"problem.get countOutput returned non-numeric: {result!r}") from e

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def get_events(
        self,
        *,
        time_from: int | None = None,
        severities: list[int] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return recent problem events (source=trigger, value=problem)."""
        params: dict = {
            "output": "extend",
            "selectTags": "extend",
            "selectHosts": ["host", "name"],
            "source": 0,
            "object": 0,
            "value": 1,
            "sortfield": ["clock", "eventid"],
            "sortorder": "DESC",
            "limit": limit,
        }
        if time_from:
            params["time_from"] = time_from
        if severities:
            params["severities"] = severities
        return self._call("event.get", params)

    # ------------------------------------------------------------------
    # Maintenance (read)
    # ------------------------------------------------------------------
    def get_maintenances(self) -> list[dict]:
        """Return all maintenance windows with hosts, host groups, time periods, and tags.

        Returns raw rows, including expired windows: classifying a window as
        active/upcoming/expired depends on "now", which this library doesn't
        track, so that judgment is left to the caller -- matching the
        get_hosts/get_problems/get_events convention of returning unfiltered
        API rows rather than pre-interpreting them.

        The host-group selector is version-gated: Zabbix 6.4 renamed
        ``selectGroups`` to ``selectHostGroups`` (the old name is deprecated
        from 6.4 on, though still accepted as of 7.0), so the result key for
        group membership is ``"groups"`` on Zabbix < 6.4 and ``"hostgroups"``
        on >= 6.4 -- callers must check both.

        Rows are returned in whatever order ``maintenance.get`` gives back
        (no ``sortfield`` is requested: ``active_since``/``active_till`` are
        only valid sort fields on Zabbix >= 7.0, and this library also
        supports older versions -- sort client-side if order matters).

        ``active_since``/``active_till`` and the ``timeperiods`` entries carry
        epoch seconds as Zabbix-flavored strings, not ints.
        """
        groups_key = "selectHostGroups" if self._version_tuple(self.version) >= (6, 4) else "selectGroups"
        params: dict = {
            "output": "extend",
            "selectHosts": ["hostid", "host", "name"],
            groups_key: ["groupid", "name"],
            "selectTimeperiods": "extend",
            "selectTags": "extend",
        }
        return self._call("maintenance.get", params)

    # ------------------------------------------------------------------
    # Acknowledge
    # ------------------------------------------------------------------
    def acknowledge_problem(self, event_ids: list[str], message: str = "") -> dict:
        """Acknowledge problems, optionally adding a message.

        Action is a bitmask: acknowledge (2), plus add-message (4) only when a
        non-empty message is given (Zabbix rejects an empty message when bit 4
        is set). Does NOT close problems (close is bit 1), so the tool is safe
        even when triggers disallow manual close.
        """
        action = 2 | (4 if message else 0)
        params: dict = {"eventids": event_ids, "action": action}
        if message:
            params["message"] = message
        return self._call("event.acknowledge", params)

    # ------------------------------------------------------------------
    # Generic call (escape hatch for methods without a dedicated helper)
    # ------------------------------------------------------------------
    def call(self, method: str, params: dict, *, auth: bool = True) -> object:
        """Invoke any Zabbix JSON-RPC method directly.

        A thin public wrapper over the internal dispatcher for callers that need
        a method this client does not wrap. ``auth=False`` omits the session
        token (only ``apiinfo.version`` / ``user.login`` need that).
        """
        return self._call(method, params, auth=auth)

    # ------------------------------------------------------------------
    # Host groups (write)
    # ------------------------------------------------------------------
    def get_group_id(self, name: str) -> str | None:
        """Return the id of a host group by exact name, or None when absent."""
        result = self._call("hostgroup.get", {"output": "groupid", "filter": {"name": [name]}})
        return result[0]["groupid"] if result else None

    def create_group(self, name: str) -> str:
        """Create a host group and return its id."""
        result = self._call("hostgroup.create", {"name": name})
        return result["groupids"][0]

    def ensure_group(self, name: str) -> str:
        """Return a host group's id, creating the group when it does not exist."""
        return self.get_group_id(name) or self.create_group(name)

    # ------------------------------------------------------------------
    # Host / item id lookups
    # ------------------------------------------------------------------
    def get_host_ids(self, host: str) -> list[str]:
        """Return sorted host ids for an exact host (technical name)."""
        result = self._call("host.get", {"filter": {"host": host}, "output": "hostid"})
        return sorted(r["hostid"] for r in result)

    def get_host_ids_by_tag(self, tag: str, value: str | None = None) -> list[str]:
        """Return sorted host ids matching a tag (Equal when a value is given)."""
        result = self._call("host.get", {"output": "hostid", "tags": [tag_filter(tag, value)]})
        return sorted(r["hostid"] for r in result)

    def get_item_ids(self, host_id: str, name: str) -> list[str]:
        """Return sorted item ids on a host matching an exact item name."""
        result = self._call("item.get", {"hostids": host_id, "filter": {"name": name}, "output": "itemid"})
        return sorted(r["itemid"] for r in result)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_maintenance_time(value: str) -> datetime:
        """Parse a maintenance since/till string, wrapping the library's error
        contract (ZapiError/ZapiAuthError are the only exceptions this library
        raises -- a raw ValueError escaping here would violate that)."""
        try:
            return datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
        except ValueError as e:
            raise ZapiError(f"invalid maintenance window timestamp {value!r} (expected %Y/%m/%d %H:%M:%S): {e}") from e

    def _create_maintenance_window(
        self,
        resolve_hostids: Callable[[], list[str]],
        since: str,
        till: str,
        name: str,
        description: str,
        *,
        tags: list[dict] | None = None,
        overwrite: bool = False,
    ) -> list[str]:
        """Shared idempotent maintenance-window creation.

        ``since``/``till`` are ``"%Y/%m/%d %H:%M:%S"`` **naive** strings --
        parsed and converted to epoch seconds via the calling process's local
        timezone (``time.mktime``), not a fixed zone. Callers running outside
        JST (or wanting a specific zone regardless of the host's) must convert
        before calling.
        The window name is ``name`` + the start time (``%y%m%d%H%M``). When a
        window with that name already exists, ``overwrite`` decides what
        happens:

        - ``overwrite=False`` (default): leave it untouched and return its
          ids. Callers keep the historical idempotent behaviour.
        - ``overwrite=True``: treat the call as a *correction* of the same
          planned outage and update ``active_since``/``active_till``/
          ``timeperiods``/``description`` in place (last write wins).

        Only turn ``overwrite`` on where ``name`` + ``since`` provably
        identifies one real-world event, because a collision then means "the
        same outage, re-announced". That holds for a machine-generated name
        derived from a site and its start time; it does NOT hold for a
        free-text name chosen per call, where two unrelated maintenances can
        share a name and overwriting would silently reschedule someone
        else's window (see the target-not-in-key caveat in
        ``set_maintenance``).

        ``hostids`` is deliberately NOT sent on update: Zabbix leaves
        properties that are omitted from ``maintenance.update`` unchanged, so
        the existing host assignment survives and the lazy host resolution
        below still holds (a caller that can ``maintenance.*`` but not
        ``host.get`` can still correct the schedule). It also means an
        overwrite never re-targets a window -- the times move, the hosts do
        not.

        In either mode the ids are returned WITHOUT ever calling
        ``resolve_hostids`` -- host
        resolution is deliberately lazy (a callable, not an already-resolved
        list) so a repeated call against an existing window still
        short-circuits before touching host.get/host-tag lookups, matching
        set_maintenance's pre-refactor behavior (a caller whose API role can
        maintenance.get but not host.get must still be able to no-op an
        idempotent repeat call). Note this check-then-act is not atomic with
        the ``maintenance.create`` below; truly concurrent callers with the
        same name+since could each observe no existing window and create a
        duplicate -- accepted for now given this library's actual callers are
        single-shot scripts or a human-gated approval flow, not a source of
        real concurrent writers.
        ``tags`` is only meaningful together with a ``location``-tag-based
        selection (see ``set_maintenance``) -- explicit host-name selection
        (``set_maintenance_for_hosts``) passes none. Only the host-based mode
        gets an ``h`` suffix appended to the window name; the tag-based mode's
        name format is untouched (bare ``name`` + timestamp, exactly the
        pre-refactor format) so a window created by an older release of
        ``set_maintenance`` is still recognized as the same window across an
        upgrade. ``strftime("%y%m%d%H%M")`` is all digits, so a bare tag-mode
        name can never end in ``h`` and the two modes can never collide with
        each other under the same ``name``+``since``.
        """
        since_dt = self._parse_maintenance_time(since)
        till_dt = self._parse_maintenance_time(till)
        mode_suffix = "" if tags is not None else "h"
        maint_name = name + since_dt.strftime("%y%m%d%H%M") + mode_suffix

        since_epoch = int(time.mktime(since_dt.timetuple()))
        till_epoch = int(time.mktime(till_dt.timetuple()))
        timeperiods = [{"start_date": since_epoch, "period": int((till_dt - since_dt).total_seconds())}]

        existing = self._call(
            "maintenance.get",
            {
                "filter": {"name": maint_name},
                "output": ["maintenanceid", "active_since", "active_till", "description"],
            },
        )
        if existing:
            ids = [e["maintenanceid"] for e in existing]
            if not overwrite:
                self.logger.info("maintenance already exists, skipping: %s", maint_name)
                return ids
            for row in existing:
                # Compare every field the update would write -- description
                # included. Skipping a description-only correction would
                # reproduce, inside the overwrite path, the exact silent-drop
                # this option exists to fix.
                #
                # Compare against what Zabbix actually stores (epoch seconds as
                # strings) so a pure re-run issues no write at all -- re-running
                # the caller must stay a no-op even with overwrite on. An
                # unreadable stored value (missing key, non-numeric) must not
                # escape as a raw KeyError/ValueError from a public method, and
                # "cannot confirm it already matches" is treated as "differs":
                # applying the correction is the safe direction, since the whole
                # point of the call is to make the window say what the caller
                # asked for.
                if (
                    _epoch_or_none(row.get("active_since")) == since_epoch
                    and _epoch_or_none(row.get("active_till")) == till_epoch
                    and row.get("description") == description
                ):
                    self.logger.info("maintenance unchanged: %s", maint_name)
                    continue
                self.logger.info(
                    "maintenance updated: %s  active_since %s->%s  active_till %s->%s",
                    maint_name,
                    row.get("active_since"),
                    since_epoch,
                    row.get("active_till"),
                    till_epoch,
                )
                self._call(
                    "maintenance.update",
                    {
                        "maintenanceid": row["maintenanceid"],
                        "active_since": since_epoch,
                        "active_till": till_epoch,
                        "timeperiods": timeperiods,
                        "description": description,
                    },
                )
            return ids

        payload = {
            "active_since": since_epoch,
            "active_till": till_epoch,
            "name": maint_name,
            "description": description,
            "hostids": resolve_hostids(),
            "timeperiods": timeperiods,
        }
        if tags is not None:
            payload["tags_evaltype"] = 0
            payload["tags"] = tags
        result = self._call("maintenance.create", payload)
        return result["maintenanceids"]

    def set_maintenance(
        self,
        location: str,
        since: str,
        till: str,
        name: str,
        description: str,
        *,
        overwrite: bool = False,
    ) -> list[str]:
        """Create a maintenance window covering hosts with a matching ``location`` tag.

        See ``_create_maintenance_window`` for the ``since``/``till``/idempotency
        contract. The positional part of the signature is fixed (``location``
        first) for backward compatibility with existing callers (e.g.
        ``nuwan-exec.py``); use ``set_maintenance_for_hosts`` for
        explicit-hostname selection.

        ``overwrite`` is keyword-only and defaults to False, so existing
        callers keep the idempotent no-op behaviour. Note the idempotency key
        is ``name`` + ``since`` and does NOT include the target: two calls
        with the same name/since but different ``location`` collide. With
        ``overwrite=False`` the second call silently protects nothing; with
        ``overwrite=True`` it additionally reschedules the first window while
        still not protecting the new target. Only pass ``overwrite=True``
        when ``name`` is generated from the event itself, so a collision can
        only mean "same outage, re-announced".
        """
        return self._create_maintenance_window(
            lambda: self.get_host_ids_by_tag("location", location),
            since,
            till,
            name,
            description,
            tags=[{"tag": "location", "operator": "0", "value": location}],
            overwrite=overwrite,
        )

    def _resolve_hostids_by_name(self, hosts: list[str]) -> list[str]:
        """Resolve exact host names to ids in one batched ``host.get`` call
        (Zabbix's ``host`` filter accepts an array), raising ZapiError (not a
        silent partial match) if any name doesn't resolve or ``hosts`` is
        empty -- a maintenance window that silently drops an unrecognized
        host, or protects nothing, leaves the caller's real target
        unprotected without telling them.

        The empty-``hosts`` check is unreachable through the current sole
        caller (``set_maintenance_for_hosts`` checks the same thing eagerly,
        before this is ever invoked) -- kept anyway as defense-in-depth for
        this private method, since a future second caller of it shouldn't
        have to remember to re-derive the same guard.

        Assumes Zabbix's own host-name-uniqueness constraint (``host.host``
        is unique server-wide), so at most one row is expected per name.
        """
        if not hosts:
            raise ZapiError("set_maintenance_for_hosts requires at least one host name")
        rows = self._call("host.get", {"filter": {"host": hosts}, "output": ["hostid", "host"]})
        found = {row["host"]: row["hostid"] for row in rows}
        missing = [host for host in hosts if host not in found]
        if missing:
            raise ZapiError(f"host(s) not found: {', '.join(missing)}")
        return sorted(set(found.values()))

    def set_maintenance_for_hosts(
        self,
        hosts: list[str],
        since: str,
        till: str,
        name: str,
        description: str,
        *,
        overwrite: bool = False,
    ) -> list[str]:
        """Create a maintenance window covering explicit hosts by exact technical name.

        Same idempotent/window-naming contract as ``set_maintenance``, but
        selects hosts by exact ``host.get`` technical name instead of a
        ``location`` tag (useful when the affected hosts don't share one, or
        when the operator wants precise host-level control). Raises
        ``ZapiError`` if any of ``hosts`` doesn't resolve to a host id.

        The ``hosts`` emptiness check runs eagerly, here, before the
        idempotency lookup -- unlike host *resolution* (a network call,
        deliberately lazy so a repeat call can no-op without host.get
        access), checking whether the list is empty is a local, free check.
        Deferring it into ``_resolve_hostids_by_name`` would let it be
        silently skipped whenever a same-named window already exists.
        """
        if not hosts:
            raise ZapiError("set_maintenance_for_hosts requires at least one host name")
        return self._create_maintenance_window(
            lambda: self._resolve_hostids_by_name(hosts),
            since,
            till,
            name,
            description,
            overwrite=overwrite,
        )


def _default_config_path() -> str:
    """Resolve the default config path: ``./config.ini`` then ``~/.config.ini``."""
    cwd = os.path.join(os.getcwd(), "config.ini")
    if os.path.isfile(cwd):
        return cwd
    return os.path.join(os.path.expanduser("~"), ".config.ini")


class ZapiProvisioner(ZapiClient):
    """Config-driven Zabbix provisioning client.

    Extends :class:`ZapiClient` with the pattern metric-collection scripts use:
    read connection and provisioning defaults from a ``config.ini`` ``[zabbix]``
    section, then auto-create Zabbix *trapper* hosts and items stamped with a
    managed-by marker tag so the collector can push values to them.

    The ``[zabbix]`` section is read as::

        [zabbix]
        url      = https://zabbix.example.com/api_jsonrpc.php
        id       = api-user        ; or `user`
        pw       = api-pass        ; or `password`
        group    = DefaultGroup    ; default host group for created/updated hosts
        location = tokyo           ; optional; added as a `location` tag
        tag      = my-collector    ; optional; managed-by marker tag on hosts/items

    ``url`` and the credentials are required; ``group``/``location``/``tag`` are
    optional. The default ``group``, when set, is looked up at construction (no
    write) and created on demand the first time a host is created or updated, so
    a provisioner used only for reads or raw :meth:`call` has no write side effect.
    """

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        *,
        group: str | None = None,
        location: str | None = None,
        managed_tag: str | None = None,
        logger: logging.Logger | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        # Set provisioning state before super().__init__ touches the network, so
        # the instance is fully formed even if login raises during construction.
        # logger itself is now ZapiClient's job (passed through, not set here
        # directly) -- a single source of truth instead of two competing
        # `logger or _logger` assignments across the class hierarchy.
        self.default_group = group
        self.default_location = location
        self.managed_tag = managed_tag
        super().__init__(url, user, password, logger=logger, timeout=timeout)
        # Resolve the default group id (GET only — no write side effect at
        # construction). It is created on demand when a host is first written
        # (see _group_list), so a provisioner used only for reads / raw calls
        # never creates a group just by being constructed.
        self.group_id: str | None = self.get_group_id(group) if group else None

    @classmethod
    def from_config(
        cls,
        path: str | None = None,
        *,
        section: str = DEFAULT_CONFIG_SECTION,
        logger: logging.Logger | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> "ZapiProvisioner":
        """Build a provisioner from a ``config.ini`` ``[zabbix]`` section.

        ``path`` defaults to ``./config.ini`` then ``~/.config.ini``. Credentials
        accept either ``id``/``pw`` or the ``user``/``password`` aliases.
        """
        cfg = configparser.ConfigParser(allow_no_value=True)
        cfg.read(path or _default_config_path())
        # Accept id/pw or the user/password aliases. Treat a blank value (the
        # deploy-time placeholder: "認証情報は空欄で格納し、デプロイ時に記入") as
        # missing and raise a clear auth error, rather than a configparser
        # NoOptionError about an alias key the operator never wrote.
        user = cfg.get(section, "id", fallback="") or cfg.get(section, "user", fallback="")
        password = cfg.get(section, "pw", fallback="") or cfg.get(section, "password", fallback="")
        if not user or not password:
            raise ZapiAuthError(f"Zabbix credentials not set in [{section}]: fill in id/pw (or user/password)")
        return cls(
            cfg.get(section, "url"),
            user,
            password,
            group=cfg.get(section, "group", fallback=None),
            location=cfg.get(section, "location", fallback=None),
            managed_tag=cfg.get(section, "tag", fallback=None),
            logger=logger,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Convention builders
    # ------------------------------------------------------------------
    def _tags(
        self,
        *,
        location: str | None = None,
        tag_name: str | None = None,
        tag_value: str | None = None,
        device_type: str | None = None,
    ) -> list[dict]:
        """Build the host/item tag set: managed-by marker + optional location etc."""
        tags: list[dict] = []
        if self.managed_tag:
            tags.append({"tag": self.managed_tag})
        loc = location if location is not None else self.default_location
        if loc is not None:
            tags.append({"tag": "location", "value": loc})
        if tag_name is not None:
            tags.append({"tag": tag_name, "value": tag_value or ""})
        if device_type is not None:
            tags.append({"tag": "device_type", "value": device_type})
        return tags

    def _group_list(self, group: str | None = None) -> list[dict]:
        """Build the host group list: the default group plus an optional extra.

        The default group is created on demand here (not at construction), so a
        provisioner used only for reads / raw calls has no write side effect.
        """
        groups: list[dict] = []
        if self.default_group is not None:
            if self.group_id is None:
                self.group_id = self.ensure_group(self.default_group)
            groups.append({"groupid": self.group_id})
        if group is not None:
            groups.append({"groupid": self.ensure_group(group)})
        return groups

    # ------------------------------------------------------------------
    # Hosts (write)
    # ------------------------------------------------------------------
    def create_host(
        self,
        host: str,
        *,
        group: str | None = None,
        location: str | None = None,
        tag_name: str | None = None,
        tag_value: str | None = None,
        device_type: str | None = None,
    ) -> list[str]:
        """Create a host in the default group (+ optional group), tagged managed-by."""
        groups = self._group_list(group)
        if not groups:
            raise ZapiError("no host group configured: set a [zabbix] group or pass group=")
        params: dict = {"host": host, "groups": groups}
        tags = self._tags(location=location, tag_name=tag_name, tag_value=tag_value, device_type=device_type)
        if tags:
            params["tags"] = tags
        result = self._call("host.create", params)
        return sorted(result["hostids"])

    def update_host(
        self,
        host_id: str,
        *,
        group: str | None = None,
        location: str | None = None,
        tag_name: str | None = None,
        tag_value: str | None = None,
        device_type: str | None = None,
    ) -> list[str]:
        """Update a host's groups and managed-by tags.

        Like Zabbix ``host.update``, the supplied groups and tags *replace* the
        host's existing sets (this is the behaviour the collectors rely on to keep
        a host's metadata in sync). Use :meth:`ZapiClient.set_host_tag` instead to
        upsert a single tag while preserving the others.
        """
        groups = self._group_list(group)
        if not groups:
            raise ZapiError("no host group configured: set a [zabbix] group or pass group=")
        params: dict = {"hostid": host_id, "groups": groups}
        tags = self._tags(location=location, tag_name=tag_name, tag_value=tag_value, device_type=device_type)
        if tags:
            params["tags"] = tags
        result = self._call("host.update", params)
        return sorted(result["hostids"])

    # ------------------------------------------------------------------
    # Items (write)
    # ------------------------------------------------------------------
    def create_item(self, host_id: str, name: str, *, value_type: int = 0) -> list[str]:
        """Create a Zabbix trapper item (``key_`` == ``name``), tagged managed-by."""
        params: dict = {
            "hostid": host_id,
            "name": name,
            "key_": name,
            "type": 2,  # Zabbix trapper
            "value_type": value_type,
        }
        if self.managed_tag:
            params["tags"] = [{"tag": self.managed_tag}]
        result = self._call("item.create", params)
        return sorted(result["itemids"])

    def update_item(self, item_id: str, *, value_type: int = 0) -> list[str]:
        """Update a trapper item's value type. Tags are left untouched.

        ``item.update`` replaces the whole tag set when ``tags`` is supplied, so
        sending no tags preserves the managed-by tag (stamped at create time)
        as well as any operator-added item tags.
        """
        result = self._call("item.update", {"itemid": item_id, "value_type": value_type})
        return sorted(result["itemids"])

    def show_version(self) -> str:
        """Log and return the Zabbix API version detected at construction."""
        self.logger.info("Zabbix API version: %s", self.version)
        return self.version
