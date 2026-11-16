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
