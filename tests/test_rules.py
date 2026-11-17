"""P1 — dirty-data rules engine tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kgov.rules import (
    FieldCheck,
    Message,
    RuleSet,
    build_ruleset,
    iter_messages_from_json,
    load_rules_file,
    load_rules_yaml,
    validate_fixture_file,
    validate_message,
)
from kgov.rules_errors import (
    RuleFieldConfigError,
    RulesConfigError,
    UnknownFieldTypeError,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _rs(**overrides) -> RuleSet:
    data = {
        "version": 1,
        "topic": "payments",
        "fields": {
            "amount_cents": {
                "type": "integer",
                "required": True,
                "non_null": True,
                "range": {"minimum": 0, "maximum": 100000},
            },
            "currency": {"type": "string", "required": True, "pattern": "^[A-Z]{3}$"},
            "status": {"type": "string", "enum": ["ACCEPTED", "PENDING", "REJECTED"]},
            "fee_cents": {"type": "integer", "non_null": True, "range": {"minimum": 0}},
        },
    }
    data.update(overrides)
    return build_ruleset(data)


def test_build_ruleset_parses():
    rs = _rs()
    assert rs.topic == "payments"
    assert isinstance(rs.fields["amount_cents"], FieldCheck)
    assert rs.fields["amount_cents"].type == "integer"
    assert rs.fields["amount_cents"].range == {"minimum": 0, "maximum": 100000}


def test_load_rules_yaml():
    text = """
version: 1
topic: payments
fields:
  amount_cents:
    type: integer
    required: true
"""
    rs = load_rules_yaml(text)
    assert rs.fields["amount_cents"].required is True


def test_load_rules_yaml_invalid():
    with pytest.raises(RulesConfigError):
        load_rules_yaml("{ unclosed")


def test_load_rules_yaml_junk_text_args_raise_rules_config_error():
    """A rules text argument that is not a YAML string (int, float, list,
    tuple, dict, None, bool, bytes) must raise a clean RulesConfigError
    rather than a raw AttributeError from yaml.safe_load reaching for a
    missing .read()."""
    junk = (123, 2.5, [1], (1,), {"k": 1}, None, True, b"b")
    for bad in junk:
        with pytest.raises(RulesConfigError, match="YAML string"):
            load_rules_yaml(bad)


def test_load_rules_file_invalid_utf8_rejected(tmp_path):
    """A rules file with non-UTF-8 bytes must raise a clean RulesConfigError,
    not leak a UnicodeDecodeError traceback."""
    path = tmp_path / "rules.yaml"
    path.write_bytes(b"{topic: \xff\xfe}")
    with pytest.raises(RulesConfigError, match="not valid UTF-8"):
        load_rules_file(path)


def test_messages_fixture_invalid_utf8_rejected(tmp_path):
    """A messages fixture with non-UTF-8 bytes must raise a clean
    RulesConfigError, not a raw UnicodeDecodeError."""
    path = tmp_path / "msgs.json"
    path.write_bytes(b'{"messages": \xff\xfe[]}')
    with pytest.raises(RulesConfigError, match="not valid UTF-8"):
        list(iter_messages_from_json(path))


def test_junk_fixture_path_args_raise_rules_config_error():
    """A rules/messages fixture argument that is not a file path (int, bytes,
    list, dict, None, tuple, float, bool) must raise a clean RulesConfigError
    rather than a raw TypeError from pathlib/os.fspath (pathlib rejects
    bytes with its own TypeError)."""
    rs = _rs()
    junk = (1, 2.5, b"b", [1], (1,), {"k": 1}, None, True)
    for bad in junk:
        with pytest.raises(RulesConfigError, match="file path"):
            load_rules_file(bad)
        with pytest.raises(RulesConfigError, match="file path"):
            list(iter_messages_from_json(bad))
        with pytest.raises(RulesConfigError, match="file path"):
            validate_fixture_file(rs, bad)


def test_unknown_type_rejected():
    with pytest.raises(UnknownFieldTypeError):
        build_ruleset({"fields": {"x": {"type": "datetime"}}})


def test_unknown_field_key_rejected():
    with pytest.raises(RuleFieldConfigError):
        build_ruleset({"fields": {"x": {"type": "string", "bogus": 1}}})


def test_invalid_regex_rejected():
    with pytest.raises(RuleFieldConfigError):
        build_ruleset({"fields": {"x": {"pattern": "["}}})


@pytest.mark.parametrize("bound", ["abc", None, [], {}])
def test_non_numeric_range_bounds_rejected(bound):
    """A non-numeric range bound (e.g. "abc", null, a list) is a config
    error, not a raw ValueError from float() (RuleFieldConfigError subclasses
    RulesConfigError, which cmd_check maps to a clean exit 3)."""
    with pytest.raises(RuleFieldConfigError, match="range"):
        build_ruleset({"fields": {"x": {"type": "integer",
                                        "range": {"minimum": bound}}}})


def test_validate_clean_message():
    rs = _rs()
    msg = Message(
        topic="payments", partition=0, offset=3, key="k",
        value={"amount_cents": 100, "currency": "USD", "status": "ACCEPTED", "fee_cents": 5},
    )
    assert validate_message(rs, msg) == []


def test_violation_required_missing_field():
    rs = _rs()
    msg = Message(topic="payments", partition=0, offset=7, key="k",
                  value={"currency": "USD"})
    v = validate_message(rs, msg)
    assert len(v) == 1
    print(v)


def test_violation_type_mismatch():
    rs = _rs()
    msg = Message(topic="payments", partition=0, offset=9, key="k",
                  value={"amount_cents": "not-int", "currency": "USD", "status": "A", "fee_cents": 1})
    v = validate_message(rs, msg)
    assert any(x.check == "type" and x.field == "amount_cents" for x in v)


def test_violation_enum():
    rs = _rs()
    msg = Message(topic="payments", partition=0, offset=11, key="k",
                  value={"amount_cents": 5, "currency": "USD", "status": "WHATEVER", "fee_cents": 1})
    v = validate_message(rs, msg)
    assert any(x.check == "enum" and x.field == "status" for x in v)


def test_violation_pattern():
    rs = _rs()
    msg = Message(topic="payments", partition=0, offset=13, key="k",
                  value={"amount_cents": 5, "currency": "usd", "status": "A", "fee_cents": 1})
    v = validate_message(rs, msg)
    assert any(x.check == "pattern" and x.field == "currency" for x in v)


def test_violation_range_min_and_max():
    rs = _rs()
    low = Message(topic="payments", partition=0, offset=15, key="k",
                  value={"amount_cents": -5, "currency": "USD", "status": "A", "fee_cents": 1})
    high = Message(topic="payments", partition=0, offset=16, key="k",
                   value={"amount_cents": 200000, "currency": "USD", "status": "A", "fee_cents": 1})
    vl = validate_message(rs, low)
    vh = validate_message(rs, high)
    assert any(x.check == "range" and "minimum" in x.message for x in vl)
    assert any(x.check == "range" and "maximum" in x.message for x in vh)


def test_violation_range_huge_integer_no_overflow():
    """An int beyond the platform double range (e.g. 10**400) must not crash
    the range check with OverflowError on float(raw); comparisons stay exact."""
    rs = _rs()
    big = Message(topic="payments", partition=0, offset=20, key="k",
                  value={"amount_cents": 10**400, "currency": "USD", "status": "A", "fee_cents": 1})
    v = validate_message(rs, big)
    assert any(x.check == "range" and "maximum" in x.message for x in v)
    assert not any("OverflowError" in x.message for x in v)


def test_violation_non_null():
    rs = _rs()
    msg = Message(topic="payments", partition=0, offset=17, key="k",
                  value={"amount_cents": 5, "currency": "USD", "status": "A", "fee_cents": None})
    v = validate_message(rs, msg)
    assert any(x.check == "non_null" and x.field == "fee_cents" for x in v)


def test_violation_has_offset_key_and_rule():
    rs = _rs()
    msg = Message(topic="payments", partition=0, offset=42, key="pay_042",
                  value={"amount_cents": "bad", "currency": "USD"})
    v = validate_message(rs, msg)
    assert all(item.offset == 42 and item.key == "pay_042" for item in v)


def test_violation_to_dict_json_serializable():
    rs = _rs()
    msg = Message(topic="payments", partition=0, offset=1, key=b"bin\xffkey",
                  value={"amount_cents": 0, "currency": 123})
    v = validate_message(rs, msg)
    d = [item.to_dict() for item in v]
    json.dumps(d)  # must not raise


def test_topic_mismatch_ignored():
    rs = _rs()
    msg = Message(topic="other-topic", partition=0, offset=0, key="k",
                  value={"amount_cents": "bad"})
    assert validate_message(rs, msg) == []


def test_validate_message_junk_rs_or_msg_no_traceback():
    """``validate_message`` with junk rs/msg (a direct library caller or test
    double) must fail closed to [] instead of raw-crashing on ``rs.topic`` /
    ``msg.topic`` AttributeError."""
    rs = _rs()
    msg = Message(topic="payments", partition=0, offset=0, key="k",
                  value={"amount_cents": 1, "currency": "USD", "status": "ACCEPTED"})
    for junk in (None, 42, 3.5, "x", b"x", ["a"], {"topic": "payments"}, {"s"}, (1, 2), object()):
        assert validate_message(junk, msg) == []
        assert validate_message(rs, junk) == []
    assert validate_message(None, None) == []
    # valid behavior is preserved
    assert validate_message(rs, msg) == []


def test_fixture_iter_messages():
    path = FIXTURES / "messages" / "payments_messages.json"
    msgs = list(iter_messages_from_json(path))
    assert len(msgs) == 6
    assert msgs[0].offset == 0
    assert msgs[0].key == "pay_001"
    assert msgs[3].offset == 3


@pytest.mark.parametrize("body", ["null", "42", '"a string"'])
def test_fixture_scalar_root_rejected(tmp_path, body):
    """A fixture whose root is a JSON null/scalar must raise a clean
    RulesConfigError, not crash later on .get() with AttributeError."""
    path = tmp_path / "msgs.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(RulesConfigError, match="JSON object or list"):
        list(iter_messages_from_json(path))


def test_fixture_messages_not_a_list_rejected(tmp_path):
    """An object fixture whose 'messages' key is not a list is malformed."""
    path = tmp_path / "msgs.json"
    path.write_text(json.dumps({"messages": {"value": {"x": 1}}}), encoding="utf-8")
    with pytest.raises(RulesConfigError, match="must be a list"):
        list(iter_messages_from_json(path))


def test_fixture_validation_yields_expected_violations():
    """AC #2 — five sample dirty messages are correctly reported with offsets."""
    rs = load_rules_file(FIXTURES / "rules" / "rules_scale.yaml")
    path = FIXTURES / "messages" / "payments_messages.json"
    violations = validate_fixture_file(rs, path)
    by_offset = {}
    for v in violations:
        by_offset.setdefault(v.offset, []).append(v)

    # message 1: negative amount + lowercase currency + (range violated too)
    off1 = by_offset[1]
    assert any(v.check == "range" and v.field == "amount_cents" for v in off1)
    assert any(v.check == "pattern" and v.field == "currency" for v in off1)

    # message 2: payment_id int (type) + amount_cents string (type)
    off2 = by_offset[2]
    assert any(v.check == "type" and v.field == "payment_id" for v in off2)
    assert any(v.check == "type" and v.field == "amount_cents" for v in off2)

    # message 3: missing payment_id (required), status EXPIRED (enum), fee_cents null (non_null)
    off3 = by_offset[3]
    assert any(v.check == "required" and v.field == "payment_id" for v in off3)
    assert any(v.check == "enum" and v.field == "status" for v in off3)
    assert any(v.check == "non_null" and v.field == "fee_cents" for v in off3)

    # message 4: amount_cents > maximum
    off4 = by_offset[4]
    assert any(v.check == "range" and v.field == "amount_cents" and "maximum" in v.message for v in off4)

    # message 5: amount_cents null (non_null)
    off5 = by_offset[5]
    assert any(v.check == "non_null" and v.field == "amount_cents" for v in off5)

    # five dirty sample messages: offsets 1..5 each reported
    assert set(by_offset) == {1, 2, 3, 4, 5}

    # message 0 is clean
    assert 0 not in by_offset


def test_nul_byte_path_raises_rules_config_error():
    """A path containing an embedded NUL byte must raise a clean
    RulesConfigError from read paths, not a raw ValueError from read_text."""
    for call in (
        lambda: load_rules_file("a\x00b"),
        lambda: list(iter_messages_from_json("a\x00b", "payments")),
    ):
        with pytest.raises(RulesConfigError, match="cannot read"):
            call()


def test_validate_message_tolerates_junk_fieldcheck_attrs():
    """FieldCheck is a plain public dataclass — a direct library caller can
    build a check whose enum/pattern/range/compiled_pattern attrs are junk
    typed (an int enum, bytes pattern, a str range, non-numeric bounds)
    bypassing the config loader. validate_message must skip those checks (or
    the junk bounds) instead of raw-crashing on `not in`/`.match`/`.items()`/
    raw < junk comparison."""
    rs = RuleSet(fields={"f": FieldCheck(name="f")})
    value = {"f": "x"}

    def run(check, raw):
        msg = Message(topic="t", partition=0, offset=0, key=b"k",
                      value={"f": raw})
        return validate_message(RuleSet(fields={"f": check}), msg)

    # junk enum attrs (non-membership containers) — skipped, no crash
    for enum in (5, "abc", {"a": 1}, None):
        assert run(FieldCheck(name="f", enum=enum), "a") == []
    # clean enum still validated
    assert run(FieldCheck(name="f", enum=["a"]), "b")  # 1 violation
    # junk compiled_pattern attrs — skipped, no crash
    for pat in (b"a", 55, "a", None):
        assert run(FieldCheck(name="f", compiled_pattern=pat), "a") == []
    # clean compiled pattern still validated
    assert run(FieldCheck(name="f", compiled_pattern=__import__("re").compile("^a$")), "b")
    # junk range attrs / junk threshold values — skipped, no crash
    for rng in ("abc", [1], None, {0: 1}, {"minimum": {1}}, {"minimum": "0"},
                {"maximum": True}):
        assert run(FieldCheck(name="f", range=rng), 3) == []
    # numeric threshold (even inf) is a real bound — range check runs normally
    assert run(FieldCheck(name="f", range={"minimum": float("inf")}), 3)  # 1 violation
    # clean range still validated
    assert run(FieldCheck(name="f", range={"maximum": 1}), 3)
    # junk required/non_null/type attrs — never crash (falsy junk is
    # disabled; truthy junk is enabled and only exercises the check)
    assert run(FieldCheck(name="f", required="yes"), "x") == []
    assert run(FieldCheck(name="f", required=[]), "x") == []
    assert run(FieldCheck(name="f", non_null=[]), None) == []
    run(FieldCheck(name="f", non_null=[1]), None)  # truthy junk fires — no crash
    run(FieldCheck(name="f", type=b"string"), "x")  # truthy junk fires — no crash
    run(FieldCheck(name="f", type=42), "x")  # truthy junk fires — no crash
    assert run(FieldCheck(name="f", type=None), "x") == []  # no type = no check
    # junk check names never crash lookup
    assert run(FieldCheck(name="g"), "x") == []
    assert run(FieldCheck(name=None), "x") == []


def test_validate_message_tolerates_junk_raw_values():
    """Junk raw values (sets, dicts, lists, NaN/inf, bytearray, memoryview,
    huge ints, arbitrary objects) under clean checks must not crash the
    enum-membership / pattern / range comparison paths."""
    rs = _rs()
    spec = {
        "amount_cents": {"type": "integer", "non_null": True,
                         "range": {"minimum": 0, "maximum": 100000}},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "status": {"type": "string", "enum": ["ACCEPTED", "PENDING", "REJECTED"]},
        "payment_id": {"type": "string"},
        "fee_cents": {"type": "integer", "non_null": True, "range": {"minimum": 0}},
    }
    rs = build_ruleset({"version": 1, "topic": "payments", "fields": spec})
    junk_values = ({1, 2}, {"a": 1}, ["a"], float("nan"), float("inf"),
                   b"x", bytearray(b"ab"), memoryview(b"ab"), 2**400,
                   10**400, object(), None)
    fields = ["amount_cents", "currency", "status", "payment_id", "fee_cents"]
    for field in fields:
        for raw in junk_values:
            msg = Message(topic="payments", partition=0, offset=0, key=b"k",
                          value={field: raw})
            validate_message(rs, msg)  # must not raise


def test_validate_message_junk_msg_attrs_no_traceback():
    """Message partition/offset/key junk values (None, str, list, dict, float,
    bytes, huge ints) must not crash validate_message or the resulting
    Violation.to_dict serialization."""
    rs = _rs()
    spec = {
        "amount_cents": {"type": "integer", "non_null": True,
                         "range": {"minimum": 0, "maximum": 100000}},
    }
    rs = build_ruleset({"version": 1, "topic": "payments", "fields": spec})
    for key in ("partition", "offset"):
        for val in (None, "x", [], {}, 1.5, b"x", 2**400):
            msg = Message(topic="payments", partition=0, offset=0, key=b"k",
                          value={"amount_cents": -1})
            setattr(msg, key, val)
            for v in validate_message(rs, msg):
                v.to_dict()  # must not raise
    for val in (b"k", None, {"k": 1}, [1], 42, 1.5):
        msg = Message(topic="payments", partition=0, offset=0, key=val,
                      value={"amount_cents": -1})
        for v in validate_message(rs, msg):
            v.to_dict()  # must not raise
