"""zapi-lib — Minimal Zabbix JSON-RPC API client (httpx-based)."""

from zapi_lib.client import (
    DEFAULT_CONFIG_SECTION,
    TAG_OP_EQUAL,
    TAG_OP_EXISTS,
    ZapiAuthError,
    ZapiClient,
    ZapiError,
    ZapiProvisioner,
    tag_filter,
)

__version__ = "0.2.0"  # x-release-please-version

__all__ = [
    "ZapiClient",
    "ZapiProvisioner",
    "ZapiError",
    "ZapiAuthError",
    "tag_filter",
    "TAG_OP_EQUAL",
    "TAG_OP_EXISTS",
    "DEFAULT_CONFIG_SECTION",
]
