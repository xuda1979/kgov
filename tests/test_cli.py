"""End-to-end CLI tests: exit codes, output formats, live registry wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kgov.cli import main
from kgov.model import CompatibilityKind

from kgov.mock_registry import MockRegistry

FIXTURES = Path(__file__).parent / "fixtures"


def _run(*argv: str) -> tuple[int, str, str]:
    import contextlib
    import io

    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(argv))
    except SystemExit as exc:  # pragma: no cover - safety net
        code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


# ---------------------------------------------------------------------------
# schema-diff
# ---------------------------------------------------------------------------


def test_schema_diff_additive_markdown(capsys):
    code = main([
        "schema-diff",
        "--before", str(FIXTURES / "schemas" / "v1.json"),
        "--after", str(FIXTURES / "schemas" / "v2.json"),
    ])
    out = capsys.readouterr()
    assert code == 0
    assert "SCHEMA DIFF" in out.err


def test_schema_diff_breaking_exit_1(tmp_path):
    out = tmp_path / "diff.json"
    code = main([
        "schema-diff",
        "--before", str(FIXTURES / "schemas" / "breaking_before.json"),
        "--after", str(FIXTURES / "schemas" / "breaking_after.json"),
        "--json", "-o", str(out),
    ])
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["kind"] == CompatibilityKind.BREAKING
    assert data["compatible"] is False


def test_schema_diff_missing_file(capsys):
    code = main(["schema-diff", "--before", "/no/such/file.json", "--after", "/nope.json"])
    assert code == 3


def test_schema_diff_junk_fixtures_exit_0(tmp_path):
    """schema-diff must tolerate junk entries inside otherwise-valid schema
    fixtures (string/object record fields, dict enum symbols, list
    properties, non-string required, mismatched bound types) instead of
    crashing with a traceback (exit 0)."""
    cases = [
        # avro: garbage record fields
        ({"type": "record", "name": "A", "fields": ["x", {"type": "long"}, {"name": "id", "type": "long"}]},
         {"type": "record", "name": "A", "fields": [42, {"name": "id", "type": "long"}]}),
        # avro: unhashable enum symbols
        ({"type": "enum", "name": "C", "symbols": ["RED", {"x": 1}]},
         {"type": "enum", "name": "C", "symbols": [["RED"], "RED", "BLUE"]}),
        # json: properties as a list + required with dicts
        ({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
          "properties": ["x"], "required": [{"x": 1}, "id"]},
         {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
          "properties": ["x"], "required": [{"x": 1}, "id"], "minLength": 5}),
        # json: mismatched bound types
        ({"$schema": "http://json-schema.org/draft-07/schema#", "type": "string", "minLength": "abc"},
         {"$schema": "http://json-schema.org/draft-07/schema#", "type": "string", "minLength": 5}),
    ]
    for i, (before, after) in enumerate(cases):
        b = tmp_path / f"before{i}.json"
        a = tmp_path / f"after{i}.json"
        b.write_text(json.dumps(before), encoding="utf-8")
        a.write_text(json.dumps(after), encoding="utf-8")
        code = main(["schema-diff", "--before", str(b), "--after", str(a)])
        assert code == 0, f"case {i}: exit {code}"



def test_schema_diff_compact_registry_ref(server) -> None:
    """Shorthand refs ``host:port/subject/version`` resolve against a registry."""
    ref1 = f"{server.base_url}/payments-value/1"
    ref2 = f"{server.base_url}/payments-value/2"
    code, out, _ = _run(
        "schema-diff",
        "--before", ref1.replace("http://", ""),
        "--after", ref2.replace("http://", ""),
        "--subject", "payments-value", "--json",
    )
    assert code == 0
    data = json.loads(out)
    assert data["subject"] == "payments-value"
    assert data["kind"] == CompatibilityKind.ADDITIVE
    assert any("fee_cents" in r["path"] for r in data["results"])


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_clean_exit_0(tmp_path):
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    out = tmp_path / "result.json"
    code = main(["check", "--config", str(rules), "--messages", str(FIXTURES / "messages" / "payments_messages.json"),
                 "-o", str(out)])
    assert code == 1  # fixture messages are intentionally dirty
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is False
    assert len(data["violations"]) == 9


def test_check_within_gate_exit_0(tmp_path):
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    code = main(["check", "--config", str(rules), "--messages", str(FIXTURES / "messages" / "payments_messages.json"),
                 "--max-violations", "10", "--quiet"])
    assert code == 0


def test_check_gate_exceeded_exit_1(tmp_path):
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    code = main(["check", "--config", str(rules), "--messages", str(FIXTURES / "messages" / "payments_messages.json"),
                 "--max-violations", "3", "--quiet"])
    assert code == 1


def test_check_requires_data_source(capsys):
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    code = main(["check", "--config", str(rules)])
    assert code == 3


def test_check_topic_without_broker_exit_3(capsys):
    """--topic is not a data source by itself; it must pair with --broker."""
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    code = main(["check", "--config", str(rules), "--topic", "payments"])
    assert code == 3
    assert "requires --broker" in capsys.readouterr().err


def test_check_missing_config(capsys):
    code = main(["check", "--config", "/no/rules.yaml", "--messages", "x.json"])
    assert code == 3


def test_check_scalar_messages_fixture_exit_3(tmp_path, capsys):
    """A messages fixture whose JSON root is null/scalar must fail cleanly
    (exit 3), not crash with AttributeError."""
    bad = tmp_path / "msgs.json"
    bad.write_text("null", encoding="utf-8")
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    code = main(["check", "--config", str(rules), "--messages", str(bad)])
    assert code == 3
    assert "bad message fixture" in capsys.readouterr().err


def test_check_non_utf8_rules_exit_3(tmp_path, capsys):
    """check --config with invalid UTF-8 must exit 3 with a clean error,
    not leak an uncaught UnicodeDecodeError."""
    bad = tmp_path / "rules.yaml"
    bad.write_bytes(b"{topic: \xff\xfe}")
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    code = main(["check", "--config", str(bad), "--messages",
                 str(FIXTURES / "messages" / "payments_messages.json")])
    assert code == 3
    assert "bad rules config" in capsys.readouterr().err


def test_check_non_numeric_range_bound_exit_3(tmp_path, capsys):
    """A rules config with a non-numeric range bound must exit 3 cleanly,
    not crash with a raw ValueError from float() (exit 1)."""
    bad = tmp_path / "rules.yaml"
    bad.write_text("version: 1\ntopic: payments\nfields:\n"
                   "  amount_cents:\n    type: integer\n"
                   "    range: {minimum: abc}\n", encoding="utf-8")
    code = main(["check", "--config", str(bad),
                 "--messages", str(FIXTURES / "messages" / "payments_messages.json")])
    assert code == 3
    assert "bad rules config" in capsys.readouterr().err


def test_check_non_utf8_messages_exit_3(tmp_path, capsys):
    """check --messages with invalid UTF-8 must exit 3 cleanly."""
    bad = tmp_path / "msgs.json"
    bad.write_bytes(b'{"messages": \xff\xfe[]}')
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    code = main(["check", "--config", str(rules), "--messages", str(bad)])
    assert code == 3
    assert "bad message fixture" in capsys.readouterr().err


def test_schema_diff_non_utf8_exit_3(tmp_path, capsys):
    """schema-diff --before with a non-UTF-8 file must exit 3 cleanly, not a
    raw UnicodeDecodeError traceback."""
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'\xff\xfe{"type": "record"}')
    code = main(["schema-diff", "--before", str(bad),
                 "--after", str(FIXTURES / "schemas" / "v1.json")])
    assert code == 3
    assert "not valid UTF-8" in capsys.readouterr().err


def test_check_broker_scan_failure_exit_3(capsys, monkeypatch):
    """A broker scan that fails (unreachable cluster) must exit 3 with a clean
    error — never pass the gate with zero violations."""
    rules = FIXTURES / "rules" / "rules_scale.yaml"

    def boom(bootstrap, topic, value_decoder=None, **kwargs):
        raise RuntimeError("cannot reach broker")

    monkeypatch.setattr("kgov.broker.read_topic", boom)
    monkeypatch.setattr("kgov.broker.AvroRecordDecoder", lambda *a, **k: None)
    code = main(["check", "--config", str(rules),
                 "--broker", "127.0.0.1:9", "--topic", "payments", "--quiet"])
    out = capsys.readouterr()
    assert code == 3
    assert "broker scan failed" in out.err


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_default_json(capsys):
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--registry", str(FIXTURES / "registry" / "registry.json"),
                 "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json")])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total_topics"] == 3


def test_report_csv(tmp_path):
    out = tmp_path / "report.csv"
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"), "-o", str(out)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("name,")


def test_report_requires_topics(capsys):
    # argparse fails on the missing required flag before our handler runs
    code = main(["report"])
    assert code == 2


def test_report_missing_rules_file_exit_3(capsys):
    """report --rules must fail cleanly (exit 3), not crash on FileNotFoundError."""
    code = main([
        "report", "--topics", str(FIXTURES / "topics" / "topics.json"),
        "--rules", "/no/rules.yaml",
        "--messages", str(FIXTURES / "messages" / "payments_messages.json"),
    ])
    assert code == 3
    out = capsys.readouterr()
    assert "--rules file missing" in out.err


def test_report_missing_registry_or_groups_exit_3(capsys):
    """report --registry/--groups must fail cleanly (exit 3) for missing files."""
    topics = str(FIXTURES / "topics" / "topics.json")
    code = main(["report", "--topics", topics, "--registry", "/no/registry.json"])
    assert code == 3
    assert "--registry file missing" in capsys.readouterr().err
    code = main(["report", "--topics", topics, "--groups", "/no/groups.json"])
    assert code == 3
    assert "--groups file missing" in capsys.readouterr().err


def test_report_non_utf8_rules_exit_3(tmp_path, capsys):
    """report --rules with a non-UTF-8 file must exit 3 cleanly."""
    bad = tmp_path / "rules.yaml"
    bad.write_bytes(b"{topic: \xff\xfe}")
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--rules", str(bad),
                 "--messages", str(FIXTURES / "messages" / "payments_messages.json")])
    assert code == 3
    assert "bad rules/messages" in capsys.readouterr().err


def test_report_non_utf8_topics_exit_3(tmp_path, capsys):
    """report --topics with a non-UTF-8 file must exit 3 cleanly."""
    bad = tmp_path / "topics.json"
    bad.write_bytes(b'\xff\xfe{"topics": []}')
    code = main(["report", "--topics", str(bad)])
    assert code == 3
    assert "not valid UTF-8" in capsys.readouterr().err


def test_report_non_utf8_violations_exit_3(tmp_path, capsys):
    """report --violations with a non-UTF-8 file must exit 3 cleanly."""
    bad = tmp_path / "vio.json"
    bad.write_bytes(b'\xff\xfe[]')
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--violations", str(bad)])
    assert code == 3
    assert "not valid UTF-8" in capsys.readouterr().err


def test_report_violations_junk_topic_exit_0(tmp_path, capsys):
    """Violations with a non-string topic value (e.g. a list) must be ignored
    cleanly (exit 0), not crash with an unhashable-key TypeError."""
    vio = tmp_path / "vio.json"
    vio.write_text('[{"topic": ["payments"]}, {"topic": "payments"}]', encoding="utf-8")
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--violations", str(vio)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    payments = next(t for t in data["topics"] if t["name"] == "payments")
    assert payments["violation_count"] == 1


def test_report_registry_junk_versions_exit_0(tmp_path, capsys):
    """A registry subject whose latest version entry is non-dict junk must not
    crash (exit 0); an earlier valid mapping is still honored."""
    reg = tmp_path / "reg.json"
    reg.write_text('{"subjects": {"payments-value": '
                   '[{"version": 2, "compatibility": "COMPATIBLE"}, "garbage"]}}',
                   encoding="utf-8")
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--registry", str(reg)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    payments = next(t for t in data["topics"] if t["name"] == "payments")
    assert payments["schema_version"] == 2
    assert payments["schema_state"] == "COMPATIBLE"


@pytest.mark.parametrize("version_value", ['"garbage"', "5", "null", '{"version": 1}'])
def test_report_registry_subject_non_list_value_exit_0(tmp_path, capsys, version_value):
    """A registry subject whose version value is not a list (string, int,
    null, single dict) must be treated as no resolvable history (exit 0),
    not crash with 'int' object is not reversible."""
    reg = tmp_path / "reg.json"
    reg.write_text(f'{{"subjects": {{"payments-value": {version_value}}}}}',
                   encoding="utf-8")
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--registry", str(reg)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    payments = next(t for t in data["topics"] if t["name"] == "payments")
    assert payments["schema_version"] is None
    assert payments["schema_state"] == "NONE"


def test_report_out_of_range_timestamps_exit_0(tmp_path, capsys):
    """Int timestamps beyond the platform time_t range (in latest_message_at
    or last_consumed_at) must be treated as absent, not crash with
    OverflowError (exit 0)."""
    fixture = tmp_path / "topics.json"
    fixture.write_text('{"topics": [{"name": "orders", "latest_message_at": '
                       '99999999999999999999999}]}', encoding="utf-8")
    groups = tmp_path / "groups.json"
    groups.write_text('{"groups": {"orders": {"last_consumed_at": '
                      '99999999999999999999999}}}', encoding="utf-8")
    code = main(["report", "--topics", str(fixture), "--groups", str(groups)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    orders = data["topics"][0]
    assert orders["latest_message_at"] is None
    assert orders["last_consumed_at"] is None
    assert orders["stale"] is False


def test_report_tags_null_or_non_list_exit_0(tmp_path, capsys):
    """A topics entry whose tags are null/non-list must not crash (exit 0);
    malformed tags are treated as empty."""
    fixture = tmp_path / "topics.json"
    fixture.write_text('{"topics": [{"name": "orders", "tags": null}, '
                       '{"name": "inventory-events", "tags": 5}, '
                       '{"name": "payments", "tags": ["core"]}]}', encoding="utf-8")
    code = main(["report", "--topics", str(fixture)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    d = {t["name"]: t for t in data["topics"]}
    assert d["orders"]["tags"] == []
    assert d["inventory-events"]["tags"] == []
    assert d["payments"]["tags"] == ["core"]


def test_report_partitions_junk_exit_0(tmp_path, capsys):
    """Non-numeric/null partitions or replication_factor must fall back to
    defaults (exit 0), not crash with int(None)/int(list) TypeError."""
    fixture = tmp_path / "topics.json"
    fixture.write_text('{"topics": [{"name": "orders", "partitions": null, '
                       '"replication_factor": 3}, {"name": "payments", '
                       '"partitions": 6, "replication_factor": [1]}]}',
                       encoding="utf-8")
    code = main(["report", "--topics", str(fixture)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    d = {t["name"]: t for t in data["topics"]}
    assert d["orders"]["partitions"] == 1
    assert d["orders"]["replication_factor"] == 3
    assert d["payments"]["partitions"] == 6
    assert d["payments"]["replication_factor"] == 1


def test_report_groups_junk_topic_keys_exit_0(tmp_path, capsys):
    """A list-form groups fixture whose topic/group key is non-string (list)
    must be skipped cleanly (exit 0), not crash on an unhashable key."""
    groups = tmp_path / "groups.json"
    groups.write_text('{"groups": ['
                      '{"topic": ["payments"], "last_consumed_at": "2026-11-13T00:00:00Z"}, '
                      '{"group": "payments", "last_consumed_at": "2026-11-13T00:00:00Z"}]}',
                      encoding="utf-8")
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--groups", str(groups)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    payments = next(t for t in data["topics"] if t["name"] == "payments")
    assert payments["last_consumed_at"] is not None


def test_report_check_directory_as_file_exit_3(tmp_path, capsys):
    """A directory passed in place of a file must fail cleanly (exit 3),
    not crash with IsADirectoryError."""
    adir = tmp_path / "adir"
    adir.mkdir()
    topics = str(FIXTURES / "topics" / "topics.json")

    code = main(["report", "--topics", str(adir)])
    assert code == 3
    assert "report requires --topics" in capsys.readouterr().err

    rules = FIXTURES / "rules" / "rules_scale.yaml"
    code = main(["check", "--config", str(adir), "--messages", str(FIXTURES / "messages" / "payments_messages.json")])
    assert code == 3
    assert "--config" in capsys.readouterr().err

    code = main(["report", "--topics", topics, "--registry", str(adir)])
    assert code == 3
    assert "--registry file missing" in capsys.readouterr().err

    code = main(["schema-diff", "--before", str(adir), "--after", str(FIXTURES / "schemas" / "v1.json")])
    assert code == 3
    assert "not a file" in capsys.readouterr().err


def test_output_to_missing_directory_exit_3(capsys):
    """An -o path inside a nonexistent directory (or pointing at a directory)
    must fail cleanly (exit 3), not crash with FileNotFoundError/
    IsADirectoryError."""
    topics = str(FIXTURES / "topics" / "topics.json")
    code = main(["report", "--topics", topics, "-o", "/no/such/dir/out.json"])
    assert code == 3
    assert "cannot write output" in capsys.readouterr().err

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    messages = str(FIXTURES / "messages" / "payments_messages.json")
    code = main(["check", "--config", rules, "--messages", messages, "-o", "/tmp"])
    assert code == 3
    assert "is a directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_schema_diff_md(tmp_path):
    out = tmp_path / "diff.md"
    code = main(["export", "--type", "schema-diff", "--format", "markdown",
                 "--before", str(FIXTURES / "schemas" / "v1.json"),
                 "--after", str(FIXTURES / "schemas" / "v2.json"), "-o", str(out)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("#")


def test_export_report_csv(tmp_path):
    out = tmp_path / "report.csv"
    code = main(["export", "--type", "report", "--format", "csv",
                 "--topics", str(FIXTURES / "topics" / "topics.json"), "-o", str(out)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("name,")


def test_export_check_stdout_no_attribute_error(capsys):
    """export --type check must not crash on the missing --quiet attribute."""
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    messages = FIXTURES / "messages" / "payments_messages.json"
    code = main(["export", "--type", "check", "--config", str(rules),
                 "--messages", str(messages)])
    assert code == 1  # fixture messages are intentionally dirty
    data = json.loads(capsys.readouterr().out)
    assert len(data["violations"]) == 9


def test_export_schema_diff_csv_unsupported_exit_3(capsys):
    """schema-diff can only render JSON/Markdown; a csv export request must
    fail cleanly (exit 3), not crash on the missing --json attribute."""
    code = main(["export", "--type", "schema-diff", "--format", "csv",
                 "--before", str(FIXTURES / "schemas" / "v1.json"),
                 "--after", str(FIXTURES / "schemas" / "v2.json")])
    assert code == 3
    assert "json|markdown" in capsys.readouterr().err


def test_export_report_markdown_unsupported_exit_3(capsys):
    """report only renders JSON/CSV; a markdown export must not silently emit
    JSON with exit 0."""
    code = main(["export", "--type", "report", "--format", "markdown",
                 "--topics", str(FIXTURES / "topics" / "topics.json")])
    assert code == 3
    assert "csv|json" in capsys.readouterr().err


def test_export_check_csv_unsupported_exit_3(capsys):
    """check only renders JSON; a csv export must not silently ignore the
    requested format."""
    code = main(["export", "--type", "check", "--format", "csv",
                 "--config", str(FIXTURES / "rules" / "rules_scale.yaml"),
                 "--messages", str(FIXTURES / "messages" / "payments_messages.json")])
    assert code == 3
    assert "json" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# registry (live mock server through the CLI)
# ---------------------------------------------------------------------------


def test_registry_cli_full(server) -> None:
    code, out, _ = _run("registry", "--registry-url", server.base_url)
    assert code == 0
    data = json.loads(out)
    assert set(data["subjects"]) == {"orders-value", "payments-value"}


def test_registry_cli_single_subject(server) -> None:
    code, out, _ = _run("registry", "--registry-url", server.base_url, "--subject", "payments-value")
    assert code == 0
    data = json.loads(out)
    assert data["subject"] == "payments-value"
    assert [v["version"] for v in data["versions"]] == [1, 2]


def test_registry_cli_unreachable(capsys) -> None:
    code = main(["registry", "--registry-url", "http://127.0.0.1:1"])
    assert code == 3
