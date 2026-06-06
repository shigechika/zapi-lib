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


class ZapiClient:
    """Minimal Zabbix API client using JSON-RPC over a single endpoint."""

    def __init__(self, url: str, user: str, password: str, *, timeout: int = DEFAULT_TIMEOUT):
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
    optional. ``group``, when set, is resolved (and created if missing) once at
    construction and applied to every host created or updated through this client.
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
        self.logger = logger or _logger
        self.default_group = group
        self.default_location = location
        self.managed_tag = managed_tag
        super().__init__(url, user, password, timeout=timeout)
        # Resolve (creating if needed) the default group once, after login.
        self.group_id: str | None = self.ensure_group(group) if group else None

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
        user = cfg.get(section, "id", fallback=None) or cfg.get(section, "user")
        password = cfg.get(section, "pw", fallback=None) or cfg.get(section, "password")
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
        """Build the host group list: the default group plus an optional extra."""
        groups: list[dict] = []
        if self.group_id is not None:
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
        params: dict = {"host": host, "groups": self._group_list(group)}
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
        params: dict = {"hostid": host_id, "groups": self._group_list(group)}
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
        """Update a trapper item's value type, re-stamping the managed-by tag."""
        params: dict = {"itemid": item_id, "value_type": value_type}
        if self.managed_tag:
            params["tags"] = [{"tag": self.managed_tag}]
        result = self._call("item.update", params)
        return sorted(result["itemids"])

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def set_maintenance(self, location: str, since: str, till: str, name: str, description: str) -> list[str]:
        """Create a maintenance window covering hosts with a matching ``location`` tag.

        ``since``/``till`` are ``"%Y/%m/%d %H:%M:%S"`` strings. The window name is
        ``name`` + the start time (``%y%m%d%H%M``); an existing window with that
        name is left untouched (idempotent) and its ids are returned.
        """
        since_dt = datetime.strptime(since, "%Y/%m/%d %H:%M:%S")
        till_dt = datetime.strptime(till, "%Y/%m/%d %H:%M:%S")
        maint_name = name + since_dt.strftime("%y%m%d%H%M")

        existing = self._call("maintenance.get", {"filter": {"name": maint_name}, "output": ["maintenanceid"]})
        if existing:
            self.logger.info("maintenance already exists, skipping: %s", maint_name)
            return [e["maintenanceid"] for e in existing]

        result = self._call(
            "maintenance.create",
            {
                "active_since": int(time.mktime(since_dt.timetuple())),
                "active_till": int(time.mktime(till_dt.timetuple())),
                "name": maint_name,
                "description": description,
                "tags_evaltype": 0,
                "hostids": self.get_host_ids_by_tag("location", location),
                "timeperiods": [
                    {
                        "start_date": int(time.mktime(since_dt.timetuple())),
                        "period": int((till_dt - since_dt).total_seconds()),
                    }
                ],
                "tags": [{"tag": "location", "operator": "0", "value": location}],
            },
        )
        return result["maintenanceids"]

    def show_version(self) -> str:
        """Log and return the Zabbix API version detected at construction."""
        self.logger.info("Zabbix API version: %s", self.version)
        return self.version
