"""Tests for the Confluent-wire Schema Registry client + mock server.

The mock registry binds an ephemeral loopback port, so the suite exercises the
real HTTP path (client -> in-process server) end to end. No external network
is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kgov.mock_registry import MockRegistry, MockRegistryError
from kgov.registry import RegistryError, SchemaRegistryClient

FIXTURES = Path(__file__).parent / "fixtures"

FIXTURE = {
    "subjects": {
        "payments-value": [
            {
                "id": 1,
                "version": 1,
                "compatibility": "BACKWARD",
                "schema": {"type": "record", "name": "Payment", "fields": []},
            },
            {
                "id": 2,
                "version": 2,
                "compatibility": "BACKWARD",
                "schema": {"type": "record", "name": "Payment", "fields": [{"name": "fee_cents", "type": ["null", "long"], "default": None}]},
            },
        ],
        "orders-value": [
            {
                "id": 3,
                "version": 1,
                "compatibility": "BREAKING",
                "schema": {"type": "record", "name": "Order", "fields": [{"name": "total_cents", "type": "long"}]},
            }
        ],
    }
}


def test_junk_base_url_raises_designed_registry_error():
    """A junk base_url (an int, a list, bytes) would leak a raw ValueError
    ('unknown url type') from urllib Request construction before any network
    call; it must surface as the designed RegistryError instead."""
    for bad in (1, [], {}, b"x", None, ["http://x"]):
        c = SchemaRegistryClient(bad)
        with pytest.raises(RegistryError):
            c._get("/subjects")


@pytest.fixture
def server():
    reg = MockRegistry(dict(FIXTURE))
    reg.start()
    try:
        yield reg
    finally:
        reg.stop()


@pytest.fixture
def client(server) -> SchemaRegistryClient:
    return server.client


def test_list_subjects(client):
    assert client.list_subjects() == ["orders-value", "payments-value"]


def test_get_versions(client):
    assert client.get_versions("payments-value") == [1, 2]
    assert client.get_versions("orders-value") == [1]


def test_get_version(client):
    v1 = client.get_version("payments-value", 1)
    assert v1["id"] == 1
    assert v1["version"] == 1
    assert v1["compatibility"] == "BACKWARD"
    assert v1["schema"]["name"] == "Payment"

    v2 = client.get_version("payments-value", 2)
    assert v2["id"] == 2
    assert v2["compatibility"] == "BACKWARD"


def test_get_latest(client):
    latest = client.get_latest("payments-value")
    assert latest["version"] == 2
    assert latest["id"] == 2


def test_compatibility_level(client):
    assert client._compatibility_level("payments-value") == "BACKWARD"
    assert client._compatibility_level("orders-value") == "BREAKING"


def test_schema_by_id(client):
    schema = client.get_schema_by_id(3)
    assert schema["id"] == 3
    assert schema["schema"]["fields"][0]["name"] == "total_cents"


def test_no_subject(client):
    with pytest.raises(RegistryError):
        client.get_version("does-not-exist", 1)


def test_fixture_dict_roundtrip(client):
    fixture = client.to_fixture_dict()
    assert set(fixture["subjects"]) == {"orders-value", "payments-value"}
    assert len(fixture["subjects"]["payments-value"]) == 2
    assert fixture["subjects"]["orders-value"][0]["version"] == 1


def test_iso_string_schemas_are_normalized():
    # Confluent wire wraps schema as a *string*; client must parse it back.
    fixture = {
        "subjects": {
            "charlie-value": [
                {
                    "id": 7,
                    "version": 1,
                    "compatibility": "FORWARD",
                    "schema": {"type": "record", "name": "Charlie", "fields": [{"name": "a", "type": "string"}]},
                }
            ]
        }
    }
    with MockRegistry(fixture) as reg:
        c = reg.client
        info = c.get_version("charlie-value", 1)
        assert info["schema"]["fields"][0]["name"] == "a"
        assert c._compatibility_level("charlie-value") == "FORWARD"


def test_mock_registry_loads_disk_fixture():
    path = FIXTURES / "registry" / "registry.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    with MockRegistry(fixture) as reg:
        c = reg.client
        assert c.list_subjects() == ["orders-value", "payments-value"]
        latest = c.get_latest("payments-value")
        assert latest["version"] == 2


def test_mock_registry_tolerates_junk_version_entries():
    """Junk version entries (ints, strings, lists, None, junk-typed
    id/version/compatibility values) must not crash the mock accessors —
    they are dropped, and unparseable int fields fall back to defaults."""
    reg = MockRegistry({
        "subjects": {
            "junked": [None, 42, "nope", [1], {"junk": True},
                       {"id": {"x": 1}, "version": 1, "compatibility": "BACKWARD"}],
            "ok": [{"id": 3, "version": 1, "schema": "s3"}],
        }
    })
    assert reg.list_subjects() == ["junked", "ok"]
    # non-dict entries are ignored; both dict entries survive (one without a
    # version key falls back to its enumeration slot 1, the other has version 1)
    assert reg.versions("junked") == [1, 1]
    info = reg.version("junked", "latest")
    assert info["version"] == 1
    assert info["id"] is None  # junk id falls back to None
    assert info["compatibility"] == "BACKWARD"
    assert reg.compatibility_level("junked") == "BACKWARD"
    assert reg.schema_by_id(3) == "s3"
    # a malformed subjects container (e.g. a bare list) must not crash __init__
    reg2 = MockRegistry({"subjects": [1, 2]})
    assert reg2.list_subjects() == []
    assert reg2.compatibility_level(None) == "BACKWARD"


def test_add_subject_tolerates_junk_versions_and_accessors_safe():
    """Junk version entries passed to add_subject (which bypasses the
    __init__/set_fixture filter) must be dropped, not crash the accessors
    with AttributeError on .get(); valid dict versions still resolve."""
    reg = MockRegistry()
    reg.add_subject(
        "payments-value",
        {"id": 1, "version": 1, "compatibility": "BACKWARD", "schema": {"t": "int"}},
        42, "junk", None, [1],
        {"id": 9, "version": 2, "compatibility": "FORWARD", "schema": {"t": "str"}},
    )
    assert reg.list_subjects() == ["payments-value"]
    # both valid dict entries survive; junk is dropped
    assert reg.versions("payments-value") == [1, 2]
    assert reg.schema_by_id(9) == {"t": "str"}
    assert reg.compatibility_level("payments-value") == "FORWARD"
    # a subject seeded with only junk stays inert, never crashes
    reg.add_subject("all-junk", 42, "nope", None, [1])
    assert reg.versions("all-junk") == []
    assert reg.version("all-junk") is None
    assert reg.compatibility_level("all-junk") is None
    assert reg.schema_by_id(42) is None



def test_set_fixture_swap(server):
    client = server.client
    assert client.list_subjects() == ["orders-value", "payments-value"]
    server.set_fixture({"subjects": {"new-value": [{"id": 9, "version": 1, "schema": {"type": "record", "name": "N", "fields": []}}]}})
    assert client.list_subjects() == ["new-value"]


def test_unreachable_registry():
    client = SchemaRegistryClient("http://127.0.0.1:1")
    assert client.is_reachable() is False
    with pytest.raises(RegistryError):
        client.list_subjects()


def test_get_schema_by_id_junk_args_raise_registry_error():
    """Junk schema_id args (dict, list, None, non-numeric str, tuple, set) to
    get_schema_by_id must raise a clean RegistryError, not a raw TypeError /
    ValueError from int() at the URL-build site. Valid int and numeric-string
    ids still resolve (wire stubbed)."""
    c = SchemaRegistryClient("http://127.0.0.1:1")
    for bad in ({"x": 1}, [1], None, "abc", (1, 2), {1}):
        with pytest.raises(RegistryError, match="schema id"):
            c.get_schema_by_id(bad)
    c._get = lambda path: {"schema": '{"type": "record", "name": "R", "fields": []}'}
    assert c.get_schema_by_id(7)["id"] == 7
    assert c.get_schema_by_id("9")["id"] == 9


def test_get_non_utf8_body_raises_registry_error(monkeypatch):
    """A non-UTF-8 response body must raise a clean RegistryError, not leak a
    raw UnicodeDecodeError (a ValueError subclass that JSONDecodeError's
    handler does not cover)."""
    import urllib.request

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"\xff\xfe\x00\x99"

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=10: FakeResp())
    c = SchemaRegistryClient("http://127.0.0.1:1")
    with pytest.raises(RegistryError, match="UTF-8"):
        c.list_subjects()


def test_junk_timeout_raises_registry_error():
    """Junk / out-of-range timeout values (non-numeric, NaN, negative, too
    large for time_t) must raise a clean RegistryError, not leak raw
    TypeError/ValueError/OverflowError from socket.settimeout before any
    connection attempt. Valid numeric timeouts still raise RegistryError only
    for the actual connection refusal."""
    c = SchemaRegistryClient("http://127.0.0.1:1")
    for bad in ("abc", {}, [], 1.5, None, -5, True, b"x", (1, 2),
                1e300, float("inf"), float("nan")):
        client = SchemaRegistryClient("http://127.0.0.1:1", timeout=bad)
        with pytest.raises(RegistryError):
            client.list_subjects()
        assert client.is_reachable() is False
    with pytest.raises(RegistryError):
        c.list_subjects()


def test_mock_registry_wire_tolerates_junk_schema_values():
    """Junk fixture schema values (bytes, sets, junk-typed compatibility,
    heterogeneous subject keys) must survive the HTTP wire: the handler
    serializes them best-effort instead of dying mid-response, so the client
    never sees a mid-body disconnect (RemoteDisconnected)."""
    with MockRegistry({
        "subjects": {
            7: [{"id": 1, "version": 1, "schema": {"t": "str"}}],
            "b": [{"id": 2, "version": 1, "compatibility": {"x": 1},
                   "schema": b"raw-bytes"}],
        }
    }) as reg:
        c = reg.client
        # heterogeneous subject keys sort as strings over the wire
        assert c.list_subjects() == ["7", "b"]
        # junk compatibility coerced to a safe string, junk schema serialized
        info = c.get_version("b", 1)
        assert info["schema"] == '{"t": "str"}' or "raw-bytes" in str(info["schema"])
        # /schemas/ids for the junk schema resolves without a disconnect
        by_id = c.get_schema_by_id(2)
        assert by_id["id"] == 2
        assert "raw-bytes" in str(by_id["schema"])


def test_bind_failure_raises():
    reg = MockRegistry()
    with pytest.raises(MockRegistryError):
        reg.start(port=1)  # port 1 is not bindable without privileges
    reg.stop()


def test_mock_registry_start_junk_ports_raise_clean_error():
    """Junk port args (str, float, None, list, dict, bytes, tuple, set,
    negative int) must raise a clean MockRegistryError from start(), not a
    raw TypeError/OverflowError leaking out of the socketserver bind."""
    for bad in ("abc", 1.5, None, ["a"], {"p": 1}, -1, b"x", (1, 2), {1, 2}):
        reg = MockRegistry()
        with pytest.raises(MockRegistryError):
            reg.start(port=bad)
        reg.stop()  # must be safe to stop a failed server


def test_mock_registry_restart_contract():
    """Lifecycle round-trip: start binds an ephemeral port and serves;
    double-start is idempotent (same URL, no rebind); stop is repeatable;
    and a fresh start after stop binds a new port that serves again. The
    CLI test fixtures and offline workflows depend on this contract."""
    import socket

    reg = MockRegistry(dict(FIXTURE))
    url = reg.start()
    port = int(url.rsplit(":", 1)[1])
    assert port > 0

    def one(raw, target_port):
        s = socket.create_connection(("127.0.0.1", target_port), timeout=5)
        try:
            s.sendall(raw)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        return data

    assert one(b"GET /subjects HTTP/1.0\r\n\r\n", port).startswith(b"HTTP/1.0 200")

    # double-start must not rebind a different port or return a new URL
    assert reg.start() == url
    assert int(reg.base_url.rsplit(":", 1)[1]) == port

    reg.stop()
    reg.stop()  # idempotent: second stop is a no-op
    assert reg.base_url == "http://127.0.0.1:0"

    # restart after stop binds anew and serves again
    url2 = reg.start()
    port2 = int(url2.rsplit(":", 1)[1])
    assert port2 > 0
    assert port2 != port
    assert one(b"GET /mode HTTP/1.0\r\n\r\n", port2).startswith(b"HTTP/1.0 200")
    reg.stop()


def test_registry_handler_path_parts_tolerates_junk_paths():
    """RegistryHandler._path_parts/_subject_from_path with a jail-broken
    self.path (non-strings, missing, odd URL shapes, embedded NULs, junk
    bytes) must not crash the handler thread — junk resolves to no path /
    no subject, normal paths still resolve."""
    from kgov.mock_registry import RegistryHandler

    class FakeHandler(RegistryHandler):
        def __init__(self, path):
            self.path = path

    # non-string / junk path values yield no parts and no subject
    for bad in (42, None, ["a"], {"k": 1}, b"x", ("a", "b"), object(), 2.5, {1, 2}):
        h = FakeHandler(bad)
        assert RegistryHandler._path_parts(h) == []
        assert RegistryHandler._subject_from_path(h) is None
    # odd-but-string paths never crash; subject extraction stays correct
    for odd in ("not-a-url", "//subjects/x", "http:///subjects/x",
                "subjects/x/versions", "", "/", "\x00/subjects/x"):
        parts = RegistryHandler._path_parts(FakeHandler(odd))
        assert isinstance(parts, list)
    h = FakeHandler("/subjects/payments-value/versions/1")
    assert RegistryHandler._path_parts(h) == ["subjects", "payments-value", "versions", "1"]
    assert RegistryHandler._subject_from_path(h) == "payments-value"
    h = FakeHandler("/config/orders-value")
    assert RegistryHandler._subject_from_path(h) == "orders-value"


def test_mock_registry_tolerates_junk_fixture_and_set_fixture():
    """A junk fixture argument (int, str, list, set, tuple, object, float,
    None) to MockRegistry(...)/set_fixture is not a subjects mapping — it
    must yield an empty backing store, not a raw AttributeError on .get()."""
    junk = (42, 2.5, "x", [1, 2], {"s"}, (1, 2), object(), None)
    for bad in junk:
        reg = MockRegistry(fixture=bad)
        assert reg.list_subjects() == []
        reg.set_fixture(bad)
        assert reg.list_subjects() == []
        reg.stop()


def test_mock_registry_tolerates_unhashable_subject_args():
    """Unhashable subject args (list, dict, set) must not raise a raw
    TypeError from setdefault/.get; they are coerced to stable string keys."""
    reg = MockRegistry()
    for bad in (["a"], {"k": 1}, {1, 2}):
        reg.add_subject(bad, {"version": 1})
        assert reg.versions(bad) == [1]
        assert reg.version(bad) is not None
        assert reg.compatibility_level(bad) == "BACKWARD"
    assert set(reg.list_subjects()) == {"['a']", "{1, 2}", "{'k': 1}"}
    # a plain str subject still works exactly as before
    reg.add_subject("plain", {"version": 1})
    assert reg.versions("plain") == [1]


def test_registry_handler_reply_tolerates_junk_payloads():
    """RegistryHandler._reply with a junk payload (bytes, sets, objects,
    non-string dict keys) must still write a body, not die mid-response
    (which the client would see as RemoteDisconnected)."""
    import io

    from kgov.mock_registry import RegistryHandler

    captures = []

    class CaptureHandler:
        def __init__(self):
            self.wfile = io.BytesIO()
        def send_response(self, code):
            captures.append(("response", code))
        def send_header(self, k, v):
            captures.append(("header", k, v))
        def end_headers(self):
            captures.append(("end",))

    for payload in (42, "s", {"a": 1}, {"k": b"b"}, {1, 2}, object(), [b"x"],
                    {"k": {"n": b"d"}}, None, (1, 2), {"k": 1, 2: "b"}, b"\xff"):
        captures.clear()
        h = CaptureHandler()
        RegistryHandler._reply(h, 200, payload)  # must not raise
        assert h.wfile.getvalue(), "a body must have been written"
    # valid envelope unchanged
    captures.clear()
    h = CaptureHandler()
    RegistryHandler._reply(h, 201, {"ok": [1, 2]})
    assert h.wfile.getvalue() == b'{"ok": [1, 2]}'


def test_as_int_tolerates_out_of_range_float():
    """_as_int must fall back to its default for out-of-range floats (inf,
    values beyond the platform double range) whose int() raises OverflowError
    — not leak the raw traceback."""
    from kgov.mock_registry import _as_int

    assert _as_int(5, -1) == 5
    assert _as_int(2.0, -1) == 2
    for bad in (float("inf"), float("-inf"), float("nan"), 1e400):
        assert _as_int(bad, -1) == -1
    assert _as_int("7", -1) == 7
    for bad in ("x", None, [1], {"a": 1}, b"x"):
        assert _as_int(bad, -1) == -1


def test_mock_registry_schema_by_id_tolerates_out_of_range_id():
    """schema_by_id with out-of-range float ids (inf, 1e400) must resolve to
    None (no match) rather than raw-crash on OverflowError from int()."""
    reg = MockRegistry(dict(FIXTURE))
    for bad in (float("inf"), float("-inf"), float("nan"), 1e400):
        assert reg.schema_by_id(bad) is None  # must not raise
    # a valid bound id still resolves (to the fixture's schema dict, id 2)
    assert reg.schema_by_id(2)["name"] == "Payment"


def test_registry_handler_do_get_fails_closed_without_registry():
    """A RegistryHandler built without a bound registry (a direct call or
    test double) must fail closed to a clean 404 response on do_GET rather
    than raw-crash on attribute access mid-route."""
    import io

    from kgov.mock_registry import RegistryHandler

    captures = []

    class BareHandler(RegistryHandler):
        def __init__(self, path):
            self.path = path
            self.wfile = io.BytesIO()
        def send_response(self, code, *args):
            captures.append(("response", code))
        def send_header(self, k, v):
            captures.append(("header", k, v))
        def end_headers(self):
            captures.append(("end",))

    # NOTE: no `registry` attribute anywhere on the handler instance/class
    for path in ("/subjects", "/subjects/pay/versions", "/schemas/ids/1",
                 "/config/pay", "/mode", "/nope"):
        captures.clear()
        h = BareHandler(path)
        h.do_GET()  # must not raise AttributeError
        assert captures[0] == ("response", 404)
        assert h.wfile.getvalue(), "a body must have been written"
    for junk_path in (None, 42, ["a"], b"x"):
        captures.clear()
        h = BareHandler(junk_path)
        h.do_GET()  # must not raise on junk paths either
        assert h.wfile.getvalue()
