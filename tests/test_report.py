"""P2 — governance report + export tests."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from kgov.report import (
    GovernanceReport,
    report_to_csv,
    build_governance_report,
)
from kgov.rules import (
    Message,
    RuleSet,
    build_ruleset,
    validate_fixture_file,
    iter_messages_from_json,
    load_rules_file,
)

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"

NOW = datetime(2026, 11, 14, 22, 0, 0, tzinfo=timezone.utc)


def test_build_report_from_fixtures():
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        registry_fixture=FIXTURES / "registry" / "registry.json",
        groups_fixture=FIXTURES / "subject_groups" / "consumer_groups.json",
        now=NOW,
    )
    assert rep.total_topics == 3
    d = {t.name: t for t in rep.topics}
    assert d["payments"].owner == "payments-team"
    assert d["payments"].schema_version == 2
    assert d["payments"].schema_state == "COMPATIBLE"
    assert d["payments"].tags == ["core", "critical"]


def test_report_staleness():
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        groups_fixture=FIXTURES / "subject_groups" / "consumer_groups.json",
        now=NOW,
    )
    d = {t.name: t for t in rep.topics}
    # payments consumed 15h ago -> fresh
    assert d["payments"].stale is False
    # orders last consumed 2026-09-20 -> stale
    assert d["orders"].stale is True
    # inventory last produced 2026-10-01 but consumed 2026-11-13 -> fresh
    assert d["inventory-events"].stale is False


def test_report_violation_count_accumulated():
    rs = load_rules_file(FIXTURES / "rules" / "rules_scale.yaml")
    violations = validate_fixture_file(rs, FIXTURES / "messages" / "payments_messages.json")
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        groups_fixture=FIXTURES / "subject_groups" / "consumer_groups.json",
        rule_violations=violations,
        now=NOW,
    )
    payments = next(t for t in rep.topics if t.name == "payments")
    assert payments.violation_count == len(violations)
    assert payments.violation_count >= 5


def test_report_to_dict_json_serializable():
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        now=NOW,
    )
    rep.to_json()  # must not raise


def test_report_to_csv():
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        now=NOW,
    )
    csv_text = report_to_csv(rep)
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert len(rows) == 3
    assert rows[0]["name"] == "payments"
    assert "schema_subject" in rows[0]
    assert "violation_count" in rows[0]


def test_registry_breaking_state_surface():
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        registry_fixture=FIXTURES / "registry" / "registry.json",
        now=NOW,
    )
    orders = next(t for t in rep.topics if t.name == "orders")
    assert orders.schema_state == "BREAKING"


def test_topic_without_name_raises_value_error():
    """A malformed topics entry must raise a clean error, not KeyError."""
    with pytest.raises(ValueError, match="'name'"):
        build_governance_report(
            topics_fixture=[{"partitions": 4}],
            now=NOW,
        )


def test_non_mapping_group_entry_is_ignored():
    """Non-dict consumer-group entries must not crash the report build."""
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        groups_fixture={"groups": {"payments": "not-a-dict"}},
        now=NOW,
    )
    payments = next(t for t in rep.topics if t.name == "payments")
    assert payments.last_consumed_at is None
    assert payments.stale is False


def test_groups_list_with_non_dict_entries_is_ignored():
    """A list-form groups fixture with junk (non-dict) entries must not crash
    when normalized to the keyed lookup map."""
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        groups_fixture={"groups": ["not-a-dict", 42, {"group": "orders",
                                                      "last_consumed_at": "2026-01-01T00:00:00Z"}]},
        now=NOW,
    )
    orders = next(t for t in rep.topics if t.name == "orders")
    assert orders.last_consumed_at is not None
    payments = next(t for t in rep.topics if t.name == "payments")
    assert payments.last_consumed_at is None


def test_non_mapping_registry_is_ignored():
    """A registry fixture that is not a mapping (e.g. a bare list) must be
    ignored, not crash when a topic references a schema subject."""
    rep = build_governance_report(
        topics_fixture=[{"name": "orders", "schema_subject": "orders-value"}],
        registry_fixture={"registry": ["not-a-dict"]},
        now=NOW,
    )
    orders = rep.topics[0]
    assert orders.schema_state == "NONE"
    assert orders.schema_version is None


def test_registry_subjects_as_list_is_ignored():
    """A registry whose 'subjects' entry is a list (not a mapping) must be
    ignored instead of crashing on .get() when a topic references a subject."""
    rep = build_governance_report(
        topics_fixture=[{"name": "payments", "schema_subject": "payments-value"}],
        registry_fixture={"subjects": [{"version": 1, "compatibility": "COMPATIBLE"}]},
        now=NOW,
    )
    payments = rep.topics[0]
    assert payments.schema_state == "NONE"
    assert payments.schema_version is None


def test_topics_fixture_non_list_raises_value_error(tmp_path):
    """A topics fixture that does not resolve to a list (e.g. null) must fail
    with a clean ValueError rather than a TypeError on iteration."""
    fixture = tmp_path / "topics_null.json"
    fixture.write_text(json.dumps({"topics": None}), encoding="utf-8")
    with pytest.raises(ValueError, match="resolve to a list of topic mappings"):
        build_governance_report(
            topics_fixture=fixture,
            now=NOW,
        )


def test_non_utf8_fixture_raises_value_error(tmp_path):
    """A fixture file with invalid UTF-8 must fail with a clean ValueError,
    not a raw UnicodeDecodeError."""
    fixture = tmp_path / "topics_bad.json"
    fixture.write_bytes(b'\xff\xfe{"topics": []}')
    with pytest.raises(ValueError, match="not valid UTF-8"):
        build_governance_report(topics_fixture=fixture, now=NOW)


def test_junk_violation_topic_values_ignored():
    """Violations whose 'topic' value is not a string (e.g. a list) must be
    ignored when accumulating per-topic counts, not crash on dict keying."""
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        rule_violations=[
            {"topic": "payments"},
            {"topic": ["payments"]},   # unhashable -> would crash before fix
            {"topic": 42},
            {"topic": None},
            {"topic": ""},
        ],
        now=NOW,
    )
    payments = next(t for t in rep.topics if t.name == "payments")
    assert payments.violation_count == 1


def test_registry_junk_version_entries_ignored():
    """A registry subject version list containing non-dict junk (even as the
    last/latest entry) must not crash the report build."""
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        registry_fixture={"subjects": {"payments-value": [
            {"version": 2, "compatibility": "COMPATIBLE"}, "garbage", 42,
        ]}},
        now=NOW,
    )
    payments = next(t for t in rep.topics if t.name == "payments")
    assert payments.schema_version == 2
    assert payments.schema_state == "COMPATIBLE"

    rep2 = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        registry_fixture={"subjects": {"payments-value": ["garbage"]}},
        now=NOW,
    )
    payments2 = next(t for t in rep2.topics if t.name == "payments")
    assert payments2.schema_version is None
    assert payments2.schema_state == "NONE"
