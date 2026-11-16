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


def test_bind_failure_raises():
    reg = MockRegistry()
    with pytest.raises(MockRegistryError):
        reg.start(port=1)  # port 1 is not bindable without privileges
    reg.stop()
