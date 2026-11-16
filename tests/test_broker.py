"""P3 — real broker layer tests.

Full connection tests are skipped unless ``KGOV_LIVE_BROKER`` is set; the unit
surface (helping functions + graceful ``NeedsBrokerError`` gating) always runs.
"""

from __future__ import annotations

import json
import os

import pytest

from kgov.broker import (
    BrokerMessage,
    NeedsBrokerError,
    _decode_value,
    _json_safe,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_json_safe_utf8():
    assert _json_safe(b"hello") == "hello"


def test_json_safe_binary_hex():
    assert _json_safe(b"\xff\xfe") == {"$bytes": "fffe"}


def test_json_safe_passthrough():
    assert _json_safe({"x": 1}) == {"x": 1}
    assert _json_safe(None) is None


def test_decode_value_json_object():
    assert _decode_value(b'{"amount_cents": 100}') == {"amount_cents": 100}


def test_decode_value_raw_text_fallback():
    assert _decode_value(b"plain text") == "plain text"


def test_decode_value_binary_fallback():
    assert _decode_value(b"\xff\xfe") == {"$bytes": "fffe"}


def test_decode_value_none():
    assert _decode_value(None) is None


def test_broker_message_to_dict_serializable():
    msg = BrokerMessage(
        topic="payments",
        partition=0,
        offset=42,
        key=b"pay_042",
        value={"amount_cents": 100},
        timestamp_ms=1699999999999,
        headers={"x-tenant": "acme"},
    )
    d = msg.to_dict()
    json.dumps(d)  # must not raise
    assert d["topic"] == "payments"
    assert d["offset"] == 42
    assert d["key"] == "pay_042"


# ---------------------------------------------------------------------------
# Confluent-wire Avro decoding
# ---------------------------------------------------------------------------


def _avro_wire_bytes(schema_id: int, record: dict, avro_schema: dict) -> bytes:
    """Encode a record into the Confluent wire format (magic + id + datum)."""
    import io
    import json

    from avro import io as avro_io
    from avro import schema as avro_schema_mod

    parsed = avro_schema_mod.parse(json.dumps(avro_schema))
    buf = io.BytesIO()
    writer = avro_io.DatumWriter(parsed)
    writer.write(record, avro_io.BinaryEncoder(buf))
    return b"\x00" + schema_id.to_bytes(4, byteorder="big") + buf.getvalue()


_PAYMENT_SCHEMA = {
    "type": "record",
    "name": "Payment",
    "namespace": "com.example",
    "fields": [
        {"name": "payment_id", "type": "string"},
        {"name": "currency", "type": "string"},
    ],
}


def test_avro_decoder_round_trip(registry_server):
    """A Confluent-wire Avro payload with a registered schema decodes to a dict."""
    from kgov.broker import AvroRecordDecoder, _UNDECODED

    schema_id = 1001
    registry_server.add_subject(
        "payments-value",
        {
            "id": schema_id,
            "version": 1,
            "compatibility": "BACKWARD",
            "schema": _PAYMENT_SCHEMA,
        },
    )
    raw = _avro_wire_bytes(schema_id, {"payment_id": "pay_001", "currency": "USD"}, _PAYMENT_SCHEMA)
    decoder = AvroRecordDecoder(registry_server.client)
    assert decoder(raw) == {"payment_id": "pay_001", "currency": "USD"}


def test_avro_decoder_unknown_schema_id(registry_server):
    """An id the registry does not know falls back to the UNDECODED sentinel."""
    from kgov.broker import AvroRecordDecoder, _UNDECODED

    decoder = AvroRecordDecoder(registry_server.client)
    raw = _avro_wire_bytes(424242, {"payment_id": "pay_1", "currency": "USD"}, _PAYMENT_SCHEMA)
    assert decoder(raw) is _UNDECODED


def test_avro_decoder_rejects_non_wire_bytes(registry_server):
    """Bytes that are not Confluent-wire framed are left for the text fallback."""
    from kgov.broker import AvroRecordDecoder, _UNDECODED

    decoder = AvroRecordDecoder(registry_server.client)
    assert decoder(b"plain text payload") is _UNDECODED
    assert decoder(b"") is _UNDECODED


def test_avro_decoder_malformed_datum(registry_server):
    """A wire frame whose datum is not decodable returns UNDECODED (no crash)."""
    from kgov.broker import AvroRecordDecoder, _UNDECODED

    schema_id = 2002
    registry_server.add_subject(
        "broken-value",
        {
            "id": schema_id,
            "version": 1,
            "compatibility": "BACKWARD",
            "schema": _PAYMENT_SCHEMA,
        },
    )
    trash = b"\x00" + schema_id.to_bytes(4, byteorder="big") + b"\xff\xff\xff\xff\xff"
    decoder = AvroRecordDecoder(registry_server.client)
    assert decoder(trash) is _UNDECODED


def test_decode_value_json_preferred_over_avro():
    """Valid JSON bytes are decoded as JSON; the Avro decoder is never asked."""
    from kgov.broker import _UNDECODED, _decode_value

    def boom(raw: bytes) -> object:  # pragma: no cover - must not be reached
        raise AssertionError("decoder should not be called for JSON")

    assert _decode_value(b'{"amount_cents": 100}', boom) == {"amount_cents": 100}


def test_decode_value_avro_fallback(registry_server):
    """Opaque non-JSON bytes flow through the supplied Avro decoder."""
    from kgov.broker import AvroRecordDecoder, _decode_value

    schema_id = 1001
    registry_server.add_subject(
        "payments-value",
        {
            "id": schema_id,
            "version": 1,
            "compatibility": "BACKWARD",
            "schema": _PAYMENT_SCHEMA,
        },
    )
    decoder = AvroRecordDecoder(registry_server.client)
    raw = _avro_wire_bytes(schema_id, {"payment_id": "pay_9", "currency": "EUR"}, _PAYMENT_SCHEMA)
    assert _decode_value(raw, decoder) == {"payment_id": "pay_9", "currency": "EUR"}


def test_decode_value_text_when_decoder_refuses():
    """If the decoder declines, plain UTF-8 text is returned as a string."""
    from kgov.broker import _UNDECODED, _decode_value

    def refuse(raw: bytes) -> object:
        return _UNDECODED

    assert _decode_value(b"plain text", refuse) == "plain text"


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_require_kafka_when_missing(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "confluent_kafka", None)
    with pytest.raises(NeedsBrokerError):
        from kgov import broker

        broker._require_kafka()


def test_broker_error_message():
    err = NeedsBrokerError("install with `pip install 'kgov[broker]'`")
    assert "kgov[broker]" in str(err)


# ---------------------------------------------------------------------------
# Connectivity probe
# ---------------------------------------------------------------------------


def test_probe_bootstrap_unreachable_raises(monkeypatch):
    """A scan of a dead cluster must fail fast, not silently return []."""
    from kgov import broker
    from kgov.broker import BrokerConnectError

    class FakeAdminClient:
        def __init__(self, conf):
            self.conf = conf

        def list_topics(self, timeout: float = 10.0):
            raise RuntimeError("Failed to get metadata: _TRANSPORT")

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    with pytest.raises(BrokerConnectError, match="cannot reach broker"):
        broker._probe_bootstrap("127.0.0.1:9", timeout=1)


def test_probe_bootstrap_reachable_ok(monkeypatch):
    """A reachable cluster (even with an unknown topic) must not raise."""
    from kgov import broker

    class FakeAdminClient:
        def __init__(self, conf):
            self.conf = conf

        def list_topics(self, timeout: float = 10.0):
            return type("MD", (), {"topics": {}})()

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    broker._probe_bootstrap("127.0.0.1:9", timeout=1)  # must not raise


@pytest.mark.skipif(
    not os.environ.get("KGOV_LIVE_BROKER"),
    reason="set KGOV_LIVE_BROKER=host:port to run live broker integration",
)
def test_list_topics_live():
    from kgov.broker import list_topics

    topics = list_topics(os.environ["KGOV_LIVE_BROKER"])
    assert isinstance(topics, list)


@pytest.mark.skipif(
    not os.environ.get("KGOV_LIVE_BROKER"),
    reason="set KGOV_LIVE_BROKER=host:port to run live broker integration",
)
def test_read_topic_live():
    from kgov.broker import read_topic
    from kgov.rules import Message, RuleSet, build_ruleset, validate_message

    rs = build_ruleset({"version": 1, "topic": "payments", "fields": {}})
    msgs = read_topic(os.environ["KGOV_LIVE_BROKER"], "payments", n=5, timeout=10)
    assert len(msgs) <= 5
    if msgs:
        v = validate_message(
            rs,
            Message(
                topic=msgs[0].topic,
                partition=msgs[0].partition,
                offset=msgs[0].offset,
                key=msgs[0].key,
                value=msgs[0].value,
            ),
        )
        assert isinstance(v, list)
