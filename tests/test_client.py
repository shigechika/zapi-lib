"""Tests for ZapiClient — auth path selection, tag filters, API methods."""

import json

import httpx
import pytest
import respx

from tests.conftest import ENDPOINT, SAMPLE_HOST, SAMPLE_ITEM, SAMPLE_PROBLEM, make_router
from zapi_lib.client import ZapiAuthError, ZapiClient, ZapiError, tag_filter

# ---- URL normalization ----------------------------------------------------


def test_url_gets_endpoint_suffix():
    with make_router():
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        assert c._url == "https://zabbix.example.com/api_jsonrpc.php"


def test_url_keeps_existing_suffix():
    with make_router():
        c = ZapiClient("https://zabbix.example.com/api_jsonrpc.php", "u", "p")
        assert c._url == "https://zabbix.example.com/api_jsonrpc.php"


# ---- version-adaptive auth ------------------------------------------------


def test_legacy_version_uses_user_param_and_auth_field():
    r = make_router(version="6.0.18")
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        assert c._bearer is False
        login = next(x["payload"] for x in r.captured if x["payload"]["method"] == "user.login")
        assert "user" in login["params"]
        assert "username" not in login["params"]
        # A subsequent authed call must put the token in the body `auth` field.
        c.get_problems()
        problem_call = next(x for x in r.captured if x["payload"]["method"] == "problem.get")
        assert problem_call["payload"]["auth"] == "sess-token-abc"
        assert "authorization" not in {k.lower() for k in problem_call["headers"]}


def test_modern_version_uses_username_param_and_bearer():
    r = make_router(version="7.0.0")
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        assert c._bearer is True
        login = next(x["payload"] for x in r.captured if x["payload"]["method"] == "user.login")
        assert "username" in login["params"]
        c.get_problems()
        problem_call = next(x for x in r.captured if x["payload"]["method"] == "problem.get")
        assert "auth" not in problem_call["payload"]
        assert problem_call["headers"].get("authorization") == "Bearer sess-token-abc"


def test_login_falls_back_to_other_param_on_error():
    """A misdetected modern version retries with the legacy `user` param."""
    state = {"first": True}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "apiinfo.version":
            return httpx.Response(200, json={"result": "7.0.0", "id": 1})
        if method == "user.login":
            # Reject the modern `username` param once, accept legacy `user`.
            if "username" in payload["params"] and state["first"]:
                state["first"] = False
                return httpx.Response(200, json={"error": {"message": "Invalid params"}, "id": 1})
            return httpx.Response(200, json={"result": "tok", "id": 1})
        return httpx.Response(200, json={"result": [], "id": 1})

    with respx.mock(assert_all_called=False) as router:
        router.post(ENDPOINT).mock(side_effect=handler)
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        assert c._token == "tok"
        assert c._bearer is False  # degraded to proven path


def test_auth_error_raised_on_bad_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "apiinfo.version":
            return httpx.Response(200, json={"result": "6.0.0", "id": 1})
        return httpx.Response(200, json={"error": {"message": "Login name or password is incorrect."}, "id": 1})

    with respx.mock(assert_all_called=False) as router:
        router.post(ENDPOINT).mock(side_effect=handler)
        with pytest.raises(ZapiAuthError):
            ZapiClient("https://zabbix.example.com", "u", "bad")


# ---- tag_filter -----------------------------------------------------------


def test_tag_filter_exists_when_no_value():
    assert tag_filter("dhcp") == {"tag": "dhcp", "operator": "4"}


def test_tag_filter_equal_when_value():
    assert tag_filter("role", "main") == {"tag": "role", "value": "main", "operator": "1"}


# ---- API methods ----------------------------------------------------------


def test_get_problems_passes_severities_and_returns_results():
    r = make_router(results={"problem.get": [SAMPLE_PROBLEM]})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        problems = c.get_problems(severities=[4, 5])
        assert problems[0]["eventid"] == "5001"
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "problem.get")
        assert call["params"]["severities"] == [4, 5]
        assert call["params"]["suppressed"] is False


def test_count_problems_uses_count_output():
    r = make_router(results={"problem.get": [SAMPLE_PROBLEM, SAMPLE_PROBLEM, SAMPLE_PROBLEM]})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        n = c.count_problems(severities=[4, 5])
        assert n == 3
        call = next(
            x["payload"]
            for x in r.captured
            if x["payload"]["method"] == "problem.get" and x["payload"]["params"].get("countOutput")
        )
        assert call["params"]["countOutput"] is True
        assert call["params"]["severities"] == [4, 5]
        assert call["params"]["suppressed"] is False
        assert "limit" not in call["params"]  # count must not be capped


def test_count_problems_raises_on_non_numeric_result():
    """A malformed countOutput reply surfaces as ZapiError rather than a silent 0."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        if method in ("apiinfo.version", "user.login"):
            return httpx.Response(200, json={"result": "6.0.0" if method == "apiinfo.version" else "tok", "id": 1})
        return httpx.Response(200, json={"result": "not-a-number", "id": 1})

    with respx.mock(assert_all_called=False) as router:
        router.post(ENDPOINT).mock(side_effect=handler)
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        with pytest.raises(ZapiError, match="non-numeric"):
            c.count_problems()


def test_get_hosts_by_exact_host_uses_filter():
    r = make_router(results={"host.get": [SAMPLE_HOST]})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        hosts = c.get_hosts(host="pool-a")
        assert hosts[0]["host"] == "pool-a"
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "host.get")
        assert call["params"]["filter"] == {"host": "pool-a"}


def test_get_items_by_key_uses_filter():
    r = make_router(results={"item.get": [SAMPLE_ITEM]})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        items = c.get_items(["100"], key="usage")
        assert items[0]["lastvalue"] == "85.5"
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "item.get")
        assert call["params"]["filter"] == {"key_": "usage"}


def test_get_hosts_by_group_resolves_group_id():
    r = make_router(results={"hostgroup.get": [{"groupid": "42"}], "host.get": [SAMPLE_HOST]})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        c.get_hosts(group="Routers")
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "host.get")
        assert call["params"]["groupids"] == ["42"]


def test_problem_get_sortfield_is_eventid_only():
    """problem.get only permits 'eventid' as a sortfield (not 'severity')."""
    r = make_router(results={"problem.get": [SAMPLE_PROBLEM]})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        c.get_problems()
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "problem.get")
        assert call["params"]["sortfield"] == "eventid"


def test_acknowledge_with_message_sets_message_bit():
    r = make_router()
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        c.acknowledge_problem(["5001"], "checked")
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "event.acknowledge")
        assert call["params"]["action"] == 6  # ack(2) + message(4)
        assert call["params"]["message"] == "checked"
        assert call["params"]["eventids"] == ["5001"]


def test_acknowledge_without_message_drops_message_bit():
    """Empty message must not set bit 4 (Zabbix rejects empty messages then)."""
    r = make_router()
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        c.acknowledge_problem(["5001"], "")
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "event.acknowledge")
        assert call["params"]["action"] == 2  # ack only
        assert "message" not in call["params"]


def test_close_is_idempotent_and_context_manager():
    with make_router():
        with ZapiClient("https://zabbix.example.com", "u", "p") as c:
            assert c._token == "sess-token-abc"
        c.close()  # second close must not raise


def test_wrong_password_attempts_login_only_once():
    """A credential failure must not trigger the fallback login attempt."""
    logins = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "apiinfo.version":
            return httpx.Response(200, json={"result": "7.0.0", "id": 1})
        if payload["method"] == "user.login":
            logins["n"] += 1
            return httpx.Response(200, json={"error": {"message": "Login name or password is incorrect."}, "id": 1})
        return httpx.Response(200, json={"result": [], "id": 1})

    with respx.mock(assert_all_called=False) as router:
        router.post(ENDPOINT).mock(side_effect=handler)
        with pytest.raises(ZapiAuthError):
            ZapiClient("https://zabbix.example.com", "u", "bad")
    assert logins["n"] == 1


def test_api_error_raised_for_non_login_method():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        if method in ("apiinfo.version", "user.login"):
            result = "6.0.0" if method == "apiinfo.version" else "tok"
            return httpx.Response(200, json={"result": result, "id": 1})
        return httpx.Response(200, json={"error": {"message": "boom"}, "id": 1})

    with respx.mock(assert_all_called=False) as router:
        router.post(ENDPOINT).mock(side_effect=handler)
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        with pytest.raises(ZapiError, match="problem.get failed"):
            c.get_problems()


def test_http_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] in ("apiinfo.version", "user.login"):
            result = "6.0.0" if payload["method"] == "apiinfo.version" else "tok"
            return httpx.Response(200, json={"result": result, "id": 1})
        return httpx.Response(500)

    with respx.mock(assert_all_called=False) as router:
        router.post(ENDPOINT).mock(side_effect=handler)
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        with pytest.raises(ZapiError, match="HTTP 500"):
            c.get_problems()


# ---- set_host_tag (write) -------------------------------------------------


def test_set_host_tag_preserves_other_tags():
    """Upserting a new tag keeps the host's existing tags."""
    r = make_router(results={"host.get": [SAMPLE_HOST], "host.update": {"hostids": ["100"]}})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        c.set_host_tag("pool-a", "speedtest-z", "0.8.5")
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "host.update")
        assert call["params"]["hostid"] == "100"
        tags = call["params"]["tags"]
        assert {"tag": "dhcp-pool-usage", "value": "1.0"} in tags  # existing kept
        assert {"tag": "speedtest-z", "value": "0.8.5"} in tags  # new added


def test_set_host_tag_replaces_same_name():
    """A tag with the same name is replaced (not duplicated); others survive."""
    host = {
        "hostid": "100",
        "host": "pool-a",
        "name": "Pool A",
        "status": "0",
        "tags": [
            {"tag": "speedtest-z", "value": "0.8.4"},
            {"tag": "location", "value": "tokyo"},
        ],
        "interfaces": [{"ip": "192.0.2.1"}],
    }
    r = make_router(results={"host.get": [host], "host.update": {"hostids": ["100"]}})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        c.set_host_tag("pool-a", "speedtest-z", "0.8.5")
        call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "host.update")
        tags = call["params"]["tags"]
        assert {"tag": "location", "value": "tokyo"} in tags  # untouched
        sp = [t for t in tags if t["tag"] == "speedtest-z"]
        assert sp == [{"tag": "speedtest-z", "value": "0.8.5"}]  # replaced, single entry


def test_set_host_tag_raises_when_host_missing():
    """An unknown host surfaces as ZapiError rather than a silent no-op."""
    r = make_router(results={"host.get": []})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        with pytest.raises(ZapiError, match="host not found"):
            c.set_host_tag("nope", "speedtest-z", "0.8.5")


def test_set_host_tag_strips_readonly_tag_fields():
    """Zabbix 6.4+ returns a read-only 'automatic' field on each tag; it must
    not be re-submitted to host.update, which rejects unknown tag keys.

    Also pins the data-fetch contract the merge relies on (selectTags=extend +
    exact host filter) and that multiple existing tags survive.
    """
    host = {
        "hostid": "100",
        "host": "pool-a",
        "name": "Pool A",
        "status": "0",
        "tags": [
            {"tag": "location", "value": "tokyo", "automatic": "0"},
            {"tag": "role", "value": "edge", "automatic": "0"},
            {"tag": "speedtest-z", "value": "0.8.4", "automatic": "0"},
        ],
        "interfaces": [{"ip": "192.0.2.1"}],
    }
    r = make_router(results={"host.get": [host], "host.update": {"hostids": ["100"]}})
    with r:
        c = ZapiClient("https://zabbix.example.com", "u", "p")
        result = c.set_host_tag("pool-a", "speedtest-z", "0.8.5")
        # The merge depends on host.get returning tags for the exact host.
        get_call = next(x["payload"] for x in r.captured if x["payload"]["method"] == "host.get")
        assert get_call["params"]["selectTags"] == "extend"
        assert get_call["params"]["filter"] == {"host": "pool-a"}
        # host.update payload: every tag carries only the writable keys.
        upd = next(x["payload"] for x in r.captured if x["payload"]["method"] == "host.update")
        tags = upd["params"]["tags"]
        assert all(set(t.keys()) == {"tag", "value"} for t in tags)
        assert {"tag": "location", "value": "tokyo"} in tags  # preserved, normalized
        assert {"tag": "role", "value": "edge"} in tags  # preserved, normalized
        sp = [t for t in tags if t["tag"] == "speedtest-z"]
        assert sp == [{"tag": "speedtest-z", "value": "0.8.5"}]  # replaced, single entry
        assert result == {"hostids": ["100"]}  # documented return value


def test_init_closes_http_client_on_failure(monkeypatch):
    """A failure during __init__ (api_version/_login) closes the httpx.Client
    rather than leaking it -- the with-statement cannot protect construction."""
    closed = {"n": 0}
    real_close = httpx.Client.close

    def spy_close(self):
        closed["n"] += 1
        return real_close(self)

    monkeypatch.setattr(httpx.Client, "close", spy_close)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "apiinfo.version":
            return httpx.Response(200, json={"result": "6.0.0", "id": 1})
        # user.login fails -> __init__ raises after the http client is created
        return httpx.Response(200, json={"error": {"message": "Login name or password is incorrect."}, "id": 1})

    with respx.mock(assert_all_called=False) as router:
        router.post(ENDPOINT).mock(side_effect=handler)
        with pytest.raises(ZapiAuthError):
            ZapiClient("https://zabbix.example.com", "u", "bad")
    assert closed["n"] >= 1  # closed on failed init, not leaked
