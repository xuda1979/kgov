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


def test_unknown_type_rejected():
    with pytest.raises(UnknownFieldTypeError):
        build_ruleset({"fields": {"x": {"type": "datetime"}}})


def test_unknown_field_key_rejected():
    with pytest.raises(RuleFieldConfigError):
        build_ruleset({"fields": {"x": {"type": "string", "bogus": 1}}})


def test_invalid_regex_rejected():
    with pytest.raises(RuleFieldConfigError):
        build_ruleset({"fields": {"x": {"pattern": "["}}})


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


def test_fixture_iter_messages():
    path = FIXTURES / "messages" / "payments_messages.json"
    msgs = list(iter_messages_from_json(path))
    assert len(msgs) == 6
    assert msgs[0].offset == 0
    assert msgs[0].key == "pay_001"
    assert msgs[3].offset == 3


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

    # messages 0 and 5 are clean
    assert 0 not in by_offset
    assert 5 not in by_offset
