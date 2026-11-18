"""Pin the offline ``examples/`` dataset as an end-to-end regression contract.

The README "离线数据集" section documents the exact commands below against the
committed ``examples/`` tree — no real broker / Schema Registry required
(SPEC AC #4 离线模式（无 broker）全功能可用, AC #7 fixture 数据).  Nothing else
in the suite reads those fixtures, so a change that shifts the rule-engine
violation count, the schema-diff classification, or the report/CSV shape would
silently break the documented example block.  These tests run the real process
boundary (`python -m kgov`, the same path the README uses) and assert the
documented behaviour: exit codes, diff classification, violation counts,
per-topic governance state and the CSV export.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
EXAMPLES = PROJECT_ROOT / "examples"


def _run(*argv: str) -> tuple[int, str, str]:
    """Run ``python -m kgov`` as the README documents it, from the project
    root.  Returns (exit_code, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "kgov", *argv],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# schema-diff against the example pair (README: Additive rc=0 / Breaking rc=1)
# ---------------------------------------------------------------------------


def test_examples_schema_diff_additive_exit_0():
    rc, out, err = _run(
        "schema-diff",
        "--before", str(EXAMPLES / "schemas" / "payment_v1.json"),
        "--after", str(EXAMPLES / "schemas" / "payment_v2.json"),
        "--subject", "payments-value",
        "--json",
    )
    assert rc == 0
    assert "SCHEMA DIFF: Additive" in err
    data = json.loads(out)
    assert data["kind"] == "Additive"
    assert data["compatible"] is True
    assert data["before_version"] == 1
    assert data["after_version"] == 2


def test_examples_schema_diff_breaking_exit_1():
    rc, out, err = _run(
        "schema-diff",
        "--before", str(EXAMPLES / "schemas" / "payment_v1.json"),
        "--after", str(EXAMPLES / "schemas" / "payment_breaking.json"),
        "--subject", "payments-value",
        "--json",
    )
    assert rc == 1
    assert "Breaking" in err
    data = json.loads(out)
    assert data["kind"] == "Breaking"
    assert data["compatible"] is False


# ---------------------------------------------------------------------------
# check: 6 dirty messages -> exactly 8 detached violations, gate fails (rc=1)
# ---------------------------------------------------------------------------


def test_examples_check_detaches_eight_violations_and_gate_fails():
    rc, out, err = _run(
        "check",
        "--config", str(EXAMPLES / "rules" / "rules.yaml"),
        "--messages", str(EXAMPLES / "messages" / "payments_messages.json"),
    )
    assert rc == 1
    data = json.loads(out)
    assert data["ok"] is False
    assert data["violation_count"] == 8
    assert len(data["violations"]) == 8
    # every detached violation must name the field and rule that caught it
    for v in data["violations"]:
        assert v["field"]
        assert v["check"]


# ---------------------------------------------------------------------------
# report: three topics carry the governance states the README claims
# ---------------------------------------------------------------------------


def test_examples_report_states_and_violation_counts():
    rc, out, err = _run(
        "report",
        "--topics", str(EXAMPLES / "topics" / "topics.json"),
        "--registry", str(EXAMPLES / "registry" / "registry.json"),
        "--groups", str(EXAMPLES / "consumer_groups" / "consumer_groups.json"),
        "--rules", str(EXAMPLES / "rules" / "rules.yaml"),
        "--messages", str(EXAMPLES / "messages" / "payments_messages.json"),
    )
    assert rc == 0
    data = json.loads(out)
    assert data["total_topics"] == 3

    states = {t["name"]: t for t in data["topics"]}
    assert set(states) == {"payments", "orders", "inventory-events"}
    # payments evolved compatibly but its 8 detached violations accumulate
    assert states["payments"]["schema_state"] == "COMPATIBLE"
    assert states["payments"]["violation_count"] == 8
    # orders is registered but the topic schema no longer matches
    assert states["orders"]["schema_state"] == "BREAKING"
    assert states["orders"]["violation_count"] == 0
    # inventory-events has no schema subject at all
    assert states["inventory-events"]["schema_state"] == "NONE"
    assert states["inventory-events"]["violation_count"] == 0


# ---------------------------------------------------------------------------
# report --format csv: the README documents `report [--format json|csv]`
# (README.md:126); the explicit flag must yield the same CSV shape as export
# ---------------------------------------------------------------------------


def test_examples_report_format_csv_explicit_flag():
    rc, out, err = _run(
        "report",
        "--topics", str(EXAMPLES / "topics" / "topics.json"),
        "--registry", str(EXAMPLES / "registry" / "registry.json"),
        "--groups", str(EXAMPLES / "consumer_groups" / "consumer_groups.json"),
        "--rules", str(EXAMPLES / "rules" / "rules.yaml"),
        "--messages", str(EXAMPLES / "messages" / "payments_messages.json"),
        "--format", "csv",
    )
    assert rc == 0
    lines = out.splitlines()
    assert lines[0].split(",") == [
        "name", "owner", "tags", "partitions", "replication_factor",
        "schema_subject", "schema_version", "schema_state",
        "latest_message_at", "last_consumed_at", "stale", "violation_count",
    ]
    # header + one data row per topic, same order as the export pin
    assert [ln.split(",")[0] for ln in lines[1:]] == [
        "payments", "orders", "inventory-events",
    ]


# ---------------------------------------------------------------------------
# export: governance report as CSV with the documented header + 3 rows
# ---------------------------------------------------------------------------


def test_examples_export_report_csv_header_and_rows():
    rc, out, err = _run(
        "export",
        "--type", "report",
        "--topics", str(EXAMPLES / "topics" / "topics.json"),
        "--registry", str(EXAMPLES / "registry" / "registry.json"),
        "--groups", str(EXAMPLES / "consumer_groups" / "consumer_groups.json"),
        "--format", "csv",
    )
    assert rc == 0
    lines = out.splitlines()
    assert lines[0].split(",") == [
        "name", "owner", "tags", "partitions", "replication_factor",
        "schema_subject", "schema_version", "schema_state",
        "latest_message_at", "last_consumed_at", "stale", "violation_count",
    ]
    # header + one data row per topic
    assert [ln.split(",")[0] for ln in lines[1:]] == [
        "payments", "orders", "inventory-events",
    ]
