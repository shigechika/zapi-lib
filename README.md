# zapi-lib

Minimal [Zabbix](https://www.zabbix.com/) JSON-RPC API client for Python — a thin,
`httpx`-only wrapper around the single `/api_jsonrpc.php` endpoint.

Spun out of [zapi-mcp](https://github.com/shigechika/zapi-mcp) so that tools which only
need the API client — e.g. [speedtest-z](https://github.com/shigechika/speedtest-z) —
can depend on it without pulling in the MCP server stack (`mcp`, `starlette`, `uvicorn`, …).

## Features

- **Version-adaptive auth**: `user` + `auth` field (Zabbix ≤ 6.2) and
  `username` + `Authorization: Bearer` (6.4 / 7.0), degrading to the proven path automatically.
- **Read helpers**: `get_hosts`, `get_items`, `get_problems`, `count_problems`, `get_events`.
- **Write helpers**: `set_host_tag` (idempotent host-tag upsert that preserves other tags),
  `acknowledge_problem`.
- A single runtime dependency: `httpx`.

## Install

```bash
pip install zapi-lib
```

## Usage

```python
from zapi_lib import ZapiClient, tag_filter

with ZapiClient("https://zabbix.example.com", "api-user", "api-pass") as z:
    hosts = z.get_hosts(tags=[tag_filter("speedtest-z")])
    z.set_host_tag("eduroam", "speedtest-z", "0.10.0")
```

The URL may be given with or without the `/api_jsonrpc.php` suffix.

## License

MIT
