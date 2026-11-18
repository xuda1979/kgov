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


def test_schema_diff_version_id_markdown_flags(capsys):
    """--before/--after-version, --before/--after-id and --markdown must
    render into the diff output: the markdown header echoes the versions, the
    JSON payload carries all four, and --markdown never emits JSON."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    code = main(["schema-diff", "--before", before, "--after", after,
                 "--before-version", "3", "--after-version", "7"])
    out = capsys.readouterr()
    assert code == 0
    assert "Version 3 \u2192 7" in out.out

    code = main(["schema-diff", "--before", before, "--after", after,
                 "--markdown"])
    out = capsys.readouterr()
    assert code == 0
    assert out.out.lstrip().startswith("#")
    assert not out.out.lstrip().startswith("{")

    code = main(["schema-diff", "--before", before, "--after", after,
                 "--before-id", "11", "--after-id", "22",
                 "--before-version", "3", "--after-version", "7", "--json"])
    out = capsys.readouterr()
    assert code == 0
    data = json.loads(out.out)
    assert data["before_version"] == 3
    assert data["after_version"] == 7
    assert data["before_id"] == 11
    assert data["after_id"] == 22


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


def test_schema_diff_output_md_extension_shortcut(tmp_path):
    """README:45 documents ``[-o diff.md|diff.json]``; the direct subcommand
    must honour the ``.md`` output-extension shortcut (cli.py:238) exactly
    like ``--markdown`` — markdown written to the file, rc 0 for an additive
    diff. The export route pins the same branch; this pins the direct route
    the README documents."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    out = tmp_path / "diff.md"
    code = main([
        "schema-diff",
        "--before", before,
        "--after", after,
        "--subject", "payments-value",
        "-o", str(out),
    ])
    assert code == 0
    body = out.read_text(encoding="utf-8")
    assert body.lstrip().startswith("#")
    assert not body.lstrip().startswith("{")
    assert "Additive" in body


def test_schema_diff_output_json_extension_shortcut(tmp_path):
    """README:56 documents ``--json`` 或 ``.json`` 后缀输出 JSON; the direct
    subcommand must honour the ``.json`` output-extension shortcut
    (cli.py:237) exactly like ``--json`` even with no explicit flag —
    JSON written to the file, rc 0 for an additive diff."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    out = tmp_path / "diff.json"
    code = main([
        "schema-diff",
        "--before", before,
        "--after", after,
        "--subject", "payments-value",
        "-o", str(out),
    ])
    assert code == 0
    body = out.read_text(encoding="utf-8")
    assert body.lstrip().startswith("{")
    data = json.loads(body)
    assert data["kind"] == CompatibilityKind.ADDITIVE
    assert data["subject"] == "payments-value"



def test_schema_diff_format_avro_success_path(capsys):
    """README:44 documents ``--format avro|json`` as the schema *language*
    axis (not the output format): a successful ``--format avro`` run must exit
    0 and render the default markdown diff (no --json / no .json ext), with
    the format echoed verbatim in the header while JSON-schema *output* pins
    (--json / .json shortcut) stay unaffected."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    code = main([
        "schema-diff",
        "--before", before,
        "--after", after,
        "--subject", "payments-value",
        "--format", "avro",
    ])
    out = capsys.readouterr()
    assert code == 0
    assert out.out.lstrip().startswith("#")
    assert not out.out.lstrip().startswith("{")
    assert "- Format: **avro**" in out.out
    assert "`payments-value`" in out.out
    assert "Additive" in out.out


def test_schema_diff_format_json_success_path(capsys):
    """README:44 documents ``--format avro|json`` as the schema *language*
    axis (not the output format): the ``--format json`` member of the pair is
    symmetric to ``--format avro`` and must exit 0 with the default markdown
    diff echo ``Format: **json**`` verbatim (no --json / no .json ext), the
    subject backticked, while JSON-schema *output* pins stay unaffected. The
    schema can still be the Avro v1/v2 fixtures: forced ``json`` parses them
    via the generic JSON path (parse_schema cli.py:137) and yields the
    Non-Breaking empty-diff summary."""
    code = main([
        "schema-diff",
        "--before", str(FIXTURES / "schemas" / "v1.json"),
        "--after", str(FIXTURES / "schemas" / "v2.json"),
        "--subject", "payments-value",
        "--format", "json",
    ])
    out = capsys.readouterr()
    assert code == 0
    assert out.out.lstrip().startswith("#")
    assert not out.out.lstrip().startswith("{")
    assert "- Format: **json**" in out.out
    assert "`payments-value`" in out.out
    assert "Non-Breaking" in out.out


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


def test_schema_diff_full_http_url_refs(server) -> None:
    """The README's documented form — both refs as full
    ``http://registry/subjects/X/versions/N`` URLs — must fetch the
    Confluent-wire ``data["schema"]`` payload over real HTTP at the main()
    boundary: additive payments-value 1->2 exits 0, breaking orders-value
    1->2 exits 1, and the fetched (not local) schema text drives the diff."""
    base = server.base_url
    code, out, _ = _run(
        "schema-diff",
        "--before", f"{base}/subjects/payments-value/versions/1",
        "--after", f"{base}/subjects/payments-value/versions/2",
        "--json",
    )
    assert code == 0
    data = json.loads(out)
    assert data["subject"] == "record"  # --subject not given: default
    assert data["kind"] == CompatibilityKind.ADDITIVE
    assert any("fee_cents" in r["path"] for r in data["results"])

    # A version pair the mock marks BREAKING must gate exit 1 over the same
    # full-URL path (orders-value v2 removed a required field).
    code, out, _ = _run(
        "schema-diff",
        "--before", f"{base}/subjects/orders-value/versions/1",
        "--after", f"{base}/subjects/orders-value/versions/2",
        "--json",
    )
    assert code == 1
    data = json.loads(out)
    assert data["kind"] == CompatibilityKind.BREAKING
    assert data["compatible"] is False


def test_schema_diff_full_url_fetch_failure_exits_3(capsys):
    """A full-URL ref pointing at an unreachable registry must surface as a
    clean runtime error (exit 3, ``kgov: error:``, no traceback), not a raw
    urlopen exception escaping main(). Port 1 is never listening on
    loopback, so the fetch fails without needing a live server."""
    code = main([
        "schema-diff",
        "--before", "http://127.0.0.1:1/subjects/x/versions/1",
        "--after", "http://127.0.0.1:1/subjects/x/versions/2",
    ])
    out = capsys.readouterr()
    assert code == 3
    assert out.err.startswith("kgov: error: cannot fetch schema from http://127.0.0.1:1/")
    assert "Traceback" not in out.err
    assert out.out == ""


def test_schema_diff_full_url_json_scalar_body_fails_closed():
    """A full-URL ref whose registry answers a JSON *non-dict* body (null,
    number, bool, list) previously raw-crashed _load_schema_text with a bare
    TypeError ("schema" membership test on a non-container) escaping main().
    The loader now renders any non-str/non-wire-dict body as JSON text, so
    every scalar body must fold into a designed exit (0/1/3) with no
    traceback."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output quiet
            return

        def do_GET(self):
            body = self.server.body
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    # scalar/junk bodies: null, int, bool, list (pre-fix raw TypeError), plus
    # the further shapes that must also fail closed at the URL boundary — a
    # JSON string body (parses downstream as invalid schema text), non-JSON
    # garbage / empty bodies (JSONDecodeError is wrapped by the fetch-failure
    # CliError), a wire dict whose "schema" value is itself junk (fails in the
    # schema parse, not the loader), and a plain dict with no "schema" key
    # (rendered as JSON text and diffed against itself: designed exit 0).
    for body in (b"null", b"42", b"true", b'["payments-value"]',
                 b'"payments-value"', b'this is not json', b'',
                 b'{"schema": 42}', b'{"id": 3, "version": 2}'):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.body = body
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/subjects/x/versions/1"
            code, _, err = _run("schema-diff", "--before", url, "--after", url)
        finally:
            srv.shutdown()
            srv.server_close()
        assert code in (0, 1, 3), f"body {body!r}: exit {code}"
        assert "Traceback" not in err, f"body {body!r}: raw crash leaked"


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


def test_check_gate_exact_equality_and_payload_shape(tmp_path, capsys):
    """cli.py:366 gates with a strict len(violations) > max_v comparison, so a
    gate set exactly equal to the violation count is within the gate (9
    violations, --max-violations 9 -> rc 0) while one below fails (8 -> rc 1).
    test_check_within_gate_exit_0 (10) and test_check_gate_exceeded_exit_1 (3)
    pin both sides but never the exact-equality boundary, the full result
    payload key set, or the stderr summary line the README's CI-gate story
    relies on."""
    from datetime import datetime

    rules = FIXTURES / "rules" / "rules_scale.yaml"
    msgs = FIXTURES / "messages" / "payments_messages.json"
    out = tmp_path / "result.json"

    code = main(["check", "--config", str(rules), "--messages", str(msgs),
                 "--max-violations", "9", "-o", str(out)])
    assert code == 0  # exact equality is within the gate
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"ok", "gate", "violation_count", "rule", "topic",
                         "violations", "generated_at"}
    assert data["ok"] is False
    assert data["gate"] == 9
    assert data["violation_count"] == 9
    assert data["rule"] == "payments-constraints"
    assert data["topic"] == "payments"
    assert len(data["violations"]) == 9
    generated_at = data["generated_at"]
    parsed = datetime.fromisoformat(generated_at)
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    assert "ok=False violations=9 gate=9 -> PASS" in capsys.readouterr().err

    code = main(["check", "--config", str(rules), "--messages", str(msgs),
                 "--max-violations", "8", "-o", str(out)])
    assert code == 1  # one below the violation count fails
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["gate"] == 8
    assert "ok=False violations=9 gate=8 -> FAIL" in capsys.readouterr().err


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


def test_report_rules_messages_accumulate_violations(tmp_path):
    """AC #3 at the CLI boundary — report wired with --rules + --messages
    accumulates the real per-topic violation counts from the fixture data."""
    out = tmp_path / "report.json"
    code = main([
        "report",
        "--topics", str(FIXTURES / "topics" / "topics.json"),
        "--registry", str(FIXTURES / "registry" / "registry.json"),
        "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
        "--rules", str(FIXTURES / "rules" / "rules_scale.yaml"),
        "--messages", str(FIXTURES / "messages" / "payments_messages.json"),
        "-o", str(out),
    ])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["total_topics"] == 3
    payments = next(t for t in data["topics"] if t["name"] == "payments")
    assert payments["violation_count"] == 9
    assert all(t["violation_count"] == 0 for t in data["topics"]
               if t["name"] != "payments")


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


def test_report_violations_wrapper_shape(tmp_path, capsys):
    """report --violations accepts the dict-wrapper form {'violations': [...]}
    (cli.cmd_report:480-481) and counts matching topics; junk entries inside
    the list are ignored cleanly (exit 0), matching the flat-list behavior."""
    vio = tmp_path / "vio.json"
    vio.write_text('{"violations": [{"topic": "payments", "message": "x"}, '
                   '"garbage", {"topic": "no-such-topic"}]}', encoding="utf-8")
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--violations", str(vio)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    payments = next(t for t in data["topics"] if t["name"] == "payments")
    assert payments["violation_count"] == 1
    others = [t for t in data["topics"] if t["name"] != "payments"]
    assert all(t["violation_count"] == 0 for t in others)


def test_report_violations_junk_shape_tolerated(tmp_path, capsys):
    """A --violations file whose JSON shape is not a list of entries (dict
    without a violations list, scalar, empty list) must be treated as no
    violations (exit 0), not crash on a .get()/.extend() TypeError."""
    shapes = ('{"foo": 1}', '{"violations": "junk"}', "42", "3.5", "null", "[]")
    topics = str(FIXTURES / "topics" / "topics.json")
    for i, payload in enumerate(shapes):
        vio = tmp_path / f"vio{i}.json"
        vio.write_text(payload, encoding="utf-8")
        code = main(["report", "--topics", topics, "--violations", str(vio)])
        assert code == 0, payload
        data = json.loads(capsys.readouterr().out)
        assert all(t["violation_count"] == 0 for t in data["topics"]), payload
    # non-path --violations values fail closed with a clean runtime error
    code = main(["report", "--topics", topics, "--violations", "42"])
    assert code == 3
    assert "file not found" in capsys.readouterr().err


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


def test_report_registry_junk_compatibility_exit_0(tmp_path, capsys):
    """A version dict whose 'compatibility' is non-string junk (a list) must
    not crash the state lookup on an unhashable dict key (exit 0)."""
    reg = tmp_path / "reg.json"
    reg.write_text('{"subjects": {"payments-value": '
                   '[{"version": 2, "compatibility": ["BACKWARD"]}]}}',
                   encoding="utf-8")
    code = main(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                 "--registry", str(reg)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    payments = next(t for t in data["topics"] if t["name"] == "payments")
    assert payments["schema_version"] == 2
    assert isinstance(payments["schema_state"], str)


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


def test_write_out_junk_text_no_traceback(capsys):
    """``cli._write_out`` with a none-str ``text`` (a direct library caller)
    must render via str() instead of raw-crashing on ``text.endswith``."""
    from kgov.cli import _write_out

    for text in (None, 42, 3.5, b"x", ["a"], {"a": 1}, {"s"}, (1, 2)):
        _write_out(None, text)  # stdin/stdout path; must not raise
    for text in ("hello", "hello\n", "multi\nline"):
        capsys.readouterr()
        _write_out(None, text)
        out = capsys.readouterr().out
        assert out == text if text.endswith("\n") else out == text + "\n"


def test_write_out_junk_text_with_path_fails_closed(tmp_path):
    """``cli._write_out`` with a real output path and none-str ``text`` must
    render via str() instead of raw-crashing Path.write_text with a bare
    TypeError ("data must be str, not ...")."""
    from kgov.cli import _write_out

    out = tmp_path / "out.txt"
    for text in (None, 42, 3.5, b"x", ["a"], {"a": 1}, {"s"}, (1, 2)):
        _write_out(str(out), text)  # file-write path; must not raise
        assert out.read_text(encoding="utf-8") == str(text)
    # valid str behavior is preserved
    _write_out(str(out), "hello")
    assert out.read_text(encoding="utf-8") == "hello"


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


def test_direct_subcommand_format_axes_pinned(capsys):
    """Each direct subcommand's own --format/--markdown axis must be enforced
    at the parser (exit 2 usage error on out-of-choices values) and accepted
    for the intended values: check only accepts json, report accepts json|csv,
    schema-diff accepts avro|json and --markdown. Pins the direct-subcommand
    axis (the export battery only pins the --type route)."""
    def go(argv):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
        except SystemExit as exc:  # pragma: no cover - safety net
            code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    v1, v2 = str(FIXTURES / "schemas" / "v1.json"), str(FIXTURES / "schemas" / "v2.json")

    # check: only json is a valid choice; csv is a usage error (2)
    for junk in ("csv", "markdown", "xml"):
        code, _, err = go(["check", "--config", str(FIXTURES / "rules" / "rules_scale.yaml"),
                           "--messages", str(FIXTURES / "messages" / "payments_messages.json"),
                           "--format", junk])
        assert code == 2, junk
        assert "Traceback" not in err

    # report: json|csv are valid; xml is a usage error (2)
    code, _, err = go(["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
                       "--format", "xml"])
    assert code == 2
    assert "Traceback" not in err

    # schema-diff: avro|json are valid; yaml is a usage error (2)
    for junk in ("yaml", "xml", "markdown"):
        code, _, err = go(["schema-diff", "--before", v1, "--after", v2,
                           "--format", junk])
        assert code == 2, junk
        assert "Traceback" not in err
    code, _, _ = go(["schema-diff", "--before", v1, "--after", v2,
                     "--format", "avro"])
    assert code == 0


def test_export_violations_alias_routes_to_check(capsys):
    """export --type violations is an alias for check (cli.cmd_export:529-531);
    it must run the full check gate and reject non-JSON formats cleanly."""
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    messages = FIXTURES / "messages" / "payments_messages.json"
    code = main(["export", "--type", "violations", "--format", "json",
                 "--config", str(rules), "--messages", str(messages)])
    assert code == 1  # fixture messages are intentionally dirty
    data = json.loads(capsys.readouterr().out)
    assert data["violation_count"] == 9
    assert len(data["violations"]) == 9

    code = main(["export", "--type", "violations", "--format", "csv",
                 "--config", str(rules), "--messages", str(messages)])
    assert code == 3
    assert "supports --format json" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# registry (live mock server through the CLI)
# ---------------------------------------------------------------------------


def test_registry_cli_full(server) -> None:
    code, out, _ = _run("registry", "--registry-url", server.base_url)
    assert code == 0
    data = json.loads(out)
    assert set(data["subjects"]) == {"orders-value", "payments-value"}


def test_registry_cli_full_dump_shape(server) -> None:
    """README:109: without ``--subject`` the full-dump is
    ``{"subjects": {...}, "generated_at": ...}``. test_registry_cli_full pins
    only the subject key set; this test pins the documented top-level key set,
    a UTC ISO-8601 ``generated_at`` (Z-handling as in report._parse_dt), and
    that each subject maps to per-version id/version/compatibility/schema
    entries in ascending version order."""
    from datetime import datetime

    code, out, _ = _run("registry", "--registry-url", server.base_url)
    assert code == 0
    data = json.loads(out)
    assert set(data) == {"subjects", "generated_at"}
    generated_at = data["generated_at"]
    assert isinstance(generated_at, str)
    dt_text = generated_at[:-1] + "+00:00" if generated_at.endswith("Z") else generated_at
    parsed = datetime.fromisoformat(dt_text)
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    subjects = data["subjects"]
    assert set(subjects) == {"orders-value", "payments-value"}
    orders = subjects["orders-value"]
    assert [v["version"] for v in orders] == [1, 2]
    assert [v["compatibility"] for v in orders] == ["BREAKING", "BREAKING"]
    for version in orders:
        assert set(version) == {"id", "version", "compatibility", "schema"}


def test_registry_cli_single_subject(server) -> None:
    code, out, _ = _run("registry", "--registry-url", server.base_url, "--subject", "payments-value")
    assert code == 0
    data = json.loads(out)
    assert data["subject"] == "payments-value"
    assert [v["version"] for v in data["versions"]] == [1, 2]


def test_registry_cli_subject_version_shape(server) -> None:
    """README:109-110: ``--subject`` mode outputs that subject's per-version
    id/version/compatibility/schema. test_registry_cli_single_subject pins only
    the subject name and version numbers; this test pins the documented
    per-version key shape (cli.py:441-447) and that each version carries its own
    schema — v2 evolved: a record named Payment with two new nullable fields
    over v1."""
    code, out, _ = _run("registry", "--registry-url", server.base_url,
                        "--subject", "payments-value")
    assert code == 0
    data = json.loads(out)
    assert data["subject"] == "payments-value"
    assert [v["version"] for v in data["versions"]] == [1, 2]
    for version in data["versions"]:
        assert set(version) == {"id", "version", "compatibility", "schema"}
    v1, v2 = data["versions"]
    assert (v1["id"], v1["compatibility"]) == (1, "BACKWARD")
    assert v2["id"] == 2
    assert v1["schema"]["type"] == "record"
    assert v1["schema"]["name"] == "Payment"
    v1_fields = [f["name"] for f in v1["schema"]["fields"]]
    v2_fields = [f["name"] for f in v2["schema"]["fields"]]
    assert "fee_cents" not in v1_fields
    assert v2_fields == v1_fields + ["fee_cents", "created_at"]


def test_registry_cli_unreachable(capsys) -> None:
    code = main(["registry", "--registry-url", "http://127.0.0.1:1"])
    assert code == 3


def test_registry_cli_junk_subject_exit_3(server, capsys) -> None:
    """A junk-typed or unknown --subject must fail closed with a clean exit 3
    CliError (the RegistryError from the 404 is wrapped, never a raw
    TypeError/traceback), for every junk-typed CLI value."""
    for junk in ("42", "no-such-subject", "@@bad@@", "['a']", "{'a': 1}", "b'sub'"):
        code, _, err = _run("registry", "--registry-url", server.base_url,
                            "--subject", junk)
        assert code == 3, junk
        assert "registry request failed" in err, junk
        assert "Traceback" not in err, junk

    # an empty --subject falls through to the falsy full-dump branch
    code, out, _ = _run("registry", "--registry-url", server.base_url, "--subject", "")
    assert code == 0
    assert "subjects" in json.loads(out)


def test_registry_cli_junk_timeout_exit_3(server, capsys) -> None:
    """A float-parseable but invalid --timeout (-1, nan, inf) must not hang or
    crash: the socket timeout error is funneled into RegistryError and wrapped
    into a clean exit 3 CliError. A non-float --timeout is a usage error (2)."""
    for junk in ("-1", "nan", "inf", "1e999"):
        code, _, err = _run("registry", "--registry-url", server.base_url,
                            "--timeout", junk)
        assert code == 3, junk
        assert "registry request failed" in err, junk
        assert "Traceback" not in err, junk

    code, out, _ = _run("registry", "--registry-url", server.base_url,
                        "--timeout", "abc")
    assert code == 2
    code, out, _ = _run("registry", "--registry-url", server.base_url,
                        "--timeout", "0")
    assert code == 0


def test_registry_cli_output_write_fails_closed(server) -> None:
    """cmd_registry's -o path (reached only after a successful registry
    request) must reuse the shared _write_out fail-closed contract: a
    nonexistent directory or a directory target exits 3 with a clean error,
    never a raw FileNotFoundError/IsADirectoryError traceback."""
    for out, expected in (("/no/such/dir/out.json", "no such directory"),
                          ("/tmp", "is a directory")):
        code, _, err = _run("registry", "--registry-url", server.base_url,
                            "-o", out)
        assert code == 3, out
        assert "cannot write output" in err, out
        assert expected in err, out
        assert "Traceback" not in err, out


def test_registry_wire_mode_single_response(server) -> None:
    """GET /mode must emit exactly one response (the intended 200). Before the
    fall-through fix the /mode branch replied 200 and then kept routing into a
    second 404 reply on the same connection — two stacked responses that
    corrupt any keep-alive client stream. Read the raw socket bytes so the
    second stacked 404 would be caught."""
    import socket

    port = int(server.base_url.rsplit(":", 1)[1])
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(b"GET /mode HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    assert data.count(b"HTTP/1.0") == 1, data.decode("utf-8", "replace")
    assert b"{\"mode\": \"READWRITE\"}" in data


def test_registry_wire_routing_table_single_response(server) -> None:
    """Every route in the do_GET/POST/PUT/DELETE table must emit exactly one
    response. The /mode fall-through (two stacked replies on one connection)
    only corrupted keep-alive streams invisibly to http.client, which surfaces
    just the first response; a raw read to socket close catches any regrowth.
    One HEAD is per path; a DOUBLE means an integer multiple escaped."""
    import socket

    port = int(server.base_url.rsplit(":", 1)[1])
    routes = [
        (b"GET /subjects HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        (b"GET /subjects/payments-value/versions HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        (b"GET /subjects/payments-value/versions/1 HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        (b"GET /config HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        (b"GET /config/payments-value HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        (b"GET /schemas/ids/2 HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        (b"GET /mode HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        (b"GET /bogus HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        (b"GET /subjects/nope/versions HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        (b"POST /subjects HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        (b"PUT /config HTTP/1.0\r\n\r\n", b"HTTP/1.0 405"),
        (b"DELETE /subjects/x HTTP/1.0\r\n\r\n", b"HTTP/1.0 405"),
    ]
    for raw, expect in routes:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall(raw)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        assert data.count(b"HTTP/1.0") == 1, (
            f"path {raw.split()[1].decode()!r} stacked responses: "
            f"{data.decode('utf-8', 'replace')}"
        )
        assert data.startswith(expect), (
            f"path {raw.split()[1].decode()!r} unexpected status: "
            f"{data.split(b'\r\n')[0].decode()!r}"
        )


def test_registry_wire_permutation_grid_single_response(server) -> None:
    """The fixed routing-table pin cannot perturb the dispatch's branch
    discriminators (segment counts 2/4/5, the schemas/ids int-parse, the
    per-subject version/config lookup misses, HEAD). Every reachable do_GET
    branch must still emit exactly one frame: the 404 fall-throughs and the
    odd shapes exercise the same trailing-reply path the /mode double-reply
    escaped through, so each is pinned here raw-read to close."""
    import socket

    port = int(server.base_url.rsplit(":", 1)[1])
    grid = [
        # segment-count discriminators: len 2 (no versions word) falls to 404
        (b"GET /subjects/payments-value HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        # len 5: extra trailing segment escapes both /versions shapes
        (b"GET /subjects/payments-value/versions/1/extra HTTP/1.0\r\n\r\n",
         b"HTTP/1.0 404"),
        # version lookup miss on a well-formed /versions/{id} path
        (b"GET /subjects/payments-value/versions/999 HTTP/1.0\r\n\r\n",
         b"HTTP/1.0 404"),
        # config lookup miss
        (b"GET /config/nope HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        # config accepts any len > 1 (loose subject read) — still one frame
        (b"GET /config/payments-value/extra HTTP/1.0\r\n\r\n",
         b"HTTP/1.0 200"),
        # trailing slash: parts == ['schemas'], no ids keyword → 404
        (b"GET /schemas/ HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        # schemas/ids int-parse miss (ValueError caught inside the branch)
        (b"GET /schemas/ids/nope HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        # the only get-schema bound branch (id not in backer)
        (b"GET /schemas/ids/999 HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        # schemas/ids accepts len >= 3 — trailing extra stays a single 200
        (b"GET /schemas/ids/2/extra HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        # mode matches any length; keep the loose shape pinned as design
        (b"GET /mode/extra HTTP/1.0\r\n\r\n", b"HTTP/1.0 200"),
        # foreign head with deep parts — single trailing 404
        (b"GET /foreign/x/y HTTP/1.0\r\n\r\n", b"HTTP/1.0 404"),
        # HEAD has no do_HEAD override → stdlib 501, still one frame
        (b"HEAD /subjects HTTP/1.0\r\n\r\n", b"HTTP/1.0 501"),
    ]
    for raw, expect in grid:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall(raw)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        assert data.count(b"HTTP/1.0") == 1, (
            f"path {raw.split()[1].decode()!r} stacked responses: "
            f"{data.decode('utf-8', 'replace')}"
        )
        assert data.startswith(expect), (
            f"path {raw.split()[1].decode()!r} unexpected status: "
            f"{data.split(b'\r\n')[0].decode()!r}"
        )


def test_registry_wire_survives_abrupt_client_drops(server) -> None:
    """Handler threads must not take the mock server down when a client aborts
    mid-request or mid-reply: connect-and-close, send-a-partial-line-and-
    close, and send-then-close-before-reading all land the reply write on a
    dead socket. Increment 1 pinned _reply's (AttributeError, OSError) guards
    in-process; this pins the live server stays healthy for the next request,
    which still emits exactly one 200 frame afterwards."""
    import socket
    import time

    port = int(server.base_url.rsplit(":", 1)[1])

    def one(raw):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall(raw)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        return data

    assert one(b"GET /subjects HTTP/1.0\r\n\r\n").count(b"HTTP/1.0") == 1
    for _ in range(5):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /subjects/payments-value/versions HTTP/1.0\r\n\r\n")
        s.close()  # abort before reading the reply
    for _ in range(5):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /config")
        s.close()  # partial request line, then close
    for _ in range(5):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.close()  # connect, send nothing, close
    time.sleep(0.5)  # let the handler threads finish their dead-socket writes
    after = one(b"GET /subjects HTTP/1.0\r\n\r\n")
    assert after.count(b"HTTP/1.0") == 1, after.decode("utf-8", "replace")
    assert after.startswith(b"HTTP/1.0 200")


# ---------------------------------------------------------------------------
# junk argv / unreadable paths (CLI entry surface)
# ---------------------------------------------------------------------------


def test_main_junk_argv_no_traceback(capsys):
    """Non-list/tuple argv containers (an int, a string, bytes, a dict, ...)
    must not crash argparse with a raw TypeError on list(args); the CLI exits
    with a usage code instead."""
    for junk in (42, "schema-diff", b"schema-diff", {"a": 1}, 2.5, None):
        code = main(junk)  # type: ignore[arg-type]
        assert code == 2
        assert "Traceback" not in capsys.readouterr().err


def test_main_non_str_argv_entries_tolerated(capsys):
    """Non-str entries inside argv (bytes, ints, lists, dicts, ...) must not
    crash argparse on arg_string[0]/path construction; they are dropped and
    the remaining args dispatch normally (usage error if now incomplete)."""
    for argv in (
        ["schema-diff", "--before", b"x", "--after", 2],
        ["schema-diff", "--before", 1, "--after", 2],
        ["report", "--topics", None],
        ["check", "--config", ["a"]],
        ["registry", "--registry-url", True],
        [("schema", "diff")],
    ):
        code = main(argv)  # type: ignore[list-item]
        assert code in (0, 1, 2, 3)
        assert "Traceback" not in capsys.readouterr().err


def test_unreadable_fixture_files_exit_3(tmp_path, capsys):
    """A fixture file that passes is_file() but cannot be read (permission
    denied) must exit 3 with a clean error, not leak a raw PermissionError."""
    fixture = tmp_path / "noperm.json"
    fixture.write_text(json.dumps({"topics": [{"name": "t"}]}), encoding="utf-8")
    fixture.chmod(0)
    try:
        code = main(["report", "--topics", str(fixture)])
        assert code == 3
        assert "Traceback" not in capsys.readouterr().err

        code = main(["schema-diff", "--before", str(fixture), "--after", str(fixture)])
        assert code == 3
        assert "Traceback" not in capsys.readouterr().err
    finally:
        fixture.chmod(0o644)


def test_nul_byte_path_exit_3(capsys) -> None:
    """A schema-diff path containing an embedded NUL byte must exit 3 with a
    clean error, not leak a raw ValueError from read_text."""
    code = main(["schema-diff", "--before", "a\x00b", "--after", "c.json"])
    assert code == 3


# ---------------------------------------------------------------------------
# junk option values on the public cmd_* handlers
# ---------------------------------------------------------------------------


def test_cmd_handlers_tolerate_junk_option_values():
    """The cmd_* handlers are public functions; a library caller can hand them
    a Namespace whose option values are junk-typed (int, list, bytes, dict,
    ...). Those must raise a clean CliError — never a raw TypeError from
    pathlib.Path()/.lower()/.startswith() or a comparison crash."""
    import argparse

    from kgov.cli import CliError, cmd_check, cmd_registry, cmd_report, cmd_schema_diff

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")
    topics = str(FIXTURES / "topics" / "topics.json")
    before = str(FIXTURES / "schemas" / "breaking_before.json")
    after = str(FIXTURES / "schemas" / "breaking_after.json")

    def ns(**kw):
        return argparse.Namespace(**kw)

    for junk in (42, ["x"], b"rules", {"a": 1}):
        with pytest.raises(CliError):
            cmd_check(ns(config=junk, messages=msgs))
    for junk in (42, [1], b"msgs", {"a": 1}):
        with pytest.raises(CliError):
            cmd_check(ns(config=rules, messages=junk, broker=None, topic=None,
                         quiet=True, max_violations=None, schema_registry=None,
                         output=None))
    with pytest.raises(CliError):
        cmd_schema_diff(ns(before=42, after=after, format=None, subject="record",
                           before_version=1, after_version=2, before_id=None,
                           after_id=None, json=False, markdown=False, output=None))
    # Junk output values are *dropped* (rendered to stdout), never crashed on:
    assert cmd_schema_diff(ns(before=before, after=after, format=None, subject="record",
                              before_version=1, after_version=2, before_id=None,
                              after_id=None, json=False, markdown=False, output=["x"])) == 1
    with pytest.raises(CliError):
        cmd_registry(ns(registry_url=42, subject=None, timeout=10.0, output=None))
    for junk in (42, ["x"], b"topics", {"a": 1}):
        with pytest.raises(CliError):
            cmd_report(ns(topics=junk, registry=None, groups=None, rules=None,
                          messages=None, violations=None, format="json", output=None))
    # A junk max_violations value cannot participate in the numeric gate
    # comparison; it fails closed (gate=0 -> exit 1), never a raw TypeError:
    assert cmd_check(ns(config=rules, messages=msgs, broker=None, topic=None,
                        quiet=True, max_violations="abc", schema_registry=None,
                        output=None)) == 1


def test_cmd_check_junk_broker_topic_raises_clierror():
    """Truthy junk --broker/--topic values must raise a clean CliError at the
    CLI boundary — before any lazy broker import or live AdminClient/Consumer
    gets a junk bootstrap string. Without the gate, list/dict/bytes bootstrap
    values reach a real confluent-kafka client and hang the scan (Fix 24's
    hang class); junks must never leak past the boundary."""
    import argparse

    from kgov.cli import CliError, cmd_check

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")

    def ns(**kw):
        return argparse.Namespace(**kw)

    # every junk broker value -> CliError, never a wire call
    for junk in (None, "", 0, False, ["x"], {"a": 1}, 42, b"host", 3.5):
        with pytest.raises(CliError):
            cmd_check(ns(config=rules, messages=None, broker=junk, topic="t",
                         max_violations=None, schema_registry=None, output=None))
    # junk topic with a valid broker -> CliError (boundary gate, not the wire)
    for junk in (None, "", 0, ["x"], {"a": 1}, b"t"):
        with pytest.raises(CliError):
            cmd_check(ns(config=rules, messages=None, broker="127.0.0.1:9092",
                         topic=junk, max_violations=None, schema_registry=None,
                         output=None))


def test_cmd_check_broker_scan_junk_msgs_fail_closed(monkeypatch):
    """A read_topic wire/double returning junk msgs topology (non-list
    containers, non-message entries) must raise a clean CliError — never a
    raw TypeError/AttributeError from `for m in msgs` / `m.topic`, and never
    a gate that passes vacuously on a junk read."""
    import argparse

    from kgov.broker import BrokerMessage
    from kgov.cli import CliError, cmd_check

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")

    def ns(**kw):
        return argparse.Namespace(**kw)

    calls = {}

    def junk_read(bootstrap, topic, value_decoder=None, **kw):
        return calls["return"]

    monkeypatch.setattr("kgov.broker.read_topic", junk_read)

    for junk in (None, 42, "msg", {"a": 1}, [None], [42],
                 [{"topic": "t", "partition": 0, "offset": 1}], [object()]):
        calls["return"] = junk
        with pytest.raises(CliError, match="invalid messages"):
            cmd_check(ns(config=rules, messages=None, broker="127.0.0.1:9092",
                         topic="t", max_violations=None, schema_registry=None,
                         output=None))

    # a valid BrokerMessage list still checks normally (fence, not broken)
    calls["return"] = [BrokerMessage(topic="t", partition=0, offset=1,
                                     key=None, value={"amount_cents": 100})]
    assert cmd_check(ns(config=rules, messages=None, broker="127.0.0.1:9092",
                        topic="t", max_violations=None, schema_registry=None,
                        output=None)) == 0


def test_cmd_check_junk_schema_registry_fails_closed(monkeypatch):
    """Truthy junk --schema-registry values must raise a clean CliError at the
    CLI boundary — before any lazy import or live client. Without the gate,
    SchemaRegistryClient str()'s a list/dict/int/bytes URL into a garbage
    base_url, the AvroRecordDecoder then silently returns _UNDECODED for every
    wire message, and a reachable broker passes the gate as if it decoded
    everything — a dead-decoder false pass for the same junk class already
    gated for --broker/--topic."""
    import argparse

    from kgov.broker import BrokerMessage
    from kgov.cli import CliError, cmd_check

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")

    def ns(**kw):
        return argparse.Namespace(**kw)

    monkeypatch.setattr(
        "kgov.broker.read_topic",
        lambda bootstrap, topic, value_decoder=None, **kw:
        [BrokerMessage(topic="t", partition=0, offset=1,
                       key=None, value={"amount_cents": 100})],
    )

    # every junk registry value -> CliError, never a wire call
    for junk in ([], [("http://x",)], {"u": 1}, 42, b"http://x", 3.5,
                 True, object()):
        with pytest.raises(CliError, match="schema-registry"):
            cmd_check(ns(config=rules, messages=None, broker="127.0.0.1:9092",
                         topic="t", max_violations=None, schema_registry=junk,
                         output=None))
    # None (absent flag) and a well-formed URL are accepted (fence, not broken)
    for ok in (None, "http://127.0.0.1:8081"):
        assert cmd_check(ns(config=rules, messages=None,
                            broker="127.0.0.1:9092", topic="t",
                            max_violations=None, schema_registry=ok,
                            output=None)) == 0


def test_cmd_check_schema_registry_wires_decoder(monkeypatch):
    """A broker scan with --schema-registry must wire an AvroRecordDecoder into
    read_topic: the flag's only purpose. The wiring runs inside _validate, so
    this pins the cli.py branch that constructs
    AvroRecordDecoder(SchemaRegistryClient(url)) — no live registry is touched
    (SchemaRegistryClient is only constructed, never called)."""
    import argparse

    from kgov.broker import AvroRecordDecoder, BrokerMessage
    from kgov.cli import cmd_check

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    calls = {}

    def capture(bootstrap, topic, value_decoder=None, **kw):
        calls["decoder"] = value_decoder
        return [BrokerMessage(topic="t", partition=0, offset=1,
                              key=None, value={"amount_cents": 100})]

    monkeypatch.setattr("kgov.broker.read_topic", capture)
    code = cmd_check(argparse.Namespace(
        config=rules, messages=None, broker="127.0.0.1:9092", topic="t",
        max_violations=None, schema_registry="http://127.0.0.1:8081",
        output=None))
    assert code == 0
    # the wiring passed a decoder built from a real SchemaRegistryClient
    assert isinstance(calls["decoder"], AvroRecordDecoder)

    # without the flag, no decoder is wired
    monkeypatch.setattr("kgov.broker.read_topic", capture)
    code = cmd_check(argparse.Namespace(
        config=rules, messages=None, broker="127.0.0.1:9092", topic="t",
        max_violations=None, schema_registry=None, output=None))
    assert code == 0
    assert calls["decoder"] is None


def test_cmd_registry_junk_client_returns_fail_closed(monkeypatch):
    """A registry client wire/double returning junk must never traceback.

    Back-to-back with a library double, the request wrapper (except
    Exception -> CliError) already converts non-iterable version lists and
    junk info dicts to a clean error. The gap this test guards: non-JSON-
    serializable values (set/bytes) smuggled into the payload inside junk
    get_version/to_fixture_dict returns crash raw from json.dumps, because
    the serialization runs *after* the request wrapper (Fix 28)."""
    import argparse

    from kgov.cli import CliError, cmd_registry

    def ns(**kw):
        return argparse.Namespace(**kw)

    def call(subject="s"):
        return cmd_registry(ns(registry_url="http://127.0.0.1:1", subject=subject,
                               timeout=10.0, output=None))

    calls = {}

    class JunkClient:
        def __init__(self, url, timeout=None):
            pass

        def get_versions(self, subject):
            return calls["versions"]

        def get_version(self, subject, v):
            return calls["info"]

        def to_fixture_dict(self):
            return calls["fixture"]

    monkeypatch.setattr("kgov.registry.SchemaRegistryClient", JunkClient)

    good_info = {"id": 1, "version": 1, "compatibility": "BACKWARD", "schema": {"type": "string"}}

    # non-iterable version lists -> clean CliError (existing request wrapper)
    for versions in (None, 42, 7):
        calls["versions"] = versions
        calls["info"] = good_info
        with pytest.raises(CliError):
            call()

    # iterable-but-junk versions are tolerated (exit 0, no traceback); a
    # broken wire cannot crash the scan through the iteration itself
    for versions in ("v", {"a": 1}, [[1, 2]], [None], [1, "x", None]):
        calls["versions"] = versions
        calls["info"] = good_info
        assert call() == 0

    # junk get_version info (valid versions list): missing keys / None ->
    # CliError; non-JSON-serializable values (set/bytes) -> CliError via the
    # serialization guard (Fix 28), never a raw json.dumps TypeError
    calls["versions"] = [1]
    for info in ({}, None, {"id": 1, "version": 1, "compatibility": "BACKWARD"},
                 {"id": {1, 2}, "version": 1, "compatibility": "BACKWARD",
                  "schema": {}},
                 {"id": b"x", "version": 1, "compatibility": "BACKWARD",
                  "schema": {}},
                 {"id": 1, "version": 1, "compatibility": "BACKWARD",
                  "schema": b"raw"},
                 {"id": 1, "version": 1, "compatibility": "BACKWARD",
                  "schema": {"fields": [{"type": {1, 2}}]}}):
        calls["info"] = info
        with pytest.raises(CliError):
            call()
    calls["info"] = good_info

    # junk to_fixture_dict (no-subject path): missing/absent subjects -> the
    # request wrapper raises CliError; non-JSON-serializable subject entries
    # raise CliError via the serialization guard
    for fixture in (None, 42, {},
                    {"subjects": [{"name": "t", "compatibility": {1}}]},
                    {"subjects": [b"x"]}):
        calls["fixture"] = fixture
        with pytest.raises(CliError):
            call(subject=None)
    # tolerated: serializable junk subjects render exit 0 (fence)
    calls["fixture"] = {"subjects": None}
    assert call(subject=None) == 0
    calls["fixture"] = {"subjects": {"a": 1}}
    assert call(subject=None) == 0

    # valid single-subject payload still renders (fence, not broken)
    calls["versions"] = [1]
    calls["info"] = good_info
    assert call() == 0


def test_cmd_check_junk_max_violations_fails_closed():
    """A junk max_violations value must not participate in the numeric gate
    comparison; a library caller passing junk gets a fail-closed gate
    (junk -> gate 0 -> exit 1), never a raw TypeError on '<='."""
    import argparse

    from kgov.cli import cmd_check

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")
    for junk in ("abc", "", {"a": 1}, [1], b"x"):
        assert cmd_check(argparse.Namespace(
            config=rules, messages=msgs, broker=None, topic=None,
            quiet=True, max_violations=junk, schema_registry=None,
            output=None)) == 1


def test_cmd_handlers_tolerate_missing_attributes():
    """The cmd_* handlers are public functions; a library caller can hand them
    a partial Namespace that lacks some of the parser's option attributes.
    Missing attributes must behave exactly like their argparse defaults
    (None / False) — never a raw AttributeError on direct attribute access."""
    import argparse

    from kgov.cli import (CliError, cmd_check, cmd_export, cmd_registry,
                          cmd_report, cmd_schema_diff)

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")
    topics = str(FIXTURES / "topics" / "topics.json")
    before = str(FIXTURES / "schemas" / "breaking_before.json")
    after = str(FIXTURES / "schemas" / "breaking_after.json")

    def ns(**kw):
        return argparse.Namespace(**kw)

    # Empty Namespace: each handler must produce its clean missing-arg CliError.
    for handler, junk in (
        (cmd_schema_diff, ns()),
        (cmd_registry, ns()),
        (cmd_report, ns()),
    ):
        with pytest.raises(CliError):
            handler(junk)
    with pytest.raises(CliError):
        cmd_check(ns(config=rules))  # no data source -> CliError

    # Partial Namespace missing the data-source attributes: the missing
    # attributes behave as None, so cmd_check still runs against --messages
    # (broker/topic absent -> not required) and gates exit 1 on the matches,
    # never an AttributeError on direct access.
    assert cmd_check(ns(config=rules, messages=msgs)) == 1

    # Partial Namespace missing `output` (parser default None): rendered to
    # stdout; schema-diff on the breaking fixture still gates exit 1.
    assert cmd_schema_diff(ns(before=before, after=after)) == 1

    # cmd_export with a partial Namespace delegates to the target handler with
    # argparse defaults filled in for the schema-diff version/id attributes.
    assert cmd_export(ns(type="schema-diff", format="json", before=before,
                         after=after)) == 1
    # cmd_export delegating to cmd_check with a real data source runs and
    # gates exit 1 on the 9 violations (broker/topic absent -> not required).
    assert cmd_export(ns(type="check", format="json", config=rules,
                         messages=msgs, broker=None, topic=None)) == 1
    # Missing registry/groups behave as None (tolerated) -> report builds.
    assert cmd_report(ns(topics=topics)) == 0


def test_cmd_topics_tolerates_junk_bootstrap_and_options(monkeypatch, capsys):
    """cmd_topics is the one public handler Fixes 22/23 did not probe, and this
    venv genuinely ships confluent-kafka, so a truthy junk --bootstrap
    (list/dict/int/bytes) would previously pass the `if not bootstrap` gate
    and reach a live AdminClient network call. A str-only gate sends junk
    bootstrap to a clean CliError before any import/network; a valid str
    bootstrap still reaches list_topics (stubbed offline, no hang)."""
    import argparse

    from kgov.broker import _require_timeout
    from kgov.cli import CliError, cmd_topics

    calls: dict = {}

    def fake_list_topics(bootstrap, timeout=10.0):
        # Honor the real broker contract (list_topics calls _require_timeout
        # first): junk timeouts must fail closed here too, so the truthful
        # stub can't mask the wire guard behind a permissive pass-through.
        _require_timeout(timeout)
        calls["timeout"] = timeout
        return []

    monkeypatch.setattr("kgov.broker.list_topics", fake_list_topics)

    def ns(**kw):
        return argparse.Namespace(**kw)

    for junk in (None, "", 0, False, 42, ["x"], {"a": 1}, b"host", 3.5):
        with pytest.raises(CliError):
            cmd_topics(ns(bootstrap=junk))
    with pytest.raises(CliError):
        cmd_topics(ns())  # missing bootstrap attribute -> gate, not AttributeError

    # Valid str bootstrap: no hang, no error, exit 0 (wire stubbed).
    assert cmd_topics(ns(bootstrap="127.0.0.1:9092")) == 0
    # Falsy timeouts coerce to the default; must reach the wire as 10.0.
    assert calls["timeout"] == 10.0
    # The --timeout value is wired through unchanged (7.5 -> list_topics 7.5).
    assert cmd_topics(ns(bootstrap="127.0.0.1:9092", timeout=7.5)) == 0
    assert calls["timeout"] == 7.5
    # Truthy junk timeouts fail closed fast (real _require_timeout rejects).
    for junk in ("abc", -1, True, float("nan"), float("inf")):
        with pytest.raises(CliError):
            cmd_topics(ns(bootstrap="127.0.0.1:9092", timeout=junk))
    # A truthy junk output value takes the force-write path and raises the
    # clean CliError (same design as cmd_check); falsy junk drops to stdout.
    with pytest.raises(CliError):
        cmd_topics(ns(bootstrap="127.0.0.1:9092", output=["x"]))
    assert cmd_topics(ns(bootstrap="127.0.0.1:9092", output=0)) == 0
    capsys.readouterr()


def test_topics_dispatch_exit_codes(monkeypatch, capsys):
    """topics is the only subcommand with no main()-level dispatch pin (the
    handlers for schema-diff/check/report/registry/export all route through
    ``main``). A failed metadata pull must exit 3 with a clean error — never
    pass the gate with an empty list — and a successful pull must exit 0 with
    the topics JSON payload on stdout."""
    def boom(bootstrap, timeout=10.0):
        raise RuntimeError("cannot list topics")

    monkeypatch.setattr("kgov.broker.list_topics", boom)
    assert main(["topics", "--bootstrap", "127.0.0.1:9"]) == 3
    assert "failed to list topics" in capsys.readouterr().err

    monkeypatch.setattr("kgov.broker.list_topics",
                        lambda bootstrap, timeout=10.0: ["payments", "orders"])
    assert main(["topics", "--bootstrap", "127.0.0.1:9092"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["topics"] == ["payments", "orders"]


def test_cmd_topics_output_write_fails_closed(monkeypatch, tmp_path):
    """cmd_topics's -o path (reached only after list_topics succeeds) must
    reuse the shared _write_out fail-closed contract — a nonexistent directory
    or a directory target raises the clean CliError, and a valid path writes
    the payload file with exit 0."""
    import argparse

    from kgov.broker import _require_timeout
    from kgov.cli import CliError, cmd_topics

    def fake_list_topics(bootstrap, timeout=10.0):
        _require_timeout(timeout)
        return ["payments"]

    monkeypatch.setattr("kgov.broker.list_topics", fake_list_topics)

    ns = argparse.Namespace
    for out, expected in (("/no/such/dir/out.json", "no such directory"),
                          ("/tmp", "is a directory")):
        with pytest.raises(CliError) as exc:
            cmd_topics(ns(bootstrap="127.0.0.1:9092", output=out,
                          timeout=None))
        assert "cannot write output" in str(exc.value)
        assert expected in str(exc.value)

    good = tmp_path / "topics.json"
    assert cmd_topics(ns(bootstrap="127.0.0.1:9092", output=str(good),
                         timeout=None)) == 0
    data = json.loads(good.read_text(encoding="utf-8"))
    assert data["topics"] == ["payments"]


def test_topics_dump_shape_pins_rich_metadata(monkeypatch, capsys):
    """README:118: ``kgov topics --bootstrap`` pulls "topic 名称、分区数、副本
    因子". test_topics_dispatch_exit_codes stubs list_topics with bare name
    strings, so nothing pins the documented rich per-topic shape that broker.
    list_topics actually mirrors ({name, partitions, replication_factor, owner,
    tags, latest_message_at}) nor the top-level ``{"topics", "generated_at"}``
    key set at the CLI boundary. This test stubs the real client-mirror shape
    and pins both the pass-through payload keys and a UTC ISO-8601
    generated_at (same Z-normalization as report._parse_dt)."""
    from datetime import datetime

    def rich_list_topics(bootstrap, timeout=10.0):
        return [
            {
                "name": "payments",
                "partitions": 6,
                "replication_factor": 3,
                "owner": "payments-team",
                "tags": ["core"],
                "latest_message_at": "2026-11-17T08:00:00Z",
            },
            {
                "name": "orders",
                "partitions": 1,
                "replication_factor": 1,
                "owner": "",
                "tags": [],
                "latest_message_at": None,
            },
        ]

    monkeypatch.setattr("kgov.broker.list_topics", rich_list_topics)
    assert main(["topics", "--bootstrap", "127.0.0.1:9092"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"topics", "generated_at"}
    generated_at = data["generated_at"]
    assert isinstance(generated_at, str)
    dt_text = generated_at[:-1] + "+00:00" if generated_at.endswith("Z") else generated_at
    parsed = datetime.fromisoformat(dt_text)
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0

    topics = data["topics"]
    assert [t["name"] for t in topics] == ["payments", "orders"]
    payments = topics[0]
    assert set(payments) == {
        "name",
        "partitions",
        "replication_factor",
        "owner",
        "tags",
        "latest_message_at",
    }
    assert payments["partitions"] == 6 and payments["replication_factor"] == 3
    assert payments["owner"] == "payments-team" and payments["tags"] == ["core"]
    assert payments["latest_message_at"] == "2026-11-17T08:00:00Z"
    assert topics[1]["partitions"] == 1 and topics[1]["replication_factor"] == 1
    assert topics[1]["latest_message_at"] is None


def test_load_schema_text_tolerates_junk_refs_fail_closed():
    """_load_schema_text (the schema-diff before/after ref loader) is the one
    public cli helper Fixes 22-28 never probed. Every junk ref class must fold
    into the clean CliError, never a raw TypeError (pathlib on non-str) or raw
    ValueError (os.fspath/embedded-null-byte on NUL-str refs): non-str refs hit
    the isinstance gate, NUL-byte refs either fall into the wrapped shorthand
    branch (with exactly one colon) or the JSON-read handler (already
    (OSError, ValueError) -> CliError)."""
    from kgov.cli import CliError, _load_schema_text

    before = FIXTURES / "schemas" / "breaking_before.json"

    # Non-str refs: clean CliError gate, never a raw pathlib TypeError.
    for junk in (None, 42, 3.5, b"x", ["x"], {"a": 1}, {"s"}, (1, 2)):
        with pytest.raises(CliError):
            _load_schema_text(junk, None)

    # NUL-byte str refs: the one-colon shape reaches the wrapped shorthand
    # branch (unpack failure -> CliError before any client/network); the
    # no-colon shapes hit the JSON-read handler's (OSError, ValueError) guard.
    for junk in ("\x00", "a\x00b", "x\x00:1"):
        with pytest.raises(CliError):
            _load_schema_text(junk, "avro")

    # A valid file ref still loads its JSON and keeps the format hint.
    text, fmt = _load_schema_text(str(before), "avro")
    assert json.loads(text)
    assert fmt == "avro"


def _run_module(*argv: str) -> int:
    """Run the real process entry (`python -m kgov.cli`, the same code path
    the installed `kgov` console script executes: sys.exit(main()) at the
    bottom of cli.py) and return its process exit code. The in-process main()
    tests above never cross the sys.exit boundary, so this subprocess pin is
    the only assertion of the exit-code contract as a real process sees it."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "kgov.cli", *argv],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
        timeout=60,
    )
    return proc.returncode


def test_module_entry_exit_code_contract():
    """The process boundary (cli.py `sys.exit(main())`) must surface the same
    exit-code contract the in-process tests assert: usage problems -> 2,
    success -> 0, breaking schema diff / violated gate -> 1, missing input
    file -> 3. No raw Traceback may escape into stderr on any of these."""

    # no subcommand / unknown subcommand -> argparse usage error (2)
    assert _run_module() == 2
    assert _run_module("bogus-cmd") == 2
    # --help is a usage success -> 0
    assert _run_module("--help") == 0

    # missing rules file -> clean runtime error (3), not a raw OSError
    rc = _run_module("check", "--config", "/no/rules.yaml",
                     "--messages", "x.json")
    assert rc == 3

    # breaking schema diff -> gate fail (1); success/no diff contact -> 0
    before = str(FIXTURES / "schemas" / "breaking_before.json")
    after = str(FIXTURES / "schemas" / "breaking_after.json")
    assert _run_module("schema-diff", "--before", before, "--after", after) == 1
    v1 = str(FIXTURES / "schemas" / "v1.json")
    v2 = str(FIXTURES / "schemas" / "v2.json")
    assert _run_module("schema-diff", "--before", v1, "--after", v2) == 0


def test_module_package_entry_matches_cli_entry():
    """``python -m kgov`` (kgov/__main__.py) must be interchangeable with the
    documented ``python -m kgov.cli`` path and the installed ``kgov`` console
    script: same version output, same exit-code contract. Otherwise README
    examples and the pyproject script alias can disagree on the entry point."""
    import subprocess
    import sys

    def run(module: str, *argv: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", module, *argv],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
            timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr

    # version surface identical to kgov.cli
    rc_pkg, out_pkg, _ = run("kgov", "--version")
    rc_cli, out_cli, _ = run("kgov.cli", "--version")
    assert rc_pkg == rc_cli == 0
    assert out_pkg == out_cli

    # argument-parsing exit codes match too (empty argv -> usage 2)
    assert run("kgov")[0] == run("kgov.cli")[0] == 2

    # schema-diff exit-code contract through the package entry
    v1 = str(FIXTURES / "schemas" / "v1.json")
    v2 = str(FIXTURES / "schemas" / "v2.json")
    assert run("kgov", "schema-diff", "--before", v1, "--after", v2)[0] == 0
    before = str(FIXTURES / "schemas" / "breaking_before.json")
    after = str(FIXTURES / "schemas" / "breaking_after.json")
    assert run("kgov", "schema-diff", "--before", before, "--after", after)[0] == 1


def test_version_flag_and_pyproject_consistency():
    """The ``kgov --version`` flag reports the package version and is the one
    printed surface tied to ``kgov/__init__.py.__version__``. That constant is
    duplicated by hand in pyproject.toml's ``version`` field; nothing keeps
    them in sync, so a bump that updates only one side would make the flag
    silently lie. Pin the process output and both declaration points to the
    same version string."""
    import re
    import subprocess
    import sys

    # process surface: exactly "kgov <version>", exit 0
    proc = subprocess.run(
        [sys.executable, "-m", "kgov.cli", "--version"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
        timeout=60,
    )
    assert proc.returncode == 0
    match = re.fullmatch(r"kgov (\S+)\s*", proc.stdout)
    assert match, f"unexpected --version output: {proc.stdout!r}"
    cli_version = match.group(1)

    # source of truth: kgov/__init__.py
    from kgov import __version__

    assert cli_version == __version__

    # pyproject.toml member must not drift from the package constant
    project_root = Path(__file__).parent.parent
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    field = re.search(r"^version\s*=\s*\"([^\"]+)\"", pyproject, re.MULTILINE)
    assert field, "pyproject.toml missing a version = \"...\" field"
    assert field.group(1) == __version__


def test_main_interrupt_and_broken_pipe_exit_codes(monkeypatch):
    """main() must translate Ctrl-C (KeyboardInterrupt) to 130 and a closed
    stdout pipe (BrokenPipeError) to 0 — never a raw traceback. These are the
    two documented exit paths (design doc: "KeyboardInterrupt（130）、
    BrokenPipeError（0）") that no other test exercises: every handler above
    wraps its failures in CliError (exit 3), so only an out-of-band signal
    reaches these clauses."""
    import argparse

    class _Parser(argparse.ArgumentParser):
        def parse_args(self, argv=None):
            return argparse.Namespace(func=self._func)

        def set_func(self, func):
            self._func = func

    def make_parser(func):
        p = _Parser(prog="kgov")
        p.set_func(func)
        return p

    monkeypatch.setattr("kgov.cli.build_parser", lambda: make_parser(
        lambda args: (_ for _ in ()).throw(KeyboardInterrupt())))
    assert main(["schema-diff"]) == 130

    monkeypatch.setattr("kgov.cli.build_parser", lambda: make_parser(
        lambda args: (_ for _ in ()).throw(BrokenPipeError())))
    assert main(["schema-diff"]) == 0

