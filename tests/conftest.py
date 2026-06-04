"""Shared fixtures for zapi-lib tests.

Zabbix exposes a single JSON-RPC endpoint, so the mock dispatches by the
``method`` field in the request body rather than by URL.
"""

import json

import httpx
import respx

ENDPOINT = "https://zabbix.example.com/api_jsonrpc.php"

# Default canned results per method; tests override individual entries.
DEFAULT_RESULTS: dict[str, object] = {
    "apiinfo.version": "6.0.0",
    "user.login": "sess-token-abc",
    "hostgroup.get": [{"groupid": "10"}],
    "host.get": [],
    "item.get": [],
    "problem.get": [],
    "event.get": [],
    "event.acknowledge": {"eventids": ["1"]},
}


def _severity_in(row: dict, severities: list[int]) -> bool:
    """True when a problem row's severity is in the requested list (mirrors Zabbix)."""
    try:
        return int(row.get("severity", -1)) in severities
    except (TypeError, ValueError):
        return False


def _eventid_key(row: dict) -> int:
    """eventid as int for DESC ordering (0 when missing/unparseable)."""
    try:
        return int(row.get("eventid") or 0)
    except (TypeError, ValueError):
        return 0


def make_router(results: dict | None = None, *, version: str = "6.0.0"):
    """Return a respx router that answers JSON-RPC calls by method.

    ``results`` overrides DEFAULT_RESULTS per method. ``version`` sets the
    apiinfo.version reply (controls the client's auth path).
    """
    table = dict(DEFAULT_RESULTS)
    table["apiinfo.version"] = version
    if results:
        table.update(results)

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append({"payload": payload, "headers": dict(request.headers)})
        method = payload.get("method")
        if method not in table:
            return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"message": f"unknown {method}"}, "id": 1})
        result = table[method]
        params = payload.get("params") or {}
        # Emulate Zabbix server-side severity filtering so countOutput and limit
        # operate on the same filtered set the real API would (only problem.get /
        # event.get carry `severities`, so other methods are untouched).
        sevs = params.get("severities")
        if isinstance(result, list) and sevs:
            result = [r for r in result if _severity_in(r, sevs)]
        # Emulate Zabbix countOutput: return the match count as a numeric string.
        if isinstance(result, list) and params.get("countOutput"):
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": str(len(result)), "id": 1})
        # Emulate `limit`: real problem.get/event.get sort by eventid DESC before
        # truncating, so order the rows the same way before slicing.
        if isinstance(result, list) and isinstance(params.get("limit"), int):
            if result and isinstance(result[0], dict) and "eventid" in result[0]:
                result = sorted(result, key=_eventid_key, reverse=True)
            result = result[: params["limit"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": 1})

    router = respx.mock(assert_all_called=False)
    router.post(ENDPOINT).mock(side_effect=handler)
    router.captured = captured  # type: ignore[attr-defined]
    return router


# ---- sample data ----------------------------------------------------------

SAMPLE_PROBLEM = {
    "eventid": "5001",
    "name": "High CPU on core-rt1",
    "severity": "4",
    "clock": "1700000000",
    "acknowledged": "0",
    "acknowledges": "0",
    "tags": [{"tag": "role", "value": "main"}],
}

SAMPLE_HOST = {
    "hostid": "100",
    "host": "pool-a",
    "name": "Pool A",
    "status": "0",
    "tags": [{"tag": "dhcp-pool-usage", "value": "1.0"}],
    "interfaces": [{"ip": "192.0.2.1"}],
}

SAMPLE_ITEM = {
    "itemid": "200",
    "hostid": "100",
    "name": "usage",
    "key_": "usage",
    "lastvalue": "85.5",
    "units": "%",
    "lastclock": "1700000000",
    "tags": [],
}
