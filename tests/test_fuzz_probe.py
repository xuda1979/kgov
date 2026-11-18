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
``http://127.0.0.1:1`` base or the in-memory mock store accessors only.  The
broker "live wire" probes contact only an unreachable localhost port
(``127.0.0.1:1``, TCP refused) under ``socket.timeout.ms``, so no external
network is ever touched.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from kgov.broker import BrokerConnectError, NeedsBrokerError
from kgov.cli import CliError
from kgov.mock_registry import MockRegistryError

JUICE = [
    None, 0, 1, -1, 3.14, float("inf"), float("nan"), True, False, "",
    "abc", "1", b"", b"abc", b"\x00\x01", [], [1], [{}], {}, {"a": 1},
    {"x": {"y": 2}}, set(), {1, 2}, (1, 2), (), type(None), object(),
    # Lone surrogates: json.dumps(ensure_ascii=False) passes them through, but
    # a later utf-8 encode (file write, stdout) raises UnicodeEncodeError.
    "\ud800", "\udbff\udfff",
]

# Self-referencing containers: json.dumps handles them with a designed
# ValueError ("Circular reference detected") but naive recursive coercers
# (model._json_safe) recurse forever and exit as a genuine RecursionError.
def _cyclic_list() -> list:
    cycle = []
    cycle.append(cycle)
    return cycle


def _cyclic_dict() -> dict:
    cycle = {}
    cycle["self"] = cycle
    return cycle


JUICE += [_cyclic_list(), _cyclic_dict()]

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
    # remaining public client methods with junk base_url + junk timeout: the
    # fast-refused base guarantees junk bases that survive url-building fail as
    # the designed RegistryError, never an unbounded DNS/connect wait.
    for b in (None, 1, [], {}, b"x", "", set(), float("inf"), 0, 3.14):
        for t in (None, 1, "x", 0, -1.0, float("inf"), float("nan"), 1e300):
            c = SchemaRegistryClient(b, timeout=t)
            _probe(genuine, "registry.get_versions.junk", c.get_versions, "s")
            _probe(genuine, "registry.get_version.junk", c.get_version, "s", 1)
            _probe(genuine, "registry.get_latest.junk", c.get_latest, "s")
            _probe(genuine, "registry.is_reachable.junk", c.is_reachable)
            _probe(genuine, "registry.to_fixture_dict.junk", c.to_fixture_dict)
    # junk token axis on __init__: any token value is stored verbatim in the
    # Authorization header dict (no coercion), so construction and a fast-refused
    # call must fail only through the designed RegistryError.
    for t in JUICE:
        ct = SchemaRegistryClient("http://127.0.0.1:1", token=t, timeout=0.1)
        _probe(genuine, "registry.list_subjects.junk_token", ct.list_subjects)
        _probe(genuine, "registry._get.junk_token", ct._get, "/subjects")
    # __init__ normalization (str(base_url).rstrip("/")) must never crash on
    # junk base_url/token/timeout shapes at construction time.
    for b in JUICE:
        for t in JUICE:
            _probe(genuine, "registry.SchemaRegistryClient.init",
                   SchemaRegistryClient, b, t, None)
    # _compatibility_level wraps the /config fetch in try/RegistryError -> None,
    # so every junk subject/base_url/timeout must yield None or the designed
    # RegistryError, never a raw crash.
    for b in (None, 1, [], {}, b"x", "", set(), float("inf"), 0, 3.14):
        for t in (None, 1, "x", 0, -1.0, float("inf"), float("nan"), 1e300):
            c = SchemaRegistryClient(b, timeout=t)
            for s in JUICE:
                _probe(genuine, "registry._compatibility_level.junk",
                       c._compatibility_level, s)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_registry_live_wire_junk_axis_probes():
    """Composed arg->wire->mock->wire->parse round-trip with junk subject/
    version args must fail closed as the designed RegistryError.

    The fast-refused base probes above never reach the network, so they miss
    the join of _quote_path, HTTP routing, the mock accessors, _wire_schema
    serialization, and the client response normalization.  Driving a live
    MockRegistry with junk args exercises that whole path at once.
    """
    from kgov.mock_registry import MockRegistry
    from kgov.registry import SchemaRegistryClient

    genuine = {}
    with MockRegistry() as mk:
        mk.add_subject("s", {"id": 1, "version": 1, "schema": {"type": "string"}},
                       {"id": 2, "version": 2, "schema": {"type": "long"}})
        c = SchemaRegistryClient(mk.base_url, timeout=5)
        subjects = JUICE + ["a/b", "a b", "s", "x" * 300, "%2F"]
        versions = JUICE + ["latest", "1", 1, 1.0, True, b"1", "999"]
        for s in subjects:
            for v in versions:
                _probe(genuine, "registry.get_version.live_wire",
                       c.get_version, s, v)
        for s in subjects:
            _probe(genuine, "registry.get_versions.live_wire",
                   c.get_versions, s)
            _probe(genuine, "registry.get_latest.live_wire",
                   c.get_latest, s)
        # junk schema ids on the live wire: int() gating happens client-side
        # and the /schemas/ids/{id} route re-gates handler-side; every junk id
        # must exit as the designed RegistryError (or a valid dict for the
        # coercible ids present in the mock).
        for sid in JUICE + ["1", b"1", 1.0, 0.0, 999, "999", 1e300, -0.0,
                            "-1"]:
            _probe(genuine, "registry.get_schema_by_id.live_wire",
                   c.get_schema_by_id, sid)


def test_registry_live_wire_junk_fixture_probes():
    """Composed wire derived from junk fixture *data* (junk schema/id/version/
    compatibility values, junk subject keys) must fail closed.

    The arg-junk probes drive a clean fixture, so the response-derivation
    chain — accessor coercion, _wire_schema, _reply json.dumps fallbacks, and
    the client parse — is never exercised with junk-typed values.  Seeding
    the mock with junk data closes that gap in one composed round-trip.
    """
    from kgov.mock_registry import MockRegistry
    from kgov.registry import SchemaRegistryClient

    def _junk_fixture() -> dict:
        subs = {}
        for k in (1, b"bb", None, True, 3.14, "a", "s"):
            subs[k] = [
                {"id": i, "version": v, "schema": sv, "compatibility": cv}
                for i, (v, sv, cv) in enumerate((
                    (1, {"type": "string"}, "BACKWARD"),
                    (2, "long", {"x": 1}),
                    (3, set(), b"B"),
                    (4, 42, None),
                    (5, {"a": [1, {"b": 2}]}, []),
                    (6, None, True),
                    (7, [1, 2], "FORWARD"),
                    (8, object(), ("NONE",)),
                ))
            ]
        return {"subjects": subs}

    genuine = {}
    with MockRegistry(fixture=_junk_fixture()) as mk:
        c = SchemaRegistryClient(mk.base_url, timeout=5)
        for s in (str(k) for k in (1, b"bb", None, True, 3.14, "a", "s")):
            _probe(genuine, "registry.get_versions.junk_fixture",
                   c.get_versions, s)
            _probe(genuine, "registry.get_latest.junk_fixture",
                   c.get_latest, s)
            for v in ("latest", 1, 2, 8, "1", "999", 0, -1, 3.14):
                _probe(genuine, "registry.get_version.junk_fixture",
                       c.get_version, s, v)
        for sid in (0, 1, 2, 3, 4, 5, 6, 7, 8, 99, -1, 3.14, "7", b"2",
                    None):
            _probe(genuine, "registry.get_schema_by_id.junk_fixture",
                   c.get_schema_by_id, sid)
        _probe(genuine, "registry.list_subjects.junk_fixture", c.list_subjects)
        _probe(genuine, "registry.to_fixture_dict.junk_fixture",
               c.to_fixture_dict)
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
            # a junk settings value (an int, a string, a dict, a list, ...)
            # must never leak a raw AttributeError from
            # settings.consumer_grace_seconds — fail closed to the stock
            # StalenessSettings tolerances
            _probe(genuine, "report.build_topic_state.junk_settings_obj",
                   rp.build_topic_state,
                   {"name": "t",
                    "latest_message_at": "2020-01-01T00:00:00Z"},
                   {}, {}, {}, None, j)
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
        # _to_int (nested in build_topic_state) funnels partitions/
        # replication_factor through int() and catches TypeError/ValueError/
        # OverflowError → default; junk numeric shapes (None via missing key,
        # floats, inf/nan, bytes, lists, dicts, objects, bools, "1", ...) must
        # all land on the default without leaking a raw exception.
        for j in JUICE:
            _probe(genuine, "report.build_topic_state.junk_partitions",
                   rp.build_topic_state,
                   {"name": "t", "partitions": j, "replication_factor": j},
                   {}, {}, {}, None)
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
    from kgov.registry import SchemaRegistryClient

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
    # _reader_for is the current schema-id cache look-up: unhashable junk ids
    # (list/dict/set) must fail through the designed ValueError, never leak the
    # raw TypeError from the dict membership test; hashable junk ids route to
    # get_schema_by_id on the fast-refused client (designed RegistryError).
    dec_r = bk.AvroRecordDecoder(
        SchemaRegistryClient("http://127.0.0.1:1", timeout=0.1))
    for j in JUICE:
        _probe(genuine, "broker._reader_for.junk_id", dec_r._reader_for, j)
    # _RecordReader.__init__ only stores its schema (no parsing), so any junk
    # schema must construct cleanly; _now_ms is a pure monotonic-ish clock.
    for j in JUICE:
        _probe(genuine, "broker._RecordReader.init", bk._RecordReader, j)
    _probe(genuine, "broker._now_ms", bk._now_ms)
    # _default_decoder_factory with a junk reader must fail through the
    # designed ValueError, never leak the raw AttributeError from .read; the
    # public __call__ path already swallows factory exceptions to _UNDECODED.
    for r in (None, 1, "x", [], {}, object()):
        for _payload in (b"", b"\x00", None, 1, "x"):
            _probe(genuine, "broker._default_decoder_factory.junk",
                   bk._default_decoder_factory, r, _payload)
    # a junk decoder_factory is swallowed by AvroRecordDecoder.__call__ (the
    # factory invocation is wrapped) so public callers always get _UNDECODED;
    # junk factories must never crash __call__ itself before the wrap.
    for f in JUICE:
        dec = bk.AvroRecordDecoder(object(), decoder_factory=f)
        for _payload in (b"\x00\x00\x00\x00\x00junk", b"", b"\x00", None, 1):
            _probe(genuine, "broker.AvroRecordDecoder.junk_factory", dec,
                   _payload)
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
    # _require_kafka: the import succeeds in this venv (clean no-op), and the
    # NeedsBrokerError arm is pinned in test_broker.py; probing keeps both in
    # the battery so a future import-rename cannot go unpinned.
    _probe(genuine, "broker._require_kafka", bk._require_kafka)
    # _probe_bootstrap: read_topic's connectivity probe.  Under the (j, j) junk
    # shape every pair trips the pure-Python gates (_require_bootstrap and the
    # _require_timeout added for the int(timeout*1000) OverflowError class)
    # before any AdminClient import — the fail-closed contract is what the
    # battery checks.  The reachable/unreachable arms with a stubbed AdminClient
    # are pinned in test_broker.py where a monkeypatch fixture is natural.
    for j in JUICE:
        _probe(genuine, "broker._probe_bootstrap.junk",
               bk._probe_bootstrap, j, j)
    # The battery above probes _reader_for only with a fast-refused client, so
    # the join of live get_schema_by_id -> parsed record schema -> _RecordReader
    # decode is never exercised.  Drive it here over a live MockRegistry with a
    # genuine Confluent-wire payload: a record schema (id 1), a non-record
    # schema (id 2), and a record-shaped schema the avro library cannot parse
    # (id 3) whose SchemaParseException used to leak from _reader_for.  Junk
    # payloads/ids must all fail closed as designed (RegistryError/ValueError)
    # or decode to _UNDECODED, never raise a third-party avro error.
    import io
    import json
    import struct

    import avro.io as avro_io
    import avro.schema as avro_schema
    from kgov.mock_registry import MockRegistry
    from kgov.registry import SchemaRegistryClient

    schema = {"type": "record", "name": "R", "fields": [
        {"name": "a", "type": "string"},
        {"name": "b", "type": ["null", "int"]},
    ]}
    with MockRegistry() as mk:
        mk.add_subject("s",
                       {"id": 1, "version": 1, "schema": schema},
                       {"id": 2, "version": 2, "schema": {"type": "string"}},
                       {"id": 3, "version": 3,
                        "schema": {"type": "record", "name": "R2",
                                   "fields": [{"name": "x",
                                               "type": "bogus"}]}})
        dec = bk.AvroRecordDecoder(SchemaRegistryClient(mk.base_url, timeout=5))

        def _wire(schema_id, datum):
            buf = io.BytesIO()
            avro_io.DatumWriter(avro_schema.parse(json.dumps(schema))).write(
                datum, avro_io.BinaryEncoder(buf))
            return bk.CONFLUENT_MAGIC + struct.pack(">I", schema_id) + \
                buf.getvalue()

        good = _wire(1, {"a": "hi", "b": 7})
        _probe(genuine, "broker.AvroRecordDecoder.live_wire.good", dec, good)
        # junk payloads reach the wire parse for len>=5 magic bytes: junk ids
        # must fail through RegistryError, the bogus id-3 schema as _UNDECODED
        # (its Wire-shape is only magic+id bytes, since the datum is never
        # parsed once the record schema fails to parse).
        for p in JUICE + [b"\x00" * 6, b"\x00\x00\x00\x00\x01junk",
                          b"\x00\x00\x00\x00\x02junk", b"\x00\x00\x00\x00\x03junk",
                          b"\x00\x00\x00\x00\xffab", good + b"trailing",
                          b"\x00\x00\x00\x00\x00\x00"]:
            _probe(genuine, "broker.AvroRecordDecoder.live_wire.junk", dec, p)
        # live _reader_for: registered ids build a reader, missing/junk ids
        # fail through the client's designed RegistryError.
        _probe(genuine, "broker._reader_for.live_wire", dec._reader_for, 1)
        rr = dec._reader_for(1)
        for sid in JUICE + [0, 2, 3, 4, 99, "1", 1.0, -0.0]:
            _probe(genuine, "broker._reader_for.live_wire.junk",
                   dec._reader_for, sid)
        # live _RecordReader.read: the genuine schema decodes a payload,
        # junk payloads fail through the designed ValueError.
        for p in JUICE + [b"", b"\x01", b"\x02" + good, good]:
            _probe(genuine, "broker._RecordReader.read.live_wire", rr.read, p)
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
    # list_subjects with mixed-type subject keys in the fixture (sorted()
    # over str/bool/int/float/bytes keys must not raise)
    mixed_seeds = [
        {"subjects": {1: [], "a": []}},
        {"subjects": {b"b": [], "a": []}},
        {"subjects": {3.14: [], 1: []}},
        {"subjects": {True: [], 1: []}},
        {"subjects": {None: []}},
        {"subjects": "junk"},
        {"subjects": {}},
        {"subjects": None},
        "junk",
        1,
        [1, 2],
    ]
    for seed in mixed_seeds:
        mk3 = mr.MockRegistry(fixture=seed)
        _probe(genuine, "mr.list_subjects.mixed_keys", mk3.list_subjects)
    # lifecycle sinks: junk ports that cannot bind (non-ints, NaN/inf floats,
    # out-of-range longs) must fail through the designed MockRegistryError;
    # bindable ports are excluded so no server thread is ever created.
    for p in (None, "x", 3.14, float("nan"), float("inf"), [], {}, set(),
              (1,), b"7", 1e300, -1e300):
        m = mr.MockRegistry()
        _probe(genuine, "mr.start.junk_port", m.start, p)
        _probe(genuine, "mr.stop.after_junk", m.stop)
    lc = mr.MockRegistry()
    _probe(genuine, "mr.start.then_stop", lc.start, 0)
    _probe(genuine, "mr.stop", lc.stop)
    _probe(genuine, "mr.stop.idempotent", lc.stop)
    _probe(genuine, "mr.base_url.unstarted", lambda: mr.MockRegistry().base_url)
    c_un = mr.MockRegistry().client
    _probe(genuine, "mr.client.unstarted_base", lambda: c_un.base_url)
    _probe(genuine, "mr.client.list_subjects.unstarted", c_un.list_subjects)
    # __exit__ as a context-manager sink: junk args / unstarted server must
    # never raise; it only delegates to the idempotent stop().
    m_ex = mr.MockRegistry()
    _probe(genuine, "mr.__exit__.unstarted", m_ex.__exit__, None, None, None)
    _probe(genuine, "mr.__exit__.junk", m_ex.__exit__, 1, "x", [])
    # RegistryHandler internals: a handler instance created without the
    # socket-server __init__ (object.__new__) has no path/wfile/registry;
    # every routing entry must fail closed instead of raw-crashing. The
    # instance is a REAL RegistryHandler so class methods (_path_parts,
    # _reply, ...) still resolve; only the attributes are missing.
    no_attrs = object.__new__(mr.RegistryHandler)
    for meth in ("_path_parts", "do_GET", "do_POST", "do_PUT", "do_DELETE"):
        _probe(genuine, "mr.handler.noattrs", getattr(mr.RegistryHandler, meth),
               no_attrs)
    _probe(genuine, "mr.handler._error", mr.RegistryHandler._error,
           no_attrs, 404, "boom")
    # log_message is a deliberate no-op (keeps the test output quiet); probing
    # it pins that it never raises regardless of fmt/args shapes.
    _probe(genuine, "mr.handler.log_message", mr.RegistryHandler.log_message,
           no_attrs, "GET %s -> %s", "/subjects", 200)
    for bad_path in JUICE:
        h = object.__new__(mr.RegistryHandler)
        h.path = bad_path
        _probe(genuine, "mr.handler.junk_path", mr.RegistryHandler.do_GET, h)
        _probe(genuine, "mr.handler.junk_path_parts",
               mr.RegistryHandler._path_parts, h)
        _probe(genuine, "mr.handler.junk_subject_from_path",
               mr.RegistryHandler._subject_from_path, h)
    # _reply against a bare object (no send_response/wfile at all): the
    # guarded response sequence must swallow AttributeError/OSError.
    for payload in (object(), {1, 2}, b"bytez", {"k": b"v"}, 3.14, None):
        _probe(genuine, "mr.handler.junk_wfile", mr.RegistryHandler._reply,
               object(), 200, payload)
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
    _load_json_or_exit, _utf8_safe, _load_schema_text, _require_export_format.

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
        _probe(genuine, "cli._utf8_safe", c._utf8_safe, j)
        _probe(genuine, "cli._load_schema_text", c._load_schema_text, j, None)
        _probe(genuine, "cli._load_schema_text.fmt",
               c._load_schema_text, "{}", j)
        ns = type("A", (), {"format": j, "type": "report"})()
        _probe(genuine, "cli._require_export_format",
               c._require_export_format, ns, {"json", "csv"})
        argv_chars = ["check", "--config", j, "--broker", j, "--topic", j]
        argv_chars = [a for a in argv_chars if isinstance(a, str)]
        _probe(genuine, "cli.main.junk_argv", c.main, argv_chars)
        _probe(genuine, "cli.main.junk_argv_single", c.main, [j])
    # main() must fail closed on junk argv shapes (designed usage error → 2 or
    # SystemExit, never a raw escape); notes: argparse exits via SystemExit so
    # _probe() records only genuinely-escaped exceptions
    _probe(genuine, "cli.main.build_parser_argv", c.main, ["--version"])
    # junk non-list argv, mixed junk types, unknown command
    _probe(genuine, "cli.main.junk_nonlist", c.main, None)
    _probe(genuine, "cli.main.junk_nonlist2", c.main, 3.14)
    _probe(genuine, "cli.main.mixed_junk", c.main, [1, b"x", [], {}])
    _probe(genuine, "cli.main.unknown_cmd", c.main, ["bogus-cmd"])
    _probe(genuine, "cli.build_parser", c.build_parser)
    # _validate: with at least one source gate guaranteed to fail, junk never
    # reaches a live client.  messages_path junk always fails its gate first
    # (_validate runs the messages branch before the broker branch); the
    # broker branch junk is probed with a non-str broker so the --broker gate
    # fails before any confluent import / AdminClient is touched.
    import kgov.rules as rl

    rs = rl.RuleSet()
    for j in JUICE:
        _probe(genuine, "cli._validate.junk_msgpath", c._validate, rs, j,
               None, None)
        _probe(genuine, "cli._validate.junk_broker", c._validate, rs, None,
               j, None)
        _probe(genuine, "cli._validate.junk_topic", c._validate, rs, None,
               b"x", j)
        _probe(genuine, "cli._validate.junk_registry", c._validate, rs,
               None, b"x", b"x", j)
    _probe(genuine, "cli._validate.both_junk", c._validate, rs, None, 3.14, [])
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_cli_load_schema_text_shorthand_probes():
    """Short-hand ``[http://]registry/subject/version`` refs resolve against a
    live registry; every junk wire ``schema`` shape must fold into a clean
    return.  The wire JSON-encodes all values, get_version normalizes any
    schema through _parse_schema_string, and the loader returns the raw (often
    non-str) body for the downstream designed SchemaParseError — no raw crash
    may escape the network round-trip, the missing-key fallback, or the body
    plumbing.  Junk ref shapes must fail closed before any fetch, and unknown
    subjects must surface as the designed CliError.
    """
    from kgov.cli import _load_schema_text
    from kgov.mock_registry import MockRegistry

    genuine = {}
    for schema_value in (None, 42, ["a", "b", True],
                         {"type": "record", "name": "R", "fields": []}):
        with MockRegistry() as mk:
            mk.add_subject("s", {"id": 1, "version": 1, "schema": schema_value})
            ref = f"{mk.base_url[7:]}/s/1"  # strip http:// -> short-hand form
            for fmt in (None, "avro", "json", 1, b"avro", ["x"]):
                _probe(genuine, "cli._load_schema_text.short",
                       _load_schema_text, ref, fmt)
    # schema key missing entirely from the wire dict -> json.dumps fallback
    with MockRegistry() as mk:
        mk.add_subject("s", {"id": 1, "version": 1})
        ref = f"{mk.base_url[7:]}/s/1"
        _probe(genuine, "cli._load_schema_text.short_noschema",
               _load_schema_text, ref, None)
    # unknown subject -> fetch fails through the designed CliError
    with MockRegistry() as mk:
        ref = f"{mk.base_url[7:]}/nope/1"
        _probe(genuine, "cli._load_schema_text.short_missing",
               _load_schema_text, ref, None)
    # junk ref shapes never reach the network (CliError gates keep them local)
    for ref in JUICE:
        _probe(genuine, "cli._load_schema_text.short_junkref",
               _load_schema_text, ref, None)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_cli_load_schema_text_url_probes():
    """Full ``http://`` refs resolve through ``urllib.request.urlopen`` against
    a live registry — the branch every short-hand pin strips away.  Junk wire
    ``schema`` bodies (JSON-encoded on the wire), a missing schema key,
    non-subjects routes and every junk ref shape must fold into a clean return
    or the designed CliError; the urlopen round-trip may never leak a raw
    exception out of _load_schema_text.
    """
    from kgov.cli import _load_schema_text
    from kgov.mock_registry import MockRegistry

    genuine = {}
    for schema_value in (None, 42, ["a", "b", True],
                         {"type": "record", "name": "R", "fields": []},
                         "not-json", 3.14, b"bytes", {"schema": "inner"},
                         [{"k": 1}], True):
        with MockRegistry() as mk:
            mk.set_fixture({"subjects": {"s": [
                {"id": 1, "version": 1, "schema": schema_value}]}})
            url = f"{mk.base_url}/subjects/s/versions/1"
            for fmt in (None, "avro", "json", 1, b"avro", ["x"], {}):
                _probe(genuine, "cli._load_schema_text.url",
                       _load_schema_text, url, fmt)
    # schema key absent wire-side -> json.dumps fallback on the fetched dict
    with MockRegistry() as mk:
        mk.set_fixture({"subjects": {"s": [{"id": 1, "version": 1}]}})
        url = f"{mk.base_url}/subjects/s/versions/1"
        _probe(genuine, "cli._load_schema_text.url_noschema",
               _load_schema_text, url, None)
    # unknown subject -> 404 -> urllib HTTPError -> designed CliError
    with MockRegistry() as mk:
        url = f"{mk.base_url}/subjects/nope/versions/1"
        _probe(genuine, "cli._load_schema_text.url_missing",
               _load_schema_text, url, None)
    # non-subjects routes answer lists/scalars: not str, no "schema" key
    for path in ("/subjects", "/"):
        with MockRegistry() as mk:
            mk.set_fixture({"subjects": {}})
            url = f"{mk.base_url}{path}"
            _probe(genuine,
                   f"cli._load_schema_text.url_route.{path.replace('/', '_')}",
                   _load_schema_text, url, None)
    # junk ref shapes gate before any fetch or fail through fast-refused urls
    for ref in JUICE + ["http://127.0.0.1:1/nope/1",
                        "http://127.0.0.1:1/subjects/s/versions/1"]:
        _probe(genuine, "cli._load_schema_text.url_junkref",
               _load_schema_text, ref, None)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_cli_live_wire_probes(tmp_path, monkeypatch):
    """The CLI command layer over a live MockRegistry must fail closed.

    cmd_registry with a live --registry-url attaches a real client, so junk
    subject x timeout values drive the composed arg -> wire -> mock -> parse
    chain that the fast-refused base probes never reach.  Junk output paths
    are probed from a temp cwd so truthy-string paths write probe files there,
    never the repo tree.

    cmd_schema_diff with short-hand refs on both sides resolves both schemas
    against the live mock, exercising _load_schema_text -> diff_schemas ->
    _write_out with a junk value on each namespace option in turn.
    """
    from kgov.cli import cmd_registry, cmd_schema_diff
    from kgov.mock_registry import MockRegistry

    monkeypatch.chdir(tmp_path)
    genuine = {}
    with MockRegistry() as mk:
        mk.add_subject("s", {"id": 1, "version": 1, "schema": {"type": "string"}},
                       {"id": 2, "version": 2, "schema": {"type": "long"}})
        # cmd_registry: live base, junk subject x junk timeout
        for subj in JUICE + ["s", "a/b", "a b", "x" * 300, "%2F"]:
            for t in (None, 5, 0.5, -1, "x", float("inf")):
                ns = type("NS", (), {"registry_url": mk.base_url, "timeout": t,
                                     "subject": subj, "output": None})()
                _probe(genuine, "cli.cmd_registry.live_wire",
                       cmd_registry, ns)
        # cmd_registry: junk output paths with a valid subject
        for o in JUICE + ["reg_out.json"]:
            ns = type("NS", (), {"registry_url": mk.base_url, "timeout": 5,
                                 "subject": "s", "output": o})()
            _probe(genuine, "cli.cmd_registry.live_wire_output",
                   cmd_registry, ns)
        # cmd_schema_diff: short-hand refs both sides, junk per-option values
        ref1 = f"{mk.base_url[7:]}/s/1"
        ref2 = f"{mk.base_url[7:]}/s/2"
        base = {"output": None, "json": False, "markdown": False,
                "subject": "rec", "before_version": 1, "after_version": 2,
                "before_id": None, "after_id": None, "format": None}
        for before, after in ((ref1, ref2), (ref2, ref1), (ref1, ref1)):
            for k in base:
                for j in (None, 1, 3.14, b"x", [], {}, "s", True):
                    n = dict(base)
                    n[k] = j
                    ns = type("NS", (), {"before": before, "after": after,
                                         **n})()
                    _probe(genuine, "cli.cmd_schema_diff.live_wire",
                           cmd_schema_diff, ns)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


def test_broker_live_wire_probes(tmp_path, monkeypatch):
    """The live confluent-kafka client join must fail closed, never leak.

    cmd_topics with a VALID --bootstrap and the broker read paths with a valid
    bootstrap/topic pass the pure-Python gates and reach the real confluent
    client — the same join the junk probes above die before ever hitting.  An
    unreachable localhost port (127.0.0.1:1, TCP refused) fails fast under
    ``socket.timeout.ms`` with no external network, and each path must fail
    closed in its designed shape:

    - ``broker.read_topic``    -> BrokerConnectError via _probe_bootstrap
    - ``broker.list_topics``   -> BrokerConnectError (wrapped, no raw KafkaException)
    - ``broker.consumer_groups`` -> {} (designed degradation on unreachable)
    - ``cmd_topics``           -> CliError ("failed to list topics")
    - ``cmd_check --broker``   -> CliError ("broker scan failed"), junk
      schema_registry shapes dying at the URL gate; the truthy-str "abc"
      reaches the (inert decoder) construction then dies at the scan

    Junk group_id/n/timeout values die at the pure-Python gates (fast), so the
    live-wire reach is limited to valid values; timeouts stay at 0.5s so each
    reach costs ~0.5s under socket.timeout.ms.  Truthy junk output paths with a
    raising list_topics never _write_out (the write only happens on success).
    cmd_check has no --timeout knob, so its reach costs the full read_topic
    default (~10s) and is capped to two representatives.
    """
    from kgov.broker import consumer_groups, list_topics, read_topic
    from kgov.cli import cmd_check, cmd_topics

    monkeypatch.chdir(tmp_path)
    genuine = {}
    un = "127.0.0.1:1"  # TCP refused, fails fast with no external network

    # Junk group_id/n values: 2+30 junk g-values x 5 n-values, all dying at
    # the str/int gates before _require_kafka.  Only "abc"/"1" (non-empty
    # strs that happen to sit in JUICE) with n in {1, None} reach the wire.
    for g in JUICE + ["a/b", b"x"]:
        for n in [1, -1, "x", 2.5, None]:
            _probe(genuine, "broker.read_topic.live_wire.junk_gates",
                   lambda g=g, n=n: read_topic(un, "_kgov_never_a_topic",
                                               group_id=g, n=n, timeout=0.5))
    # Valid group_id/n reach the wire and must fail closed (~0.5s each).
    for g in ["z" * 5, "a/b", "a b", "x" * 300]:
        for n in [None, 1, 2]:
            _probe(genuine, "broker.read_topic.live_wire.reach",
                   lambda g=g, n=n: read_topic(un, "_kgov_never_a_topic",
                                               group_id=g, n=n, timeout=0.5))
    # Junk timeouts die at _require_timeout; only 3.14 (a positive finite
    # float that happens to sit in JUICE) reaches the wire per read path.
    for t in JUICE + [0.5]:
        _probe(genuine, "broker.list_topics.live_wire", list_topics, un, t)
        _probe(genuine, "broker.consumer_groups.live_wire",
               consumer_groups, un, t)
    # cmd_topics valid bootstrap: junk timeouts die fast inside list_topics
    # (wrapped CliError); timeout 0.5 reaches the wire once per output value,
    # and the output path never writes because list_topics raised.
    for o in [None, "topics_out.json", 5, b"x", []]:
        ns = type("NS", (), {"bootstrap": un, "timeout": 0.5, "output": o})()
        _probe(genuine, "cli.cmd_topics.live_wire", cmd_topics, ns)
    # cmd_check --broker: a valid broker/topic compose _validate over real
    # read_topic.  check has no --timeout knob, so every reach costs the full
    # read_topic default (~10s under socket.timeout.ms), and the reach is
    # capped to two representatives.  A junk truthy-str schema_registry
    # ("abc") passes the URL gate, is str()'d into an inert SchemaRegistryClient
    # constructor, and dies at the scan before any decode — CliError, never a
    # raw escape.  Junk registry values (any shape) die at the URL gate fast.
    rules = Path(__file__).parent / "fixtures" / "rules" / "rules_scale.yaml"
    assert rules.is_file()
    ns_kw = dict(config=rules, topic="payments", broker=un, messages=None,
                 max_violations=None, output=None, quiet=True)
    _probe(genuine, "cli.cmd_check.live_wire.noreg",
           cmd_check, type("NS", (), {**ns_kw, "schema_registry": None})())
    _probe(genuine, "cli.cmd_check.live_wire.junkstr_reg",
           cmd_check, type("NS", (), {**ns_kw, "schema_registry": "abc"})())
    for reg in [0, b"x", [], 5, {"a": 1}, ""]:
        _probe(genuine, "cli.cmd_check.live_wire.junk_reg_gate",
               cmd_check, type("NS", (), {**ns_kw, "schema_registry": reg})())
    assert genuine == {}, f"genuine crashes escaped: {genuine}"
    """cmd_check / cmd_report / cmd_export with junk-typed FILE CONTENTS.

    The junk-namespace probes above die at the parent gates (cmd_check needs
    an existing --config file, cmd_report an existing --topics file).  A real
    existing file whose CONTENTS are junk shapes drives the composed chain the
    gate probes never reach: load_rules_file -> _validate for check; junk
    topics/registry/groups/violations contents -> build_governance_report for
    report; and the cmd_export delegations for junk type x format values.
    """
    import json

    from kgov.cli import cmd_check, cmd_export, cmd_report

    monkeypatch.chdir(tmp_path)
    genuine = {}
    fix = Path(__file__).parent / "fixtures"
    rules = fix / "rules" / "rules_scale.yaml"
    msgs = fix / "messages" / "payments_messages.json"
    topics = fix / "topics" / "topics.json"
    registry = fix / "registry" / "registry.json"
    groups = fix / "subject_groups" / "consumer_groups.json"

    def write_junk(name, shape):
        p = tmp_path / name
        if shape is None or isinstance(shape, bool):
            p.write_bytes(b"")  # empty file
        elif isinstance(shape, bytes):
            p.write_bytes(shape)
        elif isinstance(shape, str):
            p.write_text(shape, encoding="utf-8", errors="replace")
        else:
            # json-encode: cyclic containers fail designed; sanitize surrogates
            try:
                p.write_text(json.dumps(shape), encoding="utf-8",
                             errors="replace")
            except Exception:
                p.write_text("{}")
        return p

    def ns(**kw):
        return type("NS", (), kw)()

    for shape in JUICE:
        mp = write_junk("msgs.json", shape)
        _probe(genuine, "cli.cmd_check.junk_msg_contents",
               cmd_check, ns(config=rules, topic=None, broker=None,
                             messages=mp, schema_registry=None,
                             max_violations=None, output=None, gate=False,
                             quiet=True))
    for shape in JUICE:
        rp = write_junk("rules.yaml", shape)
        _probe(genuine, "cli.cmd_check.junk_rules_contents",
               cmd_check, ns(config=rp, topic=None, broker=None,
                             messages=msgs, schema_registry=None,
                             max_violations=None, output=None, gate=False,
                             quiet=True))
    for shape in JUICE:
        tp = write_junk("topics.json", shape)
        _probe(genuine, "cli.cmd_report.junk_topics",
               cmd_report, ns(topics=tp, registry=registry, groups=groups,
                              rules=None, messages=None, violations=None,
                              format=None, output=None))
    for shape in JUICE:
        rp = write_junk("registry.json", shape)
        _probe(genuine, "cli.cmd_report.junk_registry",
               cmd_report, ns(topics=topics, registry=rp, groups=groups,
                              rules=None, messages=None, violations=None,
                              format=None, output=None))
    for shape in JUICE:
        gp = write_junk("groups.json", shape)
        _probe(genuine, "cli.cmd_report.junk_groups",
               cmd_report, ns(topics=topics, registry=registry, groups=gp,
                              rules=None, messages=None, violations=None,
                              format=None, output=None))
    for shape in JUICE:
        vp = write_junk("violations.json", shape)
        _probe(genuine, "cli.cmd_report.junk_violations",
               cmd_report, ns(topics=topics, registry=None, groups=None,
                              rules=None, messages=None, violations=vp,
                              format=None, output=None))
    # cmd_report --rules + --messages: the load_rules_file -> validate_fixture_file
    # branch, junk-CONTENT on either side (real fixture files exist as the gate).
    for shape in JUICE:
        rp = write_junk("rules.yaml", shape)
        _probe(genuine, "cli.cmd_report.junk_rules_content",
               cmd_report, ns(topics=topics, registry=None, groups=None,
                              rules=rp, messages=msgs, violations=None,
                              format=None, output=None))
    for shape in JUICE:
        mp = write_junk("msgs.json", shape)
        _probe(genuine, "cli.cmd_report.junk_msgs_content",
               cmd_report, ns(topics=topics, registry=None, groups=None,
                              rules=rules, messages=mp, violations=None,
                              format=None, output=None))
    # both-sides junk plus the render path (junk --format x --output from the
    # temp cwd so truthy-string outputs write probe files there, not the repo).
    for rs in (b"", {1: 2}, "not yaml::", 5.5):
        for mp in (b"", [7], "[[", {"violations": "x"}):
            rp = write_junk("rules.yaml", rs)
            pp = write_junk("msgs.json", mp)
            _probe(genuine, "cli.cmd_report.junk_rules_msgs_both",
                   cmd_report, ns(topics=topics, registry=None, groups=None,
                                  rules=rp, messages=pp, violations=None,
                                  format=None, output=None))
    for f in JUICE + ["json", "csv", "text"]:
        for o in JUICE + ["out.json", "out.csv"]:
            _probe(genuine, "cli.cmd_report.render_format_output",
                   cmd_report, ns(topics=topics, registry=None, groups=None,
                                  rules=rules, messages=msgs, violations=None,
                                  format=f, output=o))
    for t in JUICE + ["check", "violations", "report", "schema-diff"]:
        for f in JUICE + ["json", "csv", "markdown", "md"]:
            _probe(genuine, "cli.cmd_export.delegate",
                   cmd_export, ns(type=t, format=f, config=rules,
                                  messages=msgs, topics=topics,
                                  registry=registry, groups=groups,
                                  output=None))
    assert genuine == {}, f"genuine crashes escaped: {genuine}"


# ---------------------------------------------------------------------------
# deep nesting
# ---------------------------------------------------------------------------


def _deep_nested_list(depth):
    out = []
    cur = out
    for _ in range(depth):
        cur.append([])
        cur = cur[-1]
    return out


def _deep_nested_dict(depth):
    out = {}
    cur = out
    for _ in range(depth):
        cur["a"] = {}
        cur = cur["a"]
    return out


def _deep_avro_record(depth):
    out = {"type": "record", "name": "R", "fields": []}
    cur = out
    for _ in range(depth):
        cur["fields"] = [{"name": "a",
                          "type": {"type": "record", "name": "R",
                                   "fields": []}}]
        cur = cur["fields"][0]["type"]
    return out


def _deep_js_schema(depth):
    out = {"type": "object", "properties": {}}
    cur = out
    for _ in range(depth):
        cur["properties"] = {"a": {"type": "object", "properties": {}}}
        cur = cur["properties"]["a"]
    return out


def _deep_anyof(depth):
    out = {"anyOf": [True]}
    cur = out
    for _ in range(depth):
        cur["anyOf"] = [{"anyOf": [True]}]
        cur = cur["anyOf"][0]
    return out


def test_deep_nesting_probes():
    """Pathologically deep (but cycle-free) containers must fail closed instead
    of escaping a raw RecursionError.  Kept out of JUICE on purpose: at the
    interpreter's recursionlimit (1000), stdlib ``copy`` (used by the
    asdict/report battery surfaces) cannot even copy a 2000-deep container, so
    a shared deep JUICE member would raise stdlib-framed GENUINE false
    positives in every battery surface that copies it.

    The probe depths sit far above the helpers' fail-closed caps
    (_MAX_JSON_DEPTH=300, _MAX_RECURSE_DEPTH=200), so the marker-leaf paths are
    exactly what gets exercised.
    """
    import kgov.model as m
    import kgov.schema_diff as sd

    depth = 2000
    deep_list = _deep_nested_list(depth)
    deep_dict = _deep_nested_dict(depth)
    deep_avro = _deep_avro_record(depth)
    deep_avro_b = _deep_avro_record(depth + 2)
    deep_js = _deep_js_schema(depth)
    deep_js_b = _deep_js_schema(depth + 2)
    deep_anyof = _deep_anyof(depth)
    genuine = {}
    _probe(genuine, "deep.model._json_safe.list", m._json_safe, deep_list)
    _probe(genuine, "deep.model._json_safe.dict", m._json_safe, deep_dict)
    _probe(genuine, "deep.sd._printable", sd._printable, deep_list)
    _probe(genuine, "deep.sd._desc", sd._desc, deep_list)
    _probe(genuine, "deep.sd._name_of", sd._name_of, deep_list)
    _probe(genuine, "deep.sd._type_names", sd._type_names, deep_list)
    _probe(genuine, "deep.sd._js_string_types", sd._js_string_types, deep_anyof)
    _probe(genuine, "deep.sd._avro_diff", sd._avro_diff,
           deep_avro, deep_avro_b, "", [])
    _probe(genuine, "deep.sd._js_diff.props", sd._js_diff,
           deep_js, deep_js_b, "", [])
    _probe(genuine, "deep.sd.diff_avro", sd.diff_avro, deep_avro, deep_avro_b)
    _probe(genuine, "deep.sd.diff_json_schema", sd.diff_json_schema,
           deep_js, deep_js_b)
    _probe(genuine, "deep.sd.diff_schemas", sd.diff_schemas,
           deep_avro, deep_avro_b)
    rep = m.SchemaDiffReport(format="avro", subject="s", before_version=1,
                             after_version=2, kind="Breaking",
                             results=[{"kind": "Breaking", "path": "a",
                                       "message": deep_list}])
    _probe(genuine, "deep.model.SchemaDiffReport.to_dict", rep.to_dict)
    assert genuine == {}, f"genuine crashes escaped: {genuine}"
