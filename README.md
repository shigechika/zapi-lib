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
- **Provisioning** (`ZapiProvisioner`): config-driven client that auto-creates trapper
  hosts/items stamped with a managed-by tag — `create_host`, `update_host`, `create_item`,
  `update_item`, `set_maintenance`, plus `ensure_group` / `get_host_ids` / `get_item_ids`.
- **Escape hatch**: `call(method, params)` invokes any JSON-RPC method directly.
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

### Provisioning (config-driven)

`ZapiProvisioner` extends `ZapiClient` for metric-collection scripts that register the
targets they push values to. It reads connection and provisioning defaults from a
`config.ini` `[zabbix]` section and auto-creates trapper hosts/items tagged with a
managed-by marker:

```ini
[zabbix]
url      = https://zabbix.example.com/api_jsonrpc.php
id       = api-user        ; or `user`
pw       = api-pass        ; or `password`
group    = DefaultGroup    ; default host group for created/updated hosts
location = tokyo           ; optional; added as a `location` tag
tag      = my-collector    ; optional; managed-by marker tag on hosts/items
```

```python
from zapi_lib import ZapiProvisioner

with ZapiProvisioner.from_config() as z:   # ./config.ini, then ~/.config.ini
    z.show_version()
    host_ids = z.get_host_ids("pool-a") or z.create_host("pool-a", location="tokyo")
    host_id = host_ids[0]
    if not z.get_item_ids(host_id, "usage"):
        z.create_item(host_id, "usage", value_type=0)
```

`update_host` replaces the host's groups and tags (use `set_host_tag` to upsert a single
tag instead). `set_maintenance(location, since, till, name, description)` opens an
idempotent maintenance window over hosts carrying a matching `location` tag.

## License

MIT
