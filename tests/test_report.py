"""P2 — governance report + export tests."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from kgov.report import (
    GovernanceReport,
    StalenessSettings,
    TopicState,
    report_to_csv,
    build_governance_report,
    build_topic_state,
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


def test_groups_list_with_non_string_keys_is_ignored():
    """A list-form groups entry whose topic/group key is non-string (e.g. a
    list) must be skipped, not crash on an unhashable dict key."""
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        groups_fixture={"groups": [
            {"topic": ["payments"], "last_consumed_at": "2026-11-13T00:00:00Z"},
            {"group": "payments", "last_consumed_at": "2026-11-13T00:00:00Z"},
        ]},
        now=NOW,
    )
    payments = next(t for t in rep.topics if t.name == "payments")
    assert payments.last_consumed_at == "2026-11-13T00:00:00+00:00"
    assert payments.stale is False


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


def test_registry_subject_non_list_value_is_ignored():
    """A registry subject whose version value is not a list (int/string/single
    dict) must not crash reversed()/iteration; no history is resolvable."""
    for version_value in (5, "garbage", None, {"version": 1}):
        rep = build_governance_report(
            topics_fixture=[{"name": "payments", "schema_subject": "payments-value"}],
            registry_fixture={"subjects": {"payments-value": version_value}},
            now=NOW,
        )
        payments = rep.topics[0]
        assert payments.schema_version is None
        assert payments.schema_state == "NONE"


def test_topic_non_string_schema_subject_ignored():
    """A topics entry whose schema_subject is not a string (list/int/dict/
    bytes/None) must not crash when looking up the registry subject or when
    serializing the row; no schema history is resolvable so the topic falls
    back to NONE and the junk value is not carried into the row."""
    for junk in (["payments-value", "x"], 42, {"s": 1}, None, "", b"a"):
        rep = build_governance_report(
            topics_fixture=[{"name": "payments", "schema_subject": junk}],
            registry_fixture={"subjects": {"payments-value": [
                {"version": 2, "compatibility": "COMPATIBLE"}]}},
            now=NOW,
        )
        payments = rep.topics[0]
        assert payments.schema_version is None
        assert payments.schema_state == "NONE"
        assert payments.schema_subject is None
        rep.to_json()  # row must stay JSON-serializable


def test_registry_bare_list_ignored():
    """A bare list passed directly as registry_fixture must be ignored (its
    type hint accepts list as resolved data), not crash on Path(list)."""
    rep = build_governance_report(
        topics_fixture=[{"name": "orders", "schema_subject": "orders-value"}],
        registry_fixture=[{"version": 1, "compatibility": "COMPATIBLE"}],
        now=NOW,
    )
    orders = rep.topics[0]
    assert orders.schema_state == "NONE"
    assert orders.schema_version is None


def test_groups_bare_list_no_crash():
    """A bare list passed directly as groups_fixture must not crash on
    Path(list); it is treated as list-form data (junk entries skipped, valid
    mapping entries honored)."""
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        groups_fixture=["not-a-dict", {"group": "orders", "last_consumed_at": "2026-01-01T00:00:00Z"}],
        now=NOW,
    )
    orders = next(t for t in rep.topics if t.name == "orders")
    assert orders.last_consumed_at == "2026-01-01T00:00:00+00:00"
    payments = next(t for t in rep.topics if t.name == "payments")
    assert payments.last_consumed_at is None


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


def test_tags_null_or_non_list_ignored():
    """A topics entry whose tags value is null (or a non-list) must not crash
    earlier non-list tags (e.g. an int) with TypeError."""
    rep = build_governance_report(
        topics_fixture=[
            {"name": "orders", "tags": None},
            {"name": "inventory-events", "tags": 5},
            {"name": "payments", "tags": ["core"]},
        ],
        now=NOW,
    )
    d = {t.name: t for t in rep.topics}
    assert d["orders"].tags == []
    assert d["inventory-events"].tags == []
    assert d["payments"].tags == ["core"]


def test_tags_with_junk_elements_kept_strings_only_and_csv_safe():
    """Junk tag elements (ints, dicts, ...) are dropped so the row model and
    the CSV export (which joins tags with '|') never crash on non-str items."""
    rep = build_governance_report(
        topics_fixture=[
            {"name": "payments", "tags": [1, {"junk": 1}, "core", None, "critical"]},
        ],
        now=NOW,
    )
    t = rep.topics[0]
    assert t.tags == ["core", "critical"]
    csv = report_to_csv(rep)
    assert "core|critical" in csv
    assert t.to_dict()["tags"] == ["core", "critical"]


def test_partitions_and_replication_junk_fall_back_to_defaults():
    """Missing/non-numeric partitions or replication_factor must not crash
    (e.g. int(None) / int([1])) and fall back to sensible defaults."""
    rep = build_governance_report(
        topics_fixture=[
            {"name": "a", "partitions": None, "replication_factor": [2]},
            {"name": "b", "partitions": 4, "replication_factor": "3"},
            {"name": "c", "partitions": True},
        ],
        now=NOW,
    )
    d = {t.name: t for t in rep.topics}
    assert d["a"].partitions == 1
    assert d["a"].replication_factor == 1
    assert d["b"].partitions == 4
    assert d["b"].replication_factor == 3
    # bool is technically an int subclass; the default path is the important part
    assert isinstance(d["c"].partitions, int)


def test_out_of_range_timestamps_treated_as_absent():
    """An int timestamp beyond the platform's time_t range (e.g. 1e20) must
    not crash the report with OverflowError; the value is treated as absent."""
    rep = build_governance_report(
        topics_fixture=[
            {"name": "a", "latest_message_at": 99999999999999999999999},
            {"name": "b", "latest_message_at": -99999999999999999999999},
        ],
        groups_fixture={"groups": {"c": {"last_consumed_at": 99999999999999999999999}}},
        now=NOW,
    )
    d = {t.name: t for t in rep.topics}
    assert d["a"].latest_message_at is None
    assert d["a"].stale is False
    assert d["b"].latest_message_at is None
    assert d["b"].stale is False


def test_registry_junk_compatibility_value_tolerated():
    """A version dict whose 'compatibility' is not a string (e.g. a list) must
    not crash the state-mapping lookup on an unhashable dict key; the topic
    falls back to a sane schema state instead of raising TypeError."""
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        registry_fixture={"subjects": {"payments-value": [
            {"version": 2, "compatibility": ["BACKWARD"]},   # unhashable -> would crash
            {"version": 3, "compatibility": {"BACKWARD": True}},
        ]}},
        now=NOW,
    )
    payments = next(t for t in rep.topics if t.name == "payments")
    assert payments.schema_version is not None
    assert isinstance(payments.schema_state, str)


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


def test_topic_non_string_name_raises_value_error():
    """A topics entry whose 'name' is a truthy non-string (list, dict, int,
    bytes, ...) must raise a clean ValueError, not a TypeError from an
    unhashable key in the groups/violations lookups."""
    for junk in ([1], {"k": 1}, 42, 2.5, True, b"payments"):
        with pytest.raises(ValueError, match="must be a string"):
            build_governance_report(
                topics_fixture=[{"name": junk, "owner": "team"}],
                now=NOW,
            )


def test_build_topic_state_non_dict_groups_tolerated():
    """build_topic_state with a non-mapping groups container (a bare int,
    list, ...) must not crash on .get; the topic falls back to no consumer
    state rather than raising AttributeError."""
    for junk_groups in (1, "x", [{"topic": "payments"}], None, True):
        ts = build_topic_state(
            {"name": "payments", "owner": "team"}, {}, junk_groups, {}, now=NOW,
        )
        assert ts.name == "payments"
        assert ts.last_consumed_at is None
        assert ts.stale is False


def test_fixture_junk_path_raises_value_error():
    """A fixture argument that is not a file path (int, bytes, dict, list,
    None, tuple) must raise a clean ValueError rather than a raw TypeError
    from pathlib or the open() on bytes."""
    for junk in (1, b"b", {"k": 1}, None, (1,), 2.5, True):
        with pytest.raises(ValueError, match="file path"):
            build_governance_report(topics_fixture=junk, now=NOW)


def test_rule_violations_non_list_ignored():
    """A rule_violations value that is not a list (a bare int, mapping, ...)
    must not crash the report build; counts are simply empty."""
    rep = build_governance_report(
        topics_fixture=FIXTURES / "topics" / "topics.json",
        rule_violations=7,
        now=NOW,
    )
    assert all(t.violation_count == 0 for t in rep.topics)

    rep2 = build_governance_report(
        topics_fixture=[{"name": "payments"}],
        rule_violations=[{"topic": "payments"}, 5, None],
        now=NOW,
    )
    assert rep2.topics[0].violation_count == 1


def test_junk_owner_values_fall_back_to_unknown():
    """A topic 'owner' value that is not a string (bytes, an int, a list, a
    dict, booleans, ...) must not leak into the row model where it would
    crash to_json/CSV serialization (bytes is not JSON-serializable); it
    falls back to 'unknown' instead."""
    for junk in (b"team", 42, 2.5, ["core"], {"k": 1}, True, False):
        rep = build_governance_report(
            topics_fixture=[{"name": "payments", "owner": junk}],
            now=NOW,
        )
        ts = rep.topics[0]
        assert ts.owner == "unknown"
        json.dumps(ts.to_dict())  # row model must stay JSON-safe
        report_to_csv(rep)


def test_junk_registry_version_value_dropped():
    """A registry version entry whose 'version' value is not JSON-safe (e.g.
    bytes) must not leak into schema_version where it would crash to_json;
    the junk value is dropped to None while the row stays export-safe."""
    rep = build_governance_report(
        topics_fixture=[{"name": "payments", "schema_subject": "payments-value"}],
        registry_fixture={"subjects": {"payments-value": [
            {"version": b"2", "compatibility": "COMPATIBLE"},
        ]}},
        now=NOW,
    )
    ts = rep.topics[0]
    assert ts.schema_version is None
    assert ts.schema_state == "COMPATIBLE"
    json.dumps(ts.to_dict())
    report_to_csv(rep)


def test_junk_now_arg_falls_back_to_current_time():
    """A 'now' argument that is not a datetime (an int, bytes, a string, ...)
    must not crash on .tzinfo; the report falls back to the current instant
    instead of raising a raw AttributeError."""
    for junk in (1740000000, "2026-11-14T22:00:00+00:00", b"now", [1], {"k": 1}):
        rep = build_governance_report(
            topics_fixture=[{"name": "payments"}],
            now=junk,
        )
        assert rep.topics[0].name == "payments"
        ts = build_topic_state({"name": "payments"}, {}, {}, {}, now=junk)
        assert ts.name == "payments"


def test_governance_report_tolerates_junk_topics_entries():
    """Junk entries in GovernanceReport.topics (a direct library caller or
    test double constructing it by hand) are not TopicState rows — they must
    be dropped from to_dict/to_json instead of raw-crashing on .to_dict()."""
    for entry in (42, None, "x", object(), [1], {"a": 1}, (1, 2)):
        rep = GovernanceReport(topics=[entry])
        d = rep.to_dict()
        assert d["topics"] == []
        json.dumps(rep.to_json())  # to_json output is a JSON string; parse-proof
    # empty report stays fine
    rep = GovernanceReport()
    json.loads(rep.to_json())


def test_governance_report_to_json_tolerates_junk_topic_state_fields():
    """A hand-constructed TopicState with junk field values (bytes, an
    object, ...) must not crash to_json; unknown leaves stringify via the
    json_default hook while valid output stays byte-identical on the
    build_topic_state path."""
    for junk in (
        TopicState(name="t", tags=[b"x"]),
        TopicState(name="t", schema_version=bytes(1)),
        TopicState(name="t", schema_state=object()),
    ):
        rep = GovernanceReport(topics=[junk])
        json.loads(rep.to_json())  # dumpable, no raw TypeError
    # valid path unchanged (byte-identical output shape)
    rep = build_governance_report(
        topics_fixture=[{"name": "payments", "owner": "team"}], now=NOW,
    )
    out = json.loads(rep.to_json())
    assert out["topics"][0]["tags"] == []
    assert out["topics"][0]["owner"] == "team"


def test_junk_grace_seconds_fall_back_to_non_stale():
    """Hand-built StalenessSettings with junk-typed grace seconds (strings,
    bytes, None, lists, dicts) must not crash the total_seconds comparison;
    stale falls back to the non-stale default, and clean numeric settings keep
    working unchanged (both consumer and producer branches)."""
    t_no_consumer = {"name": "t", "latest_message_at": "2026-01-01T00:00:00Z"}
    t_consumer = {"name": "t", "latest_message_at": "2026-01-01T00:00:00Z"}
    groups_consumed = {"t": {"last_consumed_at": "2026-01-01T00:00:00Z"}}

    # consumer branch: junk consumer_grace_seconds never crashes
    # (non-numeric junk falls back to non-stale)
    for gv in ("999", None, b"x", [5], {}):
        st = StalenessSettings(consumer_grace_seconds=gv)
        ts = build_topic_state(t_consumer, {}, groups_consumed, {}, now=NOW, settings=st)
        assert ts.stale is False
    # (numeric values are usable thresholds; only no-traceback is asserted)
    for gv in (-5, float("inf"), float("nan"), 7.5):
        st = StalenessSettings(consumer_grace_seconds=gv)
        build_topic_state(t_consumer, {}, groups_consumed, {}, now=NOW, settings=st)
    # producer branch: junk producer_grace_seconds never crashes
    for pv in ("999", None, b"x", [5], {}):
        st = StalenessSettings(producer_grace_seconds=pv)
        ts = build_topic_state(t_no_consumer, {}, {}, {}, now=NOW, settings=st)
        assert ts.stale is False
    for pv in (-5, float("nan"), 7.5):
        st = StalenessSettings(producer_grace_seconds=pv)
        build_topic_state(t_no_consumer, {}, {}, {}, now=NOW, settings=st)
    # a numeric inf producer grace is usable and makes nothing stale
    st = StalenessSettings(producer_grace_seconds=float("inf"))
    assert build_topic_state(t_no_consumer, {}, {}, {}, now=NOW, settings=st).stale is False
    # clean numeric settings still drive staleness in both branches
    st = StalenessSettings(consumer_grace_seconds=60)
    assert build_topic_state(t_consumer, {}, groups_consumed, {}, now=NOW,
                             settings=st).stale is True
    st = StalenessSettings(producer_grace_seconds=60)
    assert build_topic_state(t_no_consumer, {}, {}, {}, now=NOW,
                             settings=st).stale is True


def test_junk_rule_topic_violations_container_ignored():
    """A junk-typed rule_topic_violations container (None, a str, bytes, a
    list) passed directly to build_topic_state is not a {name: count} mapping
    — it must be ignored (violation_count stays 0), not crash on .get()."""
    topic = {"name": "t"}
    for bad in (None, "x", b"x", [{"t"}], ["t"], {("t",): 2}, 42):
        ts = build_topic_state(topic, {}, {}, bad)
        assert ts.violation_count == 0
    # a valid counts mapping still resolves
    ts = build_topic_state(topic, {}, {}, {"t": 3})
    assert ts.violation_count == 3


def test_inf_partitions_and_replication_fall_back_to_defaults():
    """float('inf') partitions/replication_factor must not leak a raw
    OverflowError from int() (which the old TypeError/ValueError catch did not
    cover); both fall back to the default instead."""
    for p, r in ((float("inf"), 1), (1, float("inf")), (float("inf"), float("inf"))):
        ts = build_topic_state({"name": "t", "partitions": p,
                                "replication_factor": r}, {}, {}, {})
        assert ts.partitions == 1
        assert ts.replication_factor == 1
    rep = build_governance_report(
        topics_fixture=[{"name": "a", "partitions": float("inf"),
                         "replication_factor": float("inf")}],
        now=NOW,
    )
    assert rep.topics[0].partitions == 1
    assert rep.topics[0].replication_factor == 1


def test_report_to_csv_tolerates_junk_report_arg():
    """A junk report argument to report_to_csv (None, an int, a string, a
    list, a dict, an object without 'topics', garbage bytes) carries no rows —
    it must fail closed to a header-only CSV, not raw-crash on attribute
    access. A valid report still exports normally."""
    header = "name,owner,tags"
    for bad in (None, 42, 2.5, "x", b"x", [1], {"a": 1}, (1, 2), set(), object()):
        out = report_to_csv(bad)
        assert isinstance(out, str)
        assert "name,owner,tags" in out  # header written, no crash
    # a topics container that is not a list/tuple (a dict, None on a
    # lookalike) is also junk — header-only
    for container in ({"a": TopicState(name="t")}, None):
        lookalike = type("Lookalike", (), {"topics": container})()
        out = report_to_csv(lookalike)
        assert "name,owner,tags" in out
    # valid path unchanged
    rep = GovernanceReport(topics=[TopicState(name="payments", tags=["core"])])
    out = report_to_csv(rep)
    rows = list(csv.DictReader(io.StringIO(out)))
    assert rows[0]["name"] == "payments"
    assert rows[0]["tags"] == "core"


def test_report_to_csv_tolerates_junk_topics_entries():
    """Junk entries in GovernanceReport.topics (ints, None, lists, dicts,
    objects, hand-built lookalikes without to_dict) are not TopicState rows —
    report_to_csv must skip them instead of raw-crashing on .to_dict(). A
    valid row next to junk still exports."""
    for entry in (42, None, 2.5, "x", b"x", [1], {"a": 1}, (1, 2), set(), object()):
        out = report_to_csv(GovernanceReport(topics=[entry]))
        assert "name,owner,tags" in out  # header-only, no crash
    # no to_dict attribute at all
    class NoDict:
        tags = ["a"]
    out = report_to_csv(GovernanceReport(topics=[NoDict()]))
    assert "name,owner,tags" in out
    # a valid row mixed with junk still exports
    rep = GovernanceReport(topics=[TopicState(name="ok"), 42, None, object()])
    rows = list(csv.DictReader(io.StringIO(report_to_csv(rep))))
    assert len(rows) == 1
    assert rows[0]["name"] == "ok"


def test_report_to_csv_tolerates_junk_tags_values():
    """Junk tags values on a hand-built TopicState (non-list, non-str list
    elements, bytes, None, nested lists) must not crash the '|'-join —
    the tag cell falls back to '' (or the str-only joins) instead of raising
    a raw TypeError. Valid string tags still join unchanged."""
    for tags in (None, 42, 2.5, "x", b"x", {"a": 1}, [1], (1, 2), {1, 2},
                 [b"x"], [None], ["ok", b"y"], ["ok", None], [1, "a"],
                 [["x"]], [{"a": 1}], bytearray(b"x"), memoryview(b"ab")):
        out = report_to_csv(GovernanceReport(topics=[TopicState(name="t", tags=tags)]))
        assert "name,owner,tags" in out  # never raises
    # valid tags still join with '|'
    rep = GovernanceReport(topics=[TopicState(name="t", tags=["core", "critical"])])
    rows = list(csv.DictReader(io.StringIO(report_to_csv(rep))))
    assert rows[0]["tags"] == "core|critical"


def test_report_to_csv_tolerates_unpicklable_topic_fields():
    """TopicState.to_dict must not deep-copy values (asdict() did) — a
    memoryview tags value is un-picklable and would raise raw TypeError from
    copy.deepcopy mid-export; to_dict/export must keep going."""
    ts = TopicState(name="t", tags=[memoryview(b"ab")])
    d = ts.to_dict()  # must not raise on deepcopy
    assert "tags" in d
    out = report_to_csv(GovernanceReport(topics=[ts]))
    assert "name,owner,tags" in out


def test_model_diff_report_generated_at_junk_export_safe():
    """A GovernanceReport built by hand whose topics are real TopicState rows
    still exports to CSV with stale/violation fields numeric, and the
    to_dict/to_json path stays JSON-safe (bytes owner falls back via the
    is-string guard only at build time; hand-built junk values stay stringy)."""
    rep = GovernanceReport(
        total_topics=1,
        topics=[TopicState(name="t", tags=["a"], violation_count=2)],
    )
    rows = list(csv.DictReader(io.StringIO(report_to_csv(rep))))
    assert rows[0]["violation_count"] == "2"
    assert rows[0]["tags"] == "a"
    json.loads(rep.to_json())
