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


def test_decode_value_junk_payloads_passthrough():
    """A junk payload (str/int/list/... — not bytes) reaching _decode_value
    must not raw-crash on .decode(): hand it back unchanged as a usable value."""
    for junk in ("abc", 123, ["a"], {"x": 1}, True, object()):
        assert _decode_value(junk) == junk


def test_record_reader_read_junk_payload_designed_value_error():
    """Junk/empty payloads must fail through the designed ValueError contract
    (Fix 32, _RecordReader.read), never leak raw avro/io exceptions. A junk
    schema object must also fold AttributeError/TypeError into that ValueError
    rather than leak third-party internals."""
    from kgov.broker import _RecordReader

    r = _RecordReader(object())
    for junk in (None, 123, "abc", b"", 1.5):
        with pytest.raises(ValueError):
            r.read(junk)


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


def test_avro_decoder_tolerates_non_bytes_raw(registry_server):
    """Junk non-bytes raw payloads (None, numbers, strs, bools, containers,
    bytearray, memoryview) must return the UNDECODED sentinel, not crash with
    TypeError/AttributeError on len()/startswith() at the wire-format gate."""
    from kgov.broker import AvroRecordDecoder, _UNDECODED

    decoder = AvroRecordDecoder(registry_server.client)
    for raw in (None, 0, 1, -1, 3.14, True, False, "strvalue", "\x00" * 6,
                [], (), {}, {"a": 1}, [1, 2, 3], bytearray(b"\x00"), memoryview(b"\x00")):
        assert decoder(raw) is _UNDECODED, f"raw {raw!r} must be UNDECODED"


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


def test_record_reader_read_junk_payloads_designed_value_error():
    """A direct _RecordReader.read call with an empty/truncated/junk payload
    must raise a designed ValueError, not a third-party avro exception
    traceback (InvalidAvroBinaryEncoding). A valid datum still decodes."""
    import io

    from avro import io as avro_io
    from avro import schema as avro_schema

    from kgov.broker import _RecordReader

    parsed = avro_schema.parse(json.dumps(_PAYMENT_SCHEMA))
    reader = _RecordReader(parsed)
    for junk in (None, b"", b"\xff\x01\x02", b"\xff\xff\xff\xff\xff"):
        with pytest.raises(ValueError, match="invalid avro payload"):
            reader.read(junk)
    buf = io.BytesIO()
    writer = avro_io.DatumWriter(parsed)
    writer.write(
        {"payment_id": "pay_001", "currency": "USD"},
        avro_io.BinaryEncoder(buf),
    )
    assert reader.read(buf.getvalue()) == {"payment_id": "pay_001", "currency": "USD"}


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


def test_decode_value_tolerates_misbehaving_decoder():
    """A decoder that raises must not abort the scan — the text / hex fallback
    kicks in exactly as if the decoder had declined the payload."""
    from kgov.broker import _decode_value

    def boom(raw: bytes) -> object:  # pragma: no cover - body never runs
        raise RuntimeError("boom")

    assert _decode_value(b"plain text", boom) == "plain text"
    assert _decode_value(b"\xff\xfe", boom) == {"$bytes": "fffe"}


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
# Junk-value gates (fire before any confluent-kafka import / network)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", 0, False, [], ["x"], {}, {"a": 1}, 42, b"host", 3.5])
def test_list_topics_junk_bootstrap_raises(value):
    from kgov.broker import list_topics

    with pytest.raises(ValueError, match="bootstrap"):
        list_topics(value)


@pytest.mark.parametrize("timeout", ["abc", None, 0, -1, True, float("nan"), float("inf")])
def test_list_topics_junk_timeout_raises(timeout):
    from kgov.broker import list_topics

    with pytest.raises(ValueError, match="timeout"):
        list_topics("127.0.0.1:9092", timeout=timeout)


@pytest.mark.parametrize("value", [None, "", 0, False, [], ["x"], {}, {"a": 1}, 42, b"host", 3.5])
def test_consumer_groups_junk_bootstrap_raises(value):
    from kgov.broker import consumer_groups

    with pytest.raises(ValueError, match="bootstrap"):
        consumer_groups(value)


def test_consumer_groups_junk_timeout_raises():
    from kgov.broker import consumer_groups

    with pytest.raises(ValueError, match="timeout"):
        consumer_groups("127.0.0.1:9092", timeout="abc")


@pytest.mark.parametrize("value", [None, "", 0, False, [], ["x"], {}, {"a": 1}, 42, b"host", 3.5])
def test_read_topic_junk_bootstrap_raises(value):
    from kgov.broker import read_topic

    with pytest.raises(ValueError, match="bootstrap"):
        read_topic(value, "payments")


@pytest.mark.parametrize("value", [None, "", 0, [], ["t"], 42, b"t", {"x": 1}])
def test_read_topic_junk_topic_raises(value):
    from kgov.broker import read_topic

    with pytest.raises(ValueError, match="topic"):
        read_topic("127.0.0.1:9092", value)


@pytest.mark.parametrize("timeout", ["abc", None, 0, -1, True, float("nan")])
def test_read_topic_junk_timeout_raises(timeout):
    from kgov.broker import read_topic

    with pytest.raises(ValueError, match="timeout"):
        read_topic("127.0.0.1:9092", "payments", timeout=timeout)


@pytest.mark.parametrize("n", ["abc", 0, -1, True, 1.5, 3.0])
def test_read_topic_junk_n_raises(n):
    from kgov.broker import read_topic

    with pytest.raises(ValueError, match="\\bn\\b"):
        read_topic("127.0.0.1:9092", "payments", n=n)


@pytest.mark.parametrize("group_id", [None, "", 42, b"g", [], {}])
def test_read_topic_junk_group_id_raises(group_id):
    from kgov.broker import read_topic

    with pytest.raises(ValueError, match="group_id"):
        read_topic("127.0.0.1:9092", "payments", group_id=group_id)


def test_probe_bootstrap_junk_raises():
    from kgov.broker import _probe_bootstrap

    with pytest.raises(ValueError, match="bootstrap"):
        _probe_bootstrap(42, 1.0)


@pytest.mark.parametrize("timeout", ["abc", None, 0, -1, True, float("nan"), float("inf")])
def test_probe_bootstrap_junk_timeout_raises(timeout):
    """Junk timeouts must fail at the gate, never leak int() OverflowError."""
    from kgov.broker import _probe_bootstrap

    with pytest.raises(ValueError, match="timeout"):
        _probe_bootstrap("127.0.0.1:9092", timeout)


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


# ---------------------------------------------------------------------------
# Fake-client success bodies (the confluent-kafka join, no live broker)
# ---------------------------------------------------------------------------


def test_list_topics_client_error_wraps(monkeypatch):
    """A client-side list_topics failure surfaces as the designed
    BrokerConnectError (chained from the raw exception), ''[41]'' not a raw
    confluent exception."""
    from kgov import broker
    from kgov.broker import BrokerConnectError

    class FakeAdminClient:
        def __init__(self, conf):
            self.conf = conf

        def list_topics(self, timeout: float = 10.0):
            raise RuntimeError("Failed to get metadata: _TRANSPORT")

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    with pytest.raises(BrokerConnectError, match="cannot reach broker"):
        broker.list_topics("127.0.0.1:9", timeout=1.0)


def test_list_topics_metadata_mirror_and_error_skip(monkeypatch):
    """list_topics success body mirrors client metadata into the documented
    dict shape (sorted by name, error-carrying topics skipped, rf defaults to
    0 when no partition carries replicas)."""
    from kgov import broker

    class TPart:
        def __init__(self, replicas):
            self.replicas = replicas

    class TMeta:
        def __init__(self, error, partitions):
            self.error = error
            self.partitions = partitions

    md = {
        "ok": TMeta(None, {0: TPart(["r1", "r2"]), 1: TPart(None)}),
        "no_rep": TMeta(None, {}),
        "bad": TMeta(object(), {0: TPart(["r1"])}),
    }
    seen = {}

    class FakeAdminClient:
        def __init__(self, conf):
            seen["conf"] = conf

        def list_topics(self, timeout: float = 10.0):
            seen["timeout"] = timeout
            return type("MD", (), {"topics": md})()

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    out = broker.list_topics("127.0.0.1:9", timeout=3.0)

    assert [t["name"] for t in out] == ["no_rep", "ok"]  # "bad": error-skipped
    ok = next(t for t in out if t["name"] == "ok")
    assert ok["partitions"] == 2 and ok["replication_factor"] == 2
    nr = next(t for t in out if t["name"] == "no_rep")
    assert nr["partitions"] == 0 and nr["replication_factor"] == 0
    assert seen["conf"]["bootstrap.servers"] == "127.0.0.1:9"
    assert seen["timeout"] == 3.0
    assert set(ok) == {
        "name",
        "partitions",
        "replication_factor",
        "owner",
        "tags",
        "latest_message_at",
    }


def test_consumer_groups_member_merging(monkeypatch):
    """consumer_groups success body describes only STABLE/EMPTY groups and
    aggregates member assignments per topic; broken member assignment lists
    degrade to [] instead of aborting the scan."""
    from kgov import broker

    class TPart:
        def __init__(self, topic):
            self.topic = topic

    class Member:
        def __init__(self, assignment, broken=False):
            self._assignment = assignment
            self._broken = broken

        @property
        def assignment(self):
            if self._broken:
                raise RuntimeError("no assignment")
            return self._assignment

    class Desc:
        def __init__(self, group_id, members):
            self.group_id = group_id
            self.members = members

    class Group:
        def __init__(self, group_id, state):
            self.group_id = group_id
            self.state = state

    described = []

    class FakeAdminClient:
        def __init__(self, conf):
            self.conf = conf

        def list_consumer_groups(self, timeout: float = 10.0):
            return [
                Group("g1", "STABLE"),
                Group("g2", "EMPTY"),
                Group("g3", "DEAD"),
            ]

        def describe_consumer_groups(self, ids, timeout: float = 10.0):
            described.extend(ids)
            return [
                Desc(
                    "g1",
                    [
                        Member([TPart("t1"), TPart("t2")]),
                        Member([TPart("t1")]),
                    ],
                ),
                Desc("g2", [Member(None, broken=True)]),
            ]

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    out = broker.consumer_groups("127.0.0.1:9", timeout=2.0)

    assert sorted(described) == ["g1", "g2"]  # DEAD g3 filtered out
    assert set(out) == {"t1", "t2"}
    assert out["t1"] == {"group": "g1", "consumer_count": 2, "last_consumed_at": None}
    assert out["t2"] == {"group": "g1", "consumer_count": 2, "last_consumed_at": None}


def test_consumer_groups_fail_closed_paths(monkeypatch):
    """consumer_groups degradation paths each degrade to {}:
    list failure, no STABLE/EMPTY groups, and describe failure."""
    from kgov import broker

    class Group:
        def __init__(self, group_id, state):
            self.group_id = group_id
            self.state = state

    mode = "list_raises"
    groups = []

    class FakeAdminClient:
        def __init__(self, conf):
            self.conf = conf

        def list_consumer_groups(self, timeout: float = 10.0):
            if mode == "list_raises":
                raise RuntimeError("no metadata")
            return groups

        def describe_consumer_groups(self, ids, timeout: float = 10.0):
            raise RuntimeError("no describe")

    for mode in ("list_raises", "no_valid", "describe_raises"):
        groups = [Group("g1", "DEAD" if mode == "no_valid" else "STABLE")]
        monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
        assert broker.consumer_groups("127.0.0.1:9", timeout=1.0) == {}


def test_read_topic_poll_success(monkeypatch):
    """read_topic success body pulls messages until n is reached, decoding
    JSON values and hex keys, and always closes the consumer."""
    from kgov import broker
    from confluent_kafka import KafkaError

    class Msg:
        def __init__(self, key=None, value=None, error=None, timestamp=None,
                     headers=None, topic="payments", partition=0, offset=0):
            self._key = key
            self._value = value
            self._err = error
            self._ts = timestamp
            self._headers = headers or []
            self._topic = topic
            self._part = partition
            self._off = offset

        def error(self):
            return self._err

        def timestamp(self):
            return self._ts

        def topic(self):
            return self._topic

        def partition(self):
            return self._part

        def offset(self):
            return self._off

        def key(self):
            return self._key

        def value(self):
            return self._value

        def headers(self):
            return self._headers

    queue = [
        None,  # poll returning None -> continue
        Msg(error=KafkaError(KafkaError._PARTITION_EOF)),  # EOF -> continue
        Msg(key=b"k1", value=b'{"amount_cents": 100}',
            timestamp=(1, 1234567), headers=[("h1", b"v1")],
            topic="payments", partition=0, offset=41),
        Msg(key=b"\xff\xfe", value=b"plain", topic="payments",
            partition=1, offset=42),
    ] + [None] * 100  # exhausted queue must not hang: n-limit breaks first
    closed = []
    confs = []

    class FakeAdminClient:
        def __init__(self, conf):
            self.conf = conf

        def list_topics(self, timeout: float = 10.0):
            return type("MD", (), {"topics": {}})()

    class FakeConsumer:
        def __init__(self, conf):
            confs.append(conf)

        def subscribe(self, topics):
            self.topics = topics

        def poll(self, timeout: float = 5.0):
            return queue.pop(0) if queue else None

        def close(self):
            closed.append(True)

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    monkeypatch.setattr("confluent_kafka.Consumer", FakeConsumer)
    monkeypatch.setattr(broker, "_now_ms", lambda: 1000.0)  # deadline stays ahead

    out = broker.read_topic("127.0.0.1:9", "payments", n=2, timeout=1.0)

    assert closed  # finally closed the consumer
    assert confs[0]["bootstrap.servers"] == "127.0.0.1:9"
    assert confs[0]["group.id"].startswith("kgov-scan-")
    assert confs[0]["auto.offset.reset"] == "earliest"
    assert confs[0]["enable.auto.commit"] is False
    assert len(out) == 2
    assert out[0].topic == "payments" and out[0].partition == 0 and out[0].offset == 41
    assert out[0].key == "k1" and out[0].value == {"amount_cents": 100}
    assert out[0].timestamp_ms == 1234567 and out[0].headers == {"h1": b"v1"}
    assert out[1].key == {"$bytes": "fffe"} and out[1].value == "plain"


def test_read_topic_transport_error_raises(monkeypatch):
    """A live _TRANSPORT poll error raises KafkaException (the designed wrap),
    and the consumer still closes via finally."""
    from kgov import broker
    from confluent_kafka import KafkaError, KafkaException

    class Msg:
        def error(self):
            return KafkaError(KafkaError._TRANSPORT)

    queue = [Msg()]
    closed = []

    class FakeAdminClient:
        def __init__(self, conf):
            self.conf = conf

        def list_topics(self, timeout: float = 10.0):
            return type("MD", (), {"topics": {}})()

    class FakeConsumer:
        def __init__(self, conf):
            self.conf = conf

        def subscribe(self, topics):
            self.topics = topics

        def poll(self, timeout: float = 5.0):
            return queue.pop(0) if queue else None

        def close(self):
            closed.append(True)

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    monkeypatch.setattr("confluent_kafka.Consumer", FakeConsumer)
    monkeypatch.setattr(broker, "_now_ms", lambda: 1000.0)

    with pytest.raises(KafkaException):
        broker.read_topic("127.0.0.1:9", "payments", n=1, timeout=1.0)
    assert closed


def test_read_topic_other_error_skips_message(monkeypatch):
    """A poll error that is neither EOF nor TRANSPORT is skipped (continue)
    rather than aborting the scan."""
    from kgov import broker
    from confluent_kafka import KafkaError

    class Msg:
        def __init__(self, error=None, key=None, value=None):
            self._err = error
            self._key = key
            self._value = value

        def error(self):
            return self._err

        def timestamp(self):
            return (1, 1000)

        def topic(self):
            return "payments"

        def partition(self):
            return 0

        def offset(self):
            return 0

        def key(self):
            return self._key

        def value(self):
            return self._value

        def headers(self):
            return []

    # first message carries an unrelated error code (skipped), then a good one.
    queue = [Msg(error=KafkaError(KafkaError._MSG_TIMED_OUT)), Msg(value=b"1")]
    closed = []

    class FakeAdminClient:
        def __init__(self, conf):
            self.conf = conf

        def list_topics(self, timeout: float = 10.0):
            return type("MD", (), {"topics": {}})()

    class FakeConsumer:
        def __init__(self, conf):
            self.conf = conf

        def subscribe(self, topics):
            self.topics = topics

        def poll(self, timeout: float = 5.0):
            return queue.pop(0) if queue else None

        def close(self):
            closed.append(True)

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    monkeypatch.setattr("confluent_kafka.Consumer", FakeConsumer)
    monkeypatch.setattr(broker, "_now_ms", lambda: 1000.0)

    out = broker.read_topic("127.0.0.1:9", "payments", n=1, timeout=1.0)
    assert len(out) == 1 and out[0].value == 1  # decoded JSON "1"; skipped error msg
    assert closed


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
