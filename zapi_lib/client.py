"""Zabbix JSON-RPC API client.

All requests target a single ``/api_jsonrpc.php`` endpoint; the called method is
carried in the request body. Authentication is version-adaptive but always
degrades to the proven ``user`` + ``auth``-field path used by older Zabbix
(<= 6.2), so the client works against current production while staying
forward-compatible with 6.4 / 7.0 (``username`` + ``Authorization: Bearer``).
"""

import httpx

DEFAULT_TIMEOUT = 30

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
