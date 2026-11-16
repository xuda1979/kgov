"""Real Kafka broker client (optional, P3).

Gated behind the ``broker`` extra — imports ``confluent_kafka`` lazily so the
core package stays usable offline without it. Provides three read paths that
mirror the fixture inputs used by the rest of kgov:

- ``list_topics``            → topic metadata (partitions, replication factor)
- ``consumer_groups``        → per-topic consumer-group state
- ``read_topic``             → messages (key/value/partition/offset) for rule checks

Message values are decoded as JSON records first; when that fails and a
``SchemaRegistryClient`` is supplied, values in the Confluent wire format
(``0x00`` magic byte + big-endian schema id + Avro binary payload) are decoded
with the registered Avro schema. Any residual bytes fall back to raw text /
hex so the scan never aborts on an opaque payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class BrokerMessage:
    """A raw message read from a real broker (confluent_kafka consumer)."""

    topic: str
    partition: int
    offset: int
    key: Any
    value: Any
    timestamp_ms: Optional[int] = None
    headers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "partition": self.partition,
            "offset": self.offset,
            "key": _json_safe(self.key),
            "value": self.value,
            "timestamp": self.timestamp_ms,
            "headers": self.headers,
        }


def _json_safe(v: Any) -> Any:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return {"$bytes": v.hex()}
    return v


class _UNDECODED:
    """Sentinel: raw bytes did not look like any supported encoding."""


def _decode_value(
    raw: Optional[bytes],
    decoder: Optional[Callable[[bytes], Any]] = None,
) -> Any:
    """Decode a Kafka record value: JSON object preferred, then Avro wire
    format (if a ``decoder`` is supplied), raw text / hex as a last resort."""
    if raw is None:
        return None
    import json

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if decoder is not None:
        try:
            decoded = decoder(raw)
        except Exception:
            # a misbehaving decoder must not abort the scan — fall through to
            # the text / hex fallback below (the wire decoder is already
            # defensive; this covers any caller-supplied decoder)
            decoded = _UNDECODED
        if decoded is not _UNDECODED:
            return decoded
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"$bytes": raw.hex()}


CONFLUENT_MAGIC = b"\x00"
CONFLUENT_ID_BYTES = 4
CONFLUENT_HEADER = 5  # magic byte + 4-byte big-endian schema id


class AvroRecordDecoder:
    """Decode Confluent-wire Avro payloads via a Schema Registry.

    Wire layout is ``0x00`` + 4-byte big-endian schema id + Avro binary datum.
    The schema is fetched from the registry (cached by id) and used to read the
    datum. Bytes that do not match the wire format, schemas that are not
    records, or malformed datums all return :data:`_UNDECODED` so the caller
    falls through to its text / hex fallback.
    """

    def __init__(
        self,
        client: "SchemaRegistryClient",
        decoder_factory: Optional[Callable] = None,
    ):
        self._client = client
        self._schemas: dict[int, Any] = {}
        self._decoder_factory = decoder_factory or _default_decoder_factory

    def __call__(self, raw: bytes) -> Any:
        if len(raw) <= CONFLUENT_HEADER or not raw.startswith(CONFLUENT_MAGIC):
            return _UNDECODED
        schema_id = int.from_bytes(raw[1:CONFLUENT_HEADER], byteorder="big")
        try:
            reader = self._reader_for(schema_id)
        except Exception:
            return _UNDECODED
        if reader is None:
            return _UNDECODED
        try:
            return self._decoder_factory(reader, raw[CONFLUENT_HEADER:])
        except Exception:
            return _UNDECODED

    def _reader_for(self, schema_id: int) -> Optional["_RecordReader"]:
        if schema_id not in self._schemas:
            info = self._client.get_schema_by_id(schema_id)
            parsed = info["schema"]
            if not isinstance(parsed, dict) or parsed.get("type") != "record":
                self._schemas[schema_id] = None
                return None
            self._schemas[schema_id] = parsed
        parsed = self._schemas.get(schema_id)
        if parsed is None:
            return None
        import json as _json

        from avro import schema as avro_schema

        return _RecordReader(avro_schema.parse(_json.dumps(parsed, ensure_ascii=False)))


class _RecordReader:
    def __init__(self, schema: Any):
        self._schema = schema

    def read(self, payload: bytes) -> Any:
        import io

        from avro import io as avro_io

        return avro_io.DatumReader(self._schema).read(
            avro_io.BinaryDecoder(io.BytesIO(payload))
        )


def _default_decoder_factory(reader: "_RecordReader", payload: bytes) -> Any:
    return reader.read(payload)


class NeedsBrokerError(ImportError):
    """Raised when a broker feature is used without confluent-kafka installed."""


class BrokerConnectError(RuntimeError):
    """Raised when the bootstrap cluster cannot be reached at all.

    A scan against a dead broker yields no messages; failing fast (instead of
    returning an empty list) keeps a ``check`` gate from passing vacuously.
    """


def _require_kafka() -> None:
    try:
        import confluent_kafka  # noqa: F401
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise NeedsBrokerError(
            "confluent-kafka is required for broker features; "
            "install with `pip install 'kgov[broker]'`"
        ) from exc


def list_topics(bootstrap: str, timeout: float = 10.0) -> list[dict]:
    """Return topic metadata: [{name, partitions, replication_factor, owner}]."""
    _require_kafka()
    from confluent_kafka.admin import AdminClient

    admin = AdminClient(
        {"bootstrap.servers": bootstrap, "log_level": 0, "socket.timeout.ms": int(timeout * 1000)}
    )
    md = admin.list_topics(timeout=timeout)
    out: list[dict] = []
    for name, tmeta in sorted(md.topics.items()):
        if tmeta.error is not None:
            continue
        partitions = len(tmeta.partitions)
        rf = max(
            (
                len(p.replicas)
                for p in tmeta.partitions.values()
                if p.replicas is not None
            ),
            default=0,
        )
        out.append(
            {
                "name": name,
                "partitions": partitions,
                "replication_factor": rf,
                "owner": "",
                "tags": [],
                "latest_message_at": None,
            }
        )
    return out


def consumer_groups(bootstrap: str, timeout: float = 10.0) -> dict[str, dict]:
    """Per-topic consumer-group membership, mirroring the offline fixture shape.

    Returns ``{topic: {"group": ..., "consumer_count": ..., "last_consumed_at": None}}``.
    Brokers do not expose commit timestamps through the admin API, so
    ``last_consumed_at`` stays None and the report layer falls back to the
    producer-grace staleness rule.
    """
    _require_kafka()
    from confluent_kafka.admin import AdminClient

    admin = AdminClient({"bootstrap.servers": bootstrap})
    out: dict[str, dict] = {}
    try:
        groups = admin.list_consumer_groups(timeout=timeout)
    except Exception:
        return out

    valid = [g for g in groups if g.state in ("STABLE", "EMPTY")]
    if not valid:
        return out

    try:
        descs = admin.describe_consumer_groups([g.group_id for g in valid], timeout=timeout)
    except Exception:
        return out

    for desc in descs:
        members = list(getattr(desc, "members", None) or [])
        consumers = sum(1 for _ in members)
        topic_members: dict[str, int] = {}
        for member in members:
            try:
                assignment = list(member.assignment or [])
            except Exception:
                assignment = []
            for tpart in assignment:
                topic = getattr(tpart, "topic", None)
                if topic:
                    topic_members[topic] = topic_members.get(topic, 0) + 1
        for topic, count in topic_members.items():
            out.setdefault(topic, {
                "group": desc.group_id,
                "consumer_count": max(count, consumers),
                "last_consumed_at": None,
            })
    return out


def read_topic(
    bootstrap: str,
    topic: str,
    *,
    group_id: str = "kgov-scan",
    n: Optional[int] = None,
    timeout: float = 10.0,
    value_decoder: Optional[Callable[[bytes], Any]] = None,
) -> list[BrokerMessage]:
    """Read up to ``n`` messages from a topic (oldest first).

    A fresh consumer with ``auto.offset.reset=earliest`` runs under a unique
    group id per call so concurrent scans do not collide on commits. Messages
    are never committed. If ``value_decoder`` is supplied it is consulted for
    values that are not valid JSON (e.g. an :class:`AvroRecordDecoder`).
    """
    _require_kafka()
    from confluent_kafka import KafkaError, KafkaException

    # Fail fast when the cluster is unreachable: a scan of a dead broker
    # yields no messages, which would otherwise pass a check gate vacuously.
    _probe_bootstrap(bootstrap, timeout)

    unique_group = f"{group_id}-{id(object()):x}"
    conf = {
        "bootstrap.servers": bootstrap,
        "group.id": unique_group,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    }
    consumer = Consumer(conf)
    out: list[BrokerMessage] = []
    try:
        consumer.subscribe([topic])
        deadline = _now_ms() + timeout * 1000
        while _now_ms() < deadline:
            msg = consumer.poll(timeout=5.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                if msg.error().code() == KafkaError._TRANSPORT:
                    raise KafkaException(msg.error())
                continue
            ts = msg.timestamp()
            out.append(
                BrokerMessage(
                    topic=msg.topic(),
                    partition=msg.partition(),
                    offset=msg.offset(),
                    key=_json_safe(msg.key()),
                    value=_decode_value(msg.value(), value_decoder),
                    timestamp_ms=ts[1] if ts else None,
                    headers=dict(msg.headers() or []),
                )
            )
            if n is not None and len(out) >= n:
                break
    finally:
        consumer.close()
    return out


def _probe_bootstrap(bootstrap: str, timeout: float) -> None:
    """Raise :class:`BrokerConnectError` when the cluster is unreachable.

    Uses the admin client's metadata call because it propagates the
    ``_TRANSPORT`` failure deterministically on an unreachable cluster while
    returning (with metadata) against a reachable cluster even when the topic
    itself does not exist. A scan of a dead broker otherwise yields no
    messages and a ``check`` gate would pass vacuously.
    """
    from confluent_kafka.admin import AdminClient

    admin = AdminClient(
        {"bootstrap.servers": bootstrap, "log_level": 0, "socket.timeout.ms": int(timeout * 1000)}
    )
    try:
        admin.list_topics(timeout=timeout)
    except Exception as exc:
        raise BrokerConnectError(f"cannot reach broker {bootstrap!r}: {exc}") from exc


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
