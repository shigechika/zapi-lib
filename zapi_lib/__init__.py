"""zapi-lib — Minimal Zabbix JSON-RPC API client (httpx-based)."""

from zapi_lib.client import (
    TAG_OP_EQUAL,
    TAG_OP_EXISTS,
    ZapiAuthError,
    ZapiClient,
    ZapiError,
    tag_filter,
)

__version__ = "0.1.0"  # x-release-please-version

__all__ = [
    "ZapiClient",
    "ZapiError",
    "ZapiAuthError",
    "tag_filter",
    "TAG_OP_EQUAL",
    "TAG_OP_EXISTS",
]
