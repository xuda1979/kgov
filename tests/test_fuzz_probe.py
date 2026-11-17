"""Junk-tolerance regression battery: direct-probe kgov public helpers.

Every helper is pumped with junk shapes (None / scalars / containers /
objects).  The suite fails iff a genuine crash escapes kgov's designed
contracts: an exception whose type is not a designed error, or whose last
traceback frame is outside a ``kgov`` module.  Designed contracts are:

* typed errors (CliError, SchemaParseError, RulesConfigError, RegistryError,
  MockRegistryError, NeedsBrokerError, BrokerConnectError,
  UnknownFieldTypeError, RuleFieldConfigError), and
* plain ``ValueError`` raised from a fail-closed gate inside a kgov module.

The probes are network-free: registry client probes use the fast-refused
``http://127.0.0.1:1`` base or the in-memory mock store accessors only.
"""

from __future__ import annotations

import dataclasses

import pytest

from kgov.broker import BrokerConnectError, NeedsBrokerError
from kgov.cli import CliError
from kgov.mock_registry import MockRegistryError

JUICE = [
    None, 0, 1, -1, 3.14, float("inf"), float("nan"), True, False, "",
    "abc", "1", b"", b"abc", b"\x00\x01", [], [1], [{}], {}, {"a": 1},
    {"x": {"y": 2}}, set(), {1, 2}, (1, 2), (), type(None), object(),
]

# Designed typed errors that are *not* ValueError subclasses.  ValueError
# subclasses (SchemaParseError, RulesConfigError/RuleFieldConfigError/
# UnknownFieldTypeError, RegistryError) are covered by the ValueError arm.
DESIGNED_ERROR_TYPES = (
    CliError,
    NeedsBrokerError,  # ImportError
    BrokerConnectError,  # RuntimeError
    MockRegistryError,  # RuntimeError
)


def _is_designed(exc: BaseException) -> bool:
    """True when ``exc`` is a designed contract escape: a plain ValueError or
    a known typed error whose last traceback frame lives inside a kgov module.

    Everything else (an AttributeError, a stdlib-framed ValueError, an
    arbitrary RuntimeError ...) is a genuine crash candidate.
    """
    tb = exc.__traceback__
    last = tb
    while last.tb_next is not None:
        last = last.tb_next
    fname = last.tb_frame.f_code.co_filename
    return ("kgov" in fname) and isinstance(
        exc, (ValueError,) + DESIGNED_ERROR_TYPES
    )


def _last_frame_fname(exc: BaseException) -> str:
    tb = exc.__traceback__
    last = tb
    while last.tb_next is not None:
        last = last.tb_next
    return last.tb_frame.f_code.co_filename


def _probe(genuine, name, fn, *args):
    """Run fn(*args); record a genuine crash if an exception escapes kgov.

    ``genuine`` is a dict keyed by (exc_type_name, last_frame_fname)
    collecting the probe names that hit it.
    """
    try:
        fn(*args)
        return
    except Exception as exc:  # noqa: BLE001 - the point is to catch everything
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            return
        if _is_designed(exc):
            return
        genuine.setdefault(
            (type(exc).__name__, _last_frame_fname(exc).split("/")[-1]), []
        ).append(name)


def _namespace_junk():
    """A Namespace-like object whose options are all the ``j`` junk shape."""
    n = {}
    for key in (
        "before", "after", "format", "output", "json", "markdown", "subject",
        "before_version", "after_version", "before_id", "after_id", "config",
        "messages", "broker", "topic", "schema_registry", "max_violations",
        "gate", "registry_url", "topics", "registry_path", "groups", "rules",
        "violations", "format_out", "type", "path", "url", "version",
        "base_url", "port", "fixture", "subject_name", "group", "partition",
        "offset", "n", "timeout", "value", "bootstrap", "owner", "tags",
    ):
        for j in JUICE:
            n[key] = j
    return type("NS", (), n)


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


def test_model_probes():
    import kgov.model as m

    genuine = {}
    for j in JUICE:
        _probe(genuine, "model.json_default", m.json_default, j)
        _probe(genuine, "model._json_safe", m._json_safe, j)
        _probe(genuine, "model._json_safe_key", m._json_safe_key, j)
    _probe(genuine, "model.now_utc_iso", m.now_utc_iso)
    for kind in ("Breaking", "Non-Breaking", "Additive", 1, None, b"x", [], {}, set()):
        for p in ("a.b", 1, None, b"x", [], {}):
            for msg in ("hi", 1, None, b"x", [], {}):
                _probe(genuine, "model.DiffResult.to_dict",
                       m.DiffResult(kind=kind, path=p, message=msg).to_dict)
    rep = m.SchemaDiffReport(format="avro", subject="s", before_version=1,
                             after_version=2, kind="Breaking")
    for r in (None, 1, "x", {}, [1, 2], [{"kind": 1}],
              [{"kind": "Breaking"}],
              [{"kind": "Breaking", "path": b"x", "message": 1}]):
        rep.results = r
        _probe(genuine, "model.SchemaDiffReport.to_dict", rep.to_dict)
        _probe(genuine, "model.SchemaDiffReport.to_markdown", rep.to_markdown)
    for subj in (None, 1, b"x", [], {}):
        r2 = m.SchemaDiffReport(format=None, subject=subj, before_version=None,
                                after_version=None, before_id=b"x",
                                after_id=[], kind=None, results="junk",
                                generated_at=None)
        _probe(genuine, "model.SchemaDiffReport.to_dict.junkfields", r2.to_dict)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


# ---------------------------------------------------------------------------
# schema_diff
# ---------------------------------------------------------------------------


def test_schema_diff_probes():
    import kgov.schema_diff as sd

    genuine = {}
    for r in (None, 1, "x", {}, [1, 2], {"a": 1}, [{}], [{"kind": 1}],
              [{"kind": "Breaking", "path": 1, "message": 1}], (1, 2), set()):
        _probe(genuine, "schema_diff.classify", sd.classify, r)
    for old in ("{}", {}, None, 1, [], b"{}",
                '{"type": "record", "name": "R", "fields": []}'):
        for new in ("{}", {}, None, 1, [], b"{}", "not json"):
            _probe(genuine, "schema_diff.diff_schemas", sd.diff_schemas, old, new)
    for text in ("{}", {}, None, 1, [], b"{}",
                 '{"type": "record", "name": "R"}', "not json", "", "\x00",
                 set(), ()):
        for fmt in (None, "avro", "json", 1, "bogus", b"avro"):
            _probe(genuine, "schema_diff.parse_schema", sd.parse_schema, text, fmt)
    for junk in (None, 1, "x", [], [1], {}, {"type": "record"}, b"{}", set()):
        _probe(genuine, "schema_diff.diff_avro", sd.diff_avro, junk, junk)
        _probe(genuine, "schema_diff.diff_json_schema",
               sd.diff_json_schema, junk, junk)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_schema_diff_internals_probes():
    """Internal helpers (never directly fuzzed by the top-level battery) must
    fail closed on junk: _printable/_desc/_name_of/_is_nullable/_type_names/
    _types_assignable/_join/_types_assignable_set/_js_*/parse_schema_text, and
    the _avro_diff/_js_diff walkers fed pathological-but-structured records
    (junk union members, dict-size fixed, nested record maps/arrays, NaN
    bounds, junk enums/required lists)."""
    import kgov.schema_diff as sd

    genuine = {}
    for j in JUICE:
        _probe(genuine, "sd._printable", sd._printable, j)
        _probe(genuine, "sd._desc", sd._desc, j)
        _probe(genuine, "sd._name_of", sd._name_of, j)
        _probe(genuine, "sd._type_names", sd._type_names, j)
        _probe(genuine, "sd._types_assignable", sd._types_assignable, j, j)
        _probe(genuine, "sd._is_nullable", sd._is_nullable, j)
        _probe(genuine, "sd._join", sd._join, j, "child")
        _probe(genuine, "sd._join.p", sd._join, "parent", j)
        _probe(genuine, "sd._types_assignable_set", sd._types_assignable_set, j, j)
        _probe(genuine, "sd._js_string_types", sd._js_string_types, j)
        _probe(genuine, "sd._js_scalar_rank", sd._js_scalar_rank, j)
        _probe(genuine, "sd._js_strings_assignable", sd._js_strings_assignable, j, j)
        _probe(genuine, "sd.parse_schema_text", sd.parse_schema_text, j, "avro")
        _probe(genuine, "sd.parse_schema_text.fmt", sd.parse_schema_text, "{}", j)
    # _avro_diff with pathological-but-structurally-valid records
    fld_union = [{"name": "u", "type": ["null", {"type": "record",
                 "name": "R2", "fields": [{"name": "x", "type": "int"}]}]}]
    junk_records = [
        {"type": "record", "name": "R",
         "fields": [{"name": "a", "type": [None, {}, [], "null", {"x": 1},
                    {"type": "record", "name": "N", "fields": [
                        {"name": "n", "type": {"type": "enum",
                         "name": "E", "symbols": [1, None, {}, "A"]}}]}]}]},
        {"type": "record", "name": "R", "fields": [{"name": "f",
         "type": {"type": "fixed", "name": "F", "size": {"x": 1}}}]},
        {"type": "record", "name": "R", "fields": [{"name": "m",
         "type": {"type": "map", "values": {"type": "record", "name": "M",
         "fields": []}}}], "order": "ignored"},
        {"type": "record", "name": "R", "fields": [
            {"name": "ar", "type": {"type": "array",
             "items": {"type": "record", "name": "A",
                       "fields": [{"name": "b", "type": "long"}]}}}]},
    ]
    for ra in junk_records:
        _probe(genuine, "sd._avro_diff.junkR", sd._avro_diff, ra, ra, "", [])
    _probe(genuine, "sd._avro_diff.union", sd._avro_diff,
           {"type": "record", "name": "R", "fields": fld_union},
           {"type": "record", "name": "R", "fields": list(reversed(fld_union))},
           "", [])
    for j in JUICE:
        _probe(genuine, "sd._avro_diff.junk", sd._avro_diff, j, j, "", [])
        _probe(genuine, "sd._js_diff.junk", sd._js_diff, j, j, "", [])
    # _js_diff with numeric NaN/inf bounds + junk enum/props
    js_recs = [
        ({"type": "object", "properties": {"p": {"type": "number",
           "minimum": float("nan"), "maximum": float("inf")}}},
         {"type": "object", "properties": {"p": {"type": "number",
           "minimum": 1, "maximum": 2}}}),
        ({"type": "object", "enum": [b"x", {1: 2}]},
         {"type": "object", "enum": [b"x"]}),
        ({"type": "array", "items": {"type": "object",
           "required": [1, None, "a"]}},
         {"type": "array", "items": {"type": "object", "required": []}}),
    ]
    for a, b in js_recs:
        _probe(genuine, "sd._js_diff.recs", sd._js_diff, a, b, "", [])
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_probes():
    from kgov.registry import (
        SchemaRegistryClient,
        _parse_schema_string,
        _quote_path,
    )

    genuine = {}
    for s in JUICE:
        _probe(genuine, "registry._parse_schema_string",
               _parse_schema_string, s)
        _probe(genuine, "registry._quote_path", _quote_path, s)
    # junk base_url must not leak a raw ValueError from urllib URL building;
    # junk timeouts must not leak raw TypeError/OverflowError.  http://127.0.0.1:1
    # is fast-refused, so valid-arg cases raise only the designed RegistryError.
    for b in (None, 1, [], {}, b"x", "", "http://x", set()):
        c = SchemaRegistryClient(b)
        _probe(genuine, "registry.SchemaRegistryClient._get.junk_base", c._get,
               "/subjects")
    for t in (None, 1, "1", [], {}, float("inf"), 0, -1.0, True, "x", "abc",
              {}, [], 1.5, b"x", (1, 2), 1e300, float("nan"), -5):
        c = SchemaRegistryClient("http://127.0.0.1:1", timeout=t)
        _probe(genuine, "registry.SchemaRegistryClient.list_subjects.junk_timeout",
               c.list_subjects)
    # junk schema ids must fail closed (int() raises OverflowError for
    # out-of-range floats like inf/1e300) and never touch the network until
    # the id has actually converted.
    for sid in (None, 1, 0, -3, b"7", "7", 3.14, float("inf"), float("nan"),
                1e300, [], {}, set(), (1, 2), object()):
        c = SchemaRegistryClient("http://127.0.0.1:1", timeout=0.1)
        _probe(genuine, "registry.get_schema_by_id.junk",
               c.get_schema_by_id, sid)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def test_rules_probes():
    import kgov.rules as rl

    RC = dataclasses.make_dataclass(
        "RC", [("topic", object, dataclasses.field(default="t")),
               ("name", object, dataclasses.field(default="r")),
               ("fields", object, dataclasses.field(default_factory=dict))])
    RU = dataclasses.make_dataclass(
        "RU", [("name", object, dataclasses.field(default="f")),
               ("type", object, dataclasses.field(default=None)),
               ("required", object, dataclasses.field(default=False)),
               ("non_null", object, dataclasses.field(default=False)),
               ("enum", object, dataclasses.field(default=None)),
               ("pattern", object, dataclasses.field(default=None)),
               ("range", object, dataclasses.field(default=None))])
    MC = dataclasses.make_dataclass(
        "MC", [("topic", object, dataclasses.field(default="t")),
               ("partition", object, dataclasses.field(default=0)),
               ("offset", object, dataclasses.field(default=0)),
               ("key", object, dataclasses.field(default=None)),
               ("value", object, dataclasses.field(default_factory=dict))])

    genuine = {}
    for j in JUICE:
        _probe(genuine, "rules.Violation.to_dict",
               rl.Violation(rule="r", topic="t", partition=j, offset=j, key=j,
                            field="f", check="c", message="m").to_dict)
        _probe(genuine, "rules.Message.to_dict",
               rl.Message(topic="t", partition=j, offset=j, key=j,
                          value=j).to_dict)
        _probe(genuine, "rules.build_ruleset.junk",
               rl.build_ruleset, j)
        _probe(genuine, "rules.build_ruleset.junk_rules",
               rl.build_ruleset, {"version": 1, "rules": j})
        _probe(genuine, "rules.load_rules_yaml.junk",
               rl.load_rules_yaml, j)
        rs = RC(topic=j, name=j, fields=j)
        msg = MC(topic=j, partition=j, offset=j, key=j, value=j)
        _probe(genuine, "rules.validate_message.junk_rs_msg",
               rl.validate_message, rs, msg)
        for en in (None, 1, "x", [], [1], {}, (1, 2), "ab", b"ab", set()):
            fc = RU(type=j, required=j, non_null=j, enum=en, pattern=j, range=j)
            rs2 = RC(topic="t", name="r", fields={"f": fc})
            msg2 = MC(topic="t", partition=0, offset=0, key=None, value={"f": j})
            _probe(genuine, "rules.validate_message.junk_fieldcheck",
                   rl.validate_message, rs2, msg2)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_rules_internals_probes():
    """Internal rules helpers (never directly fuzzed by the top-level battery)
    must fail closed on junk: _merge_rule/_merge_fields/_validate_type/
    _validate_field_spec/_type_ok/_type_repr/_json_safe_key, and the
    file-driven validate_fixture_file / iter_messages_from_json."""
    import kgov.rules as rl

    genuine = {}
    for j in JUICE:
        _probe(genuine, "rules._merge_rule", rl._merge_rule, rl.RuleSet(), j)
        _probe(genuine, "rules._merge_fields", rl._merge_fields,
               rl.RuleSet(), j, "r")
        _probe(genuine, "rules._validate_type", rl._validate_type, "f", j, "r")
        _probe(genuine, "rules._validate_field_spec", rl._validate_field_spec,
               "f", j, "r")
        _probe(genuine, "rules._type_ok", rl._type_ok, "int", j)
        _probe(genuine, "rules._type_repr", rl._type_repr, j)
        _probe(genuine, "rules._json_safe_key", rl._json_safe_key, j)
        _probe(genuine, "rules.validate_fixture_file",
               rl.validate_fixture_file, rl.RuleSet(), j)
        _probe(genuine, "rules.iter_messages_from_json",
               lambda: list(rl.iter_messages_from_json(j)))
        _probe(genuine, "rules.load_rules_file.junk",
               rl.load_rules_file, j)
    # deeply nested YAML blows PyYAML's recursive scanner with a raw
    # RecursionError; load_rules_yaml must surface the designed parse error
    for text in ("[" * 20000 + "x", '{"a": ' * 5000 + "1" + "}" * 5000):
        _probe(genuine, "rules.load_rules_yaml.deep_nesting",
               rl.load_rules_yaml, text)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_report_probes():
    import kgov.report as rp

    genuine = {}
    for j in JUICE:
        _probe(genuine, "report.build_topic_state.junk_now",
               rp.build_topic_state, {"name": "t"}, {}, {}, {}, j)
        try:
            g = rp.StalenessSettings(consumer_grace_seconds=j,
                                     producer_grace_seconds=j,
                                     no_consumer_since_epoch=j)
            _probe(genuine, "report.build_topic_state.junk_settings",
                   rp.build_topic_state,
                   {"name": "t",
                    "latest_message_at": "2020-01-01T00:00:00Z"},
                   {}, {}, {}, None, g)
        except Exception as exc:  # noqa: BLE001
            # StalenessSettings itself raising is designed only when it is a
            # designed contract type raised from kgov; otherwise it is a
            # genuine crash to record.
            tb = exc.__traceback__
            last = tb
            while last.tb_next is not None:
                last = last.tb_next
            fname = last.tb_frame.f_code.co_filename
            if not ("kgov" in fname and isinstance(exc, ValueError)):
                genuine.setdefault(
                    (type(exc).__name__, fname.split("/")[-1]),
                    []).append("report.StalenessSettings")
        _probe(genuine, "report.build_governance_report.junk",
               rp.build_governance_report, j)
        _probe(genuine, "report.report_to_csv.junk", rp.report_to_csv, j)

    state = rp.TopicState(name="t", tags=[b"x", 1, "ok"], partitions=None,
                          schema_version=[], stale="yes",
                          violation_count=object())
    _probe(genuine, "report.TopicState.to_dict.junkfields", state.to_dict)
    rep = rp.GovernanceReport(topics=[state, 1, None, "x"])
    _probe(genuine, "report.GovernanceReport.to_dict", rep.to_dict)
    _probe(genuine, "report.GovernanceReport.to_json", rep.to_json)

    # junk `now` kwarg (positional-only probe helpers cannot pass kwargs that
    # aren't fn args; use a lambda)
    _probe(genuine, "report.build_governance_report.junk_now",
           lambda: rp.build_governance_report({}, now=object()))

    for j in JUICE + ["2020-01-01T00:00:00Z", "not-a-date", "123",
                      b"2020-01-01", "2020-13-01",
                      "2020-01-01T00:00:00+05:30"]:
        _probe(genuine, "report._parse_dt", rp._parse_dt, j)
        _probe(genuine, "report._read_json", rp._read_json, j)

    st = rp.TopicState(name="t", tags=None, partitions=None, schema_version=None,
                       stale=None, violation_count=None)
    _probe(genuine, "report.TopicState.to_dict.empty", st.to_dict)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


# ---------------------------------------------------------------------------
# broker
# ---------------------------------------------------------------------------


def test_broker_probes():
    import kgov.broker as bk

    genuine = {}
    for j in JUICE:
        _probe(genuine, "broker.BrokerMessage.to_dict",
               bk.BrokerMessage(topic=j, partition=j, offset=j, key=j,
                                value="x", timestamp_ms=j, headers=j).to_dict)
        _probe(genuine, "broker._json_safe", bk._json_safe, j)
        _probe(genuine, "broker._decode_value", bk._decode_value, j)
        _probe(genuine, "broker._require_bootstrap", bk._require_bootstrap, j)
        _probe(genuine, "broker._require_timeout", bk._require_timeout, j)
    dec = bk.AvroRecordDecoder(object())
    for j in JUICE:
        _probe(genuine, "broker.AvroRecordDecoder.__call__", dec, j)
    rr = bk._RecordReader(object())
    for j in JUICE:
        _probe(genuine, "broker._RecordReader.read", rr.read, j)
    # public broker entry points: junk args must fail closed at the pure-Python
    # gates (_require_bootstrap/_require_timeout/topic/group_id/n) before any
    # confluent-kafka import or live client.  bootstrap+timeout (and the other
    # args) always come from the same junk shape, so no pairing can pass every
    # gate and touch the network.
    for j in JUICE:
        _probe(genuine, "broker.list_topics.junk", bk.list_topics, j, j)
        _probe(genuine, "broker.consumer_groups.junk", bk.consumer_groups, j, j)
        _probe(genuine, "broker.read_topic.junk",
               lambda j=j: bk.read_topic(j, j, group_id=j, n=j, timeout=j))
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


# ---------------------------------------------------------------------------
# mock_registry
# ---------------------------------------------------------------------------


def test_mock_registry_probes():
    import kgov.mock_registry as mr

    genuine = {}
    mk = mr.MockRegistry()
    for j in JUICE:
        _probe(genuine, "mock_registry.MockRegistry.add_subject",
               mk.add_subject, j)
        _probe(genuine, "mock_registry.MockRegistry.versions",
               mk.versions, j)
        _probe(genuine, "mock_registry.MockRegistry.version", mk.version, j)
        _probe(genuine, "mock_registry.MockRegistry.compatibility_level",
               mk.compatibility_level, j)
        _probe(genuine, "mock_registry.MockRegistry.schema_by_id",
               mk.schema_by_id, j)
    for j in JUICE:
        _probe(genuine, "mock_registry.MockRegistry.set_fixture",
               mk.set_fixture, j)
        try:
            mr.MockRegistry(fixture=j)
        except Exception as exc:  # noqa: BLE001
            tb = exc.__traceback__
            last = tb
            while last.tb_next is not None:
                last = last.tb_next
            fname = last.tb_frame.f_code.co_filename
            if not ("kgov" in fname and isinstance(
                    exc, (ValueError, RuntimeError))):
                genuine.setdefault(
                    (type(exc).__name__, fname.split("/")[-1]), []).append(
                        "mock_registry.MockRegistry.__init__")

    # internals
    JUNK_VER = JUICE + [{"compatibility": {"x": 1}}, {"compatibility": []},
                        {"version": {"x": 1}}, {"id": b"7"}, "not-a-dict"]
    _probe(genuine, "mr._version_entries", mr._version_entries, JUNK_VER)
    for j in JUICE:
        _probe(genuine, "mr._version_entries.junk", mr._version_entries, j)
        _probe(genuine, "mr._wire_schema", mr._wire_schema, j)
        _probe(genuine, "mr._as_int", mr._as_int, j, -1)
    for s in (b"bytes", {1, 2}, 3.14, float("nan"), ["l"]):
        _probe(genuine, "mr._wire_schema.spec", mr._wire_schema, s)
    m = mr.MockRegistry()
    m.add_subject("s", {"id": 1, "version": 1, "compatibility": {"x": 1},
                        "schema": object()})
    for v in JUICE + ["latest", "1", 1, 1.0, True]:
        _probe(genuine, "mr.version.junk_arg", m.version, "s", v)
    for compat in ({"x": 1}, [], b"B", 3.14, None, "BACKWARD"):
        m2 = mr.MockRegistry()
        m2.add_subject("s", {"id": 1, "version": 1, "compatibility": compat})
        _probe(genuine, "mr.compatibility_level.junk_compat",
               m2.compatibility_level, "s")
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd_name",
    ["cmd_schema_diff", "cmd_check", "cmd_topics", "cmd_registry",
     "cmd_report", "cmd_export"],
)
def test_cli_probes(cmd_name, tmp_path, monkeypatch):
    import kgov.cli as c

    # truthy-string junk path values ("1", "abc") legitimately pass the output
    # gate and write files; keep that probe output in the temp dir
    monkeypatch.chdir(tmp_path)
    fn = getattr(c, cmd_name)
    genuine = {}
    for j in JUICE:
        _probe(genuine, f"cli.{cmd_name}.junk_all_none", fn, None)
        ns = type("NS", (), {k: j for k in (
            "before", "after", "format", "output", "json", "markdown",
            "subject", "before_version", "after_version", "before_id",
            "after_id", "config", "messages", "broker", "topic",
            "schema_registry", "max_violations", "gate", "registry_url",
            "topics", "registry_path", "groups", "rules", "violations",
            "format_out", "type", "path", "url", "version", "base_url",
            "port", "fixture", "subject_name", "group", "partition",
            "offset", "n", "timeout", "value", "bootstrap", "owner", "tags",
        )})
        _probe(genuine, f"cli.{cmd_name}.junk_namespace", fn, ns)
    _probe(genuine, f"cli.{cmd_name}.junk_namespace_all", fn, _namespace_junk())
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_cli_internals_probes(tmp_path, monkeypatch):
    """Internal cli helpers (never directly fuzzed by the cmd-handler battery)
    must fail closed on junk: _write_out (both stdout and file-write branches),
    _load_json_or_exit, _load_schema_text, _require_export_format.

    The working directory is moved to tmp_path so that truthy-string path
    values in JUICE ("1", "abc") that legitimately pass the ``if path:`` gate
    write their probe output into the temp dir, never the repo tree.
    """
    import kgov.cli as c

    monkeypatch.chdir(tmp_path)
    genuine = {}
    out = str(tmp_path / "out.txt")
    for j in JUICE:
        _probe(genuine, "cli._write_out.junk_path", c._write_out, j, "x")
        _probe(genuine, "cli._write_out.junk_text_nopath", c._write_out, None, j)
        _probe(genuine, "cli._write_out.truthy_path_junk_text",
               c._write_out, out, j)
        _probe(genuine, "cli._load_json_or_exit", c._load_json_or_exit, j)
        _probe(genuine, "cli._load_schema_text", c._load_schema_text, j, None)
        _probe(genuine, "cli._load_schema_text.fmt",
               c._load_schema_text, "{}", j)
        ns = type("A", (), {"format": j, "type": "report"})()
        _probe(genuine, "cli._require_export_format",
               c._require_export_format, ns, {"json", "csv"})
    _probe(genuine, "cli.build_parser", c.build_parser)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"
