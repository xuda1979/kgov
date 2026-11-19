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


def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze kgov.model's wall clock so generated_at-bearing payloads render
    identically across separate main() runs. now_utc_iso() (kgov/model.py:18)
    stamps a fresh datetime.now() on every render, so any two-render identity
    pin (stdout leg vs -o leg) can cross a second boundary and flake
    (observed in the registry full-dump pin: 04:03:09 vs 04:03:08). Replacing
    the module-global datetime class freezes the stamp; payloads only ever
    carry the ISO string, so no other code path observes the swap."""
    from datetime import datetime, timezone

    import kgov.model

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 19, 4, 0, 0, tzinfo=tz)

    monkeypatch.setattr(kgov.model, "datetime", _FrozenDatetime)


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
    """A breaking diff exits 1 on every output route (README:57 ``kgov
    schema-diff``), yet the -o file is still written on all of them: stdout
    stays empty, the gate summary ``SCHEMA DIFF: Breaking (incompatible)`` is
    the only stderr output, and the written file carries the breaking judgment.
    Pins the stdout-empty/stderr-routing the old rc-1 pin never asserted, for
    both the ``--json`` route and the ``.md`` extension shortcut, matching the
    same contract the export route pins at cli.py."""
    before = str(FIXTURES / "schemas" / "breaking_before.json")
    after = str(FIXTURES / "schemas" / "breaking_after.json")
    base = ["schema-diff", "--before", before, "--after", after]

    # --json -o: rc 1, stdout empty, summary on stderr, JSON file written
    out_json = tmp_path / "diff.json"
    code, out, err = _run(*base, "--json", "-o", str(out_json))
    assert code == 1
    assert out == ""
    assert err == "SCHEMA DIFF: Breaking (incompatible)\n"
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["kind"] == CompatibilityKind.BREAKING
    assert data["compatible"] is False

    # .md extension shortcut (cli.py:238): same rc-1 contract, markdown file
    out_md = tmp_path / "diff.md"
    code, out, err = _run(*base, "-o", str(out_md))
    assert code == 1
    assert out == ""
    assert err == "SCHEMA DIFF: Breaking (incompatible)\n"
    body = out_md.read_text(encoding="utf-8")
    assert body.lstrip().startswith("#")
    assert not body.lstrip().startswith("{")
    assert "Compatibility: **Breaking** (INCOMPATIBLE)" in body


def test_schema_diff_output_md_extension_shortcut(tmp_path):
    """README:45 documents ``[-o diff.md|diff.json]``; the direct subcommand
    must honour the ``.md`` output-extension shortcut (cli.py:238) exactly
    like ``--markdown`` — markdown written to the file, rc 0 for an additive
    diff. The export route pins the same branch; this pins the direct route
    the README documents, including the file-vs-stdout identity: the -o leg
    keeps stdout empty, moves only the gate summary to stderr, and writes the
    byte-identical markdown render to the file (modulo the trailing newline
    the stdout print adds)."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    base = ["schema-diff", "--before", before, "--after", after,
            "--subject", "payments-value"]
    # stdout leg renders the default markdown with the gate summary on stderr
    code, out, err = _run(*base)
    assert code == 0
    assert out.lstrip().startswith("#")
    assert "Additive" in out
    assert err == "SCHEMA DIFF: Additive\n"
    assert out.endswith("\n")
    # -o diff.md honours the extension shortcut and keeps stdout empty
    md_path = tmp_path / "diff.md"
    code, out_f, err_f = _run(*base, "-o", str(md_path))
    assert code == 0
    assert out_f == ""
    assert err_f == "SCHEMA DIFF: Additive\n"
    body = md_path.read_text(encoding="utf-8")
    assert body.lstrip().startswith("#")
    assert not body.lstrip().startswith("{")
    assert "Additive" in body
    # raw texts differ only by the trailing newline the stdout print adds
    assert body == out.rstrip("\n")


def test_schema_diff_output_markdown_flag_file_vs_stdout(tmp_path):
    """The explicit ``--markdown`` flag (cli.py:613) must hold the same
    file-vs-stdout identity the ``.md`` extension shortcut pins in
    test_schema_diff_output_md_extension_shortcut: the -o leg keeps stdout
    empty, routes the gate summary to stderr, and writes the byte-identical
    markdown render to the file (modulo the stdout print's trailing newline).
    cli.py:238 resolves both routes through the same ``want_md`` branch, so the
    flag and the shortcut must also produce byte-identical files (README:56
    ``--json`` 或 ``.json`` 后缀 are documented as equivalent routes)."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    base = ["schema-diff", "--before", before, "--after", after,
            "--subject", "payments-value"]

    # stdout leg with the explicit flag: markdown render + trailing newline
    code, out, err = _run(*base, "--markdown")
    assert code == 0
    assert out.lstrip().startswith("#")
    assert not out.lstrip().startswith("{")
    assert "Additive" in out
    assert err == "SCHEMA DIFF: Additive\n"
    assert out.endswith("\n")

    # -o leg with the explicit flag: stdout empty, file == stdout minus newline
    out_path = tmp_path / "flag.md"
    code, out_f, err_f = _run(*base, "--markdown", "-o", str(out_path))
    assert code == 0
    assert out_f == ""
    assert err_f == "SCHEMA DIFF: Additive\n"
    body = out_path.read_text(encoding="utf-8")
    assert body.lstrip().startswith("#")
    assert not body.lstrip().startswith("{")
    assert body == out.rstrip("\n")

    # the extension shortcut resolves to the identical file content
    shot_path = tmp_path / "shortcut.md"
    code, _, _ = _run(*base, "-o", str(shot_path))
    assert code == 0
    assert shot_path.read_text(encoding="utf-8") == body


def test_schema_diff_output_json_extension_shortcut(tmp_path):
    """README:56 documents ``--json`` 或 ``.json`` 后缀输出 JSON; the direct
    subcommand must honour the ``.json`` output-extension shortcut
    (cli.py:237) exactly like ``--json`` even with no explicit flag —
    JSON written to the file, rc 0 for an additive diff. Also pins the
    file-vs-stdout identity: the -o leg keeps stdout empty, keeps the gate
    summary on stderr, and writes the byte-identical JSON render (modulo the
    stdout print's trailing newline) to the file."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    base = ["schema-diff", "--before", before, "--after", after,
            "--subject", "payments-value"]
    # --json stdout render: payload with trailing newline, no stderr noise
    code, out, err = _run(*base, "--json")
    assert code == 0
    data = json.loads(out)
    assert data["kind"] == CompatibilityKind.ADDITIVE
    assert data["subject"] == "payments-value"
    assert err == "SCHEMA DIFF: Additive\n"
    assert out.endswith("\n")
    # -o diff.json honours the extension shortcut and keeps stdout empty
    out_path = tmp_path / "diff.json"
    code, out_f, err_f = _run(*base, "-o", str(out_path))
    assert code == 0
    assert out_f == ""
    assert err_f == "SCHEMA DIFF: Additive\n"
    body = out_path.read_text(encoding="utf-8")
    assert body.lstrip().startswith("{")
    assert json.loads(body) == data
    # raw texts differ only by the trailing newline the stdout print adds
    assert body == out.rstrip("\n")


def test_schema_diff_output_mode_precedence_json_wins(tmp_path):
    """README:45 documents ``[-o diff.md|diff.json]`` and README:56 documents
    ``--json``/``.json`` 后缀 or Markdown output as *alternative* output-mode
    signals; when conflicting signals are combined the resolution is
    deterministic and JSON wins: cli.py:237-238 evaluates ``want_json`` (the
    ``--json`` flag OR a ``.json`` extension) before ``want_md`` (the
    ``--markdown`` flag OR a ``.md`` extension), so every conflicting combo
    renders JSON while markdown is only ever chosen when NO json signal is
    present. Pins that precedence so a future reorder of the two branches
    cannot silently flip which signal wins."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    base = ["schema-diff", "--before", before, "--after", after,
            "--subject", "payments-value"]

    # --markdown -o diff.json: the .json extension wins over the md flag
    out1 = tmp_path / "flag_vs_ext.json"
    code, out, err = _run(*base, "--markdown", "-o", str(out1))
    assert code == 0
    assert out == ""
    assert err == "SCHEMA DIFF: Additive\n"
    body1 = out1.read_text(encoding="utf-8")
    assert body1.lstrip().startswith("{")
    assert json.loads(body1)["kind"] == "Additive"

    # --json -o diff.md: the json flag wins over the md extension
    out2 = tmp_path / "flag_vs_ext.md"
    code, out, err = _run(*base, "--json", "-o", str(out2))
    assert code == 0
    assert out == ""
    body2 = out2.read_text(encoding="utf-8")
    assert body2.lstrip().startswith("{")
    assert json.loads(body2) == json.loads(body1)
    assert not body2.endswith("\n")  # file write carries no stdout newline

    # --markdown --json on stdout: the json flag wins over the md flag
    code, out, err = _run(*base, "--markdown", "--json")
    assert code == 0
    assert out.lstrip().startswith("{")
    assert json.loads(out)["kind"] == "Additive"
    assert err == "SCHEMA DIFF: Additive\n"
    assert out.endswith("\n")


def test_schema_diff_json_output_file_vs_stdout(tmp_path):
    """README:56 documents ``--json`` 输出 JSON; README:45 documents
    ``[-o diff.md|diff.json]``. The direct schema-diff route must write the
    -o file as the identical rendering to stdout — the payload equal and the
    raw texts differing only by the trailing newline the stdout print adds
    (cli.py:244 _write_out, cli.py:124 newline normalization) — with stdout
    kept empty on the -o leg and the stderr gate summary unchanged. The
    breaking-fixture leg must still write the file while exiting 1. The export
    route pins this identity for --type schema-diff; the direct route the
    README documents has no such pin."""
    v1 = str(FIXTURES / "schemas" / "v1.json")
    v2 = str(FIXTURES / "schemas" / "v2.json")
    base = ["schema-diff", "--before", v1, "--after", v2,
            "--subject", "payments-value", "--json"]

    code, out, err = _run(*base)
    assert code == 0
    data = json.loads(out)
    assert data["kind"] == "Additive"
    assert data["compatible"] is True
    assert data["subject"] == "payments-value"
    assert data["format"] == "avro"
    assert data["results"] == [{"kind": "Additive", "path": "email",
                                "message": "field added (optional): `email`"}]
    assert err == "SCHEMA DIFF: Additive\n"
    assert out.endswith("\n")  # stdout print adds a trailing newline

    out_path = tmp_path / "diff.json"
    code, out_file, err_file = _run(*base, "-o", str(out_path))
    assert code == 0
    assert out_file == ""  # -o leg keeps stdout empty
    assert err_file == "SCHEMA DIFF: Additive\n"
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data
    assert file_text == out.rstrip("\n")  # raw texts differ only by that newline

    # breaking fixtures: stdout leg exits 1 with the payload on stdout, the
    # -o leg keeps stdout empty but still writes the identical file
    bb = str(FIXTURES / "schemas" / "breaking_before.json")
    ba = str(FIXTURES / "schemas" / "breaking_after.json")
    code, out_b, err_b = _run("schema-diff", "--before", bb, "--after", ba,
                              "--subject", "payments-value", "--json")
    assert code == 1
    data_b = json.loads(out_b)
    assert data_b["kind"] == "Breaking"
    assert data_b["compatible"] is False
    assert err_b == "SCHEMA DIFF: Breaking (incompatible)\n"

    break_path = tmp_path / "breaking.json"
    code, out_bf, err_bf = _run("schema-diff", "--before", bb, "--after", ba,
                                "--subject", "payments-value", "--json",
                                "-o", str(break_path))
    assert code == 1
    assert out_bf == ""
    assert err_bf == "SCHEMA DIFF: Breaking (incompatible)\n"
    assert json.loads(break_path.read_text(encoding="utf-8")) == data_b

    # default markdown: the .md -o file is the identical render to stdout
    # (no --json / no .json ext) minus the trailing stdout newline
    code, out_md, err_md = _run(*base[:-1])  # drop --json
    assert code == 0
    assert out_md.lstrip().startswith("#")
    md_path = tmp_path / "diff.md"
    code, out_mdf, err_mdf = _run(*base[:-1], "-o", str(md_path))
    assert code == 0
    assert out_mdf == ""
    assert md_path.read_text(encoding="utf-8") == out_md.rstrip("\n")



def test_schema_diff_non_breaking_identical_renders(tmp_path, monkeypatch):
    """An identical --before/--after diff (README:55 ``kgov schema-diff``)
    exits 0 with the ``SCHEMA DIFF: Non-Breaking`` gate summary and the
    "_No structural changes detected._" empty-diff marker: the default
    markdown route renders the compatibility banner (compatible), the
    --json route carries kind Non-Breaking / results [] / compatible true,
    and both -o files are the byte-identical render to stdout (JSON needs a
    frozen clock because the payload carries generated_at). No test pinned
    this empty-diff route while Additive (56) and Breaking (98) both have
    stdout/-o identity pins."""
    v1 = str(FIXTURES / "schemas" / "v1.json")
    base = ["schema-diff", "--before", v1, "--after", v1,
            "--subject", "payments-value"]

    # default markdown: gate summary + empty-diff marker on stdout
    code, out, err = _run(*base)
    assert code == 0
    assert err == "SCHEMA DIFF: Non-Breaking\n"
    assert out.endswith("\n")
    assert "Compatibility: **Non-Breaking** (compatible)" in out
    assert "_No structural changes detected._" in out

    md_path = tmp_path / "diff.md"
    code, out_f, err_f = _run(*base, "-o", str(md_path))
    assert code == 0
    assert out_f == ""  # -o leg keeps stdout empty
    assert err_f == "SCHEMA DIFF: Non-Breaking\n"
    assert md_path.read_text(encoding="utf-8") == out.rstrip("\n")

    # --json: payload pins + -o identity (generated_at requires frozen clock)
    _freeze_clock(monkeypatch)
    code, out_j, err_j = _run(*base, "--json")
    assert code == 0
    assert err_j == "SCHEMA DIFF: Non-Breaking\n"
    assert out_j.endswith("\n")
    data = json.loads(out_j)
    assert data["kind"] == "Non-Breaking"
    assert data["results"] == []
    assert data["compatible"] is True

    json_path = tmp_path / "diff.json"
    code, out_jf, err_jf = _run(*base, "--json", "-o", str(json_path))
    assert code == 0
    assert out_jf == ""
    assert err_jf == "SCHEMA DIFF: Non-Breaking\n"
    assert json.loads(json_path.read_text(encoding="utf-8")) == data
    assert json_path.read_text(encoding="utf-8") == out_j.rstrip("\n")


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


def test_check_quiet_suppresses_stdout_at_main_boundary(tmp_path):
    """README:26 documents check as the CI gate, and cli.py:390-391 make the
    JSON payload stdout-only unless -o writes it (stderr keeps the
    ok=... gate=... -> PASS/FAIL summary in both cases). Earlier check pins
    pass --quiet only as a companion flag while asserting exit codes, so the
    flag's own contract — stdout fully suppressed, exit code and file/stderr
    legs untouched — is never pinned through the parser. This test pins: the
    dirty fixture renders JSON to stdout without the flag (exit 1); --quiet
    empties stdout while preserving the exit code (1 over gate, 0 within
    gate); --quiet with -o still writes the file and the stderr summary;
    --quiet never masks a usage failure (--topic without --broker still exit
    3); and export --type check (496-497) carries the same flag through."""
    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")
    dirty = ["check", "--config", rules, "--messages", msgs]

    # no flag: full JSON payload on stdout, exit 1, stderr empty
    code, out, err = _run(*dirty)
    assert code == 1
    assert out.startswith("{") and json.loads(out)["violation_count"] == 9
    assert err == ""

    # --quiet: stdout suppressed, exit code preserved
    code, out, err = _run(*dirty, "--quiet")
    assert code == 1
    assert out == "" and err == ""

    # --quiet within the gate: exit 0, still no stdout
    code, out, err = _run(*dirty, "--max-violations", "10", "--quiet")
    assert code == 0
    assert out == "" and err == ""

    # --quiet with -o: file written, stdout empty, summary stays on stderr
    out_file = tmp_path / "quiet.json"
    code, out, err = _run(*dirty, "--quiet", "-o", str(out_file))
    assert code == 1
    assert out == ""
    assert out_file.exists() and json.loads(out_file.read_text(encoding="utf-8"))["ok"] is False
    assert "ok=False violations=9 gate=0 -> FAIL" in err

    # --quiet never masks a usage failure: --topic without --broker -> exit 3
    code, out, err = _run("check", "--config", rules, "--topic", "payments", "--quiet")
    assert code == 3
    assert out == ""
    assert "--topic requires --broker" in err

    # export --type check alias honors --quiet identically (exit 1, no stdout)
    code, out, err = _run("export", "--type", "check", "--config", rules,
                          "--messages", msgs, "--quiet")
    assert code == 1
    assert out == "" and err == ""


def test_check_cli_output_file_success_writes_json(tmp_path, monkeypatch):
    """cmd_check writes byte-identical JSON (modulo the trailing newline a
    stdout print adds) to the -o file and to stdout on the no-output route at
    the main() boundary. report/registry/topics got this file-vs-stdout pin in
    earlier rounds; existing check pins only read the -o file back, never
    compared it against the stdout render. The -o leg keeps stdout empty and
    moves the gate summary to stderr; the stdout leg has empty stderr."""
    _freeze_clock(monkeypatch)
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    msgs = FIXTURES / "messages" / "payments_messages.json"
    base = ["check", "--config", str(rules), "--messages", str(msgs)]

    code, out, err = _run(*base)
    assert code == 1  # fixture messages are intentionally dirty
    data_stdout = json.loads(out)
    assert err == ""  # stdout leg: summary stays off stderr

    out_path = tmp_path / "check_results.json"
    code, out_file, err_file = _run(*base, "-o", str(out_path))
    assert code == 1
    assert out_file == ""  # -o route keeps stdout empty
    assert "ok=False violations=9 gate=0 -> FAIL" in err_file
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data_stdout
    # raw texts differ only by the trailing newline the stdout print adds
    assert out.rstrip("\n") == file_text


def test_check_usage_error_exit_2_and_gate_fail_closed():
    """README (91-92) documents the check exit ladder ``0|1|2|3``, but the
    main() pins cover only 0/1/3; the argparse exit-2 usage leg and the
    fail-closed gate-legs for a non-positive --max-violations were pinned only
    at the library Namespace boundary (test_cmd_check_junk_max_violations
    ~2385), never through the real parser. A non-int or missing value is an
    argparse ``type=int`` usage error (exit 2 with a clean message and empty
    stdout) that fires even when the handler would otherwise complain about
    the missing data source -- argparse rejects before dispatch. A negative or
    zero gate must fail closed (exit 1) because ``max_v <= 0`` can never pass
    while violations exist (cli.py:366); a large gate passes (exit 0) and the
    payload keeps the requested gate value."""
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    msgs = FIXTURES / "messages" / "payments_messages.json"
    base = ["check", "--config", str(rules), "--messages", str(msgs)]

    # exit 2: parser usage errors on the --max-violations axis
    for bad_argv in [["--max-violations", "abc"],
                     ["--max-violations", "3.5"],
                     ["--max-violations", "--quiet"],
                     ["--max-violations"]]:
        code, out, err = _run(*base, *bad_argv)
        assert code == 2, bad_argv
        assert out == ""
        assert "error:" in err
        assert "Traceback" not in err

        # argparse rejects before the handler: the same bad value without a
        # data source is still a usage error, never the exit-3 CliError
        code, out, err = _run("check", "--config", str(rules), *bad_argv)
        assert code == 2, bad_argv
        assert out == ""
        assert "Traceback" not in err

    # fail-closed gate: a negative or zero gate can never pass dirty messages
    for junk_gate, expect in (("-3", 1), ("0", 1)):
        code, out, _ = _run(*base, "--max-violations", junk_gate)
        assert code == expect, junk_gate
        data = json.loads(out)
        assert data["gate"] == int(junk_gate)
        assert data["violation_count"] == 9
        assert data["ok"] is False

    # a large gate passes (9 violations within gate 999) with gate preserved
    code, out, _ = _run(*base, "--max-violations", "999")
    assert code == 0
    data = json.loads(out)
    assert data["gate"] == 999
    assert data["violation_count"] == 9
    assert data["ok"] is False


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


def test_check_broker_scan_success_path(tmp_path, capsys, monkeypatch):
    """README.md:96-97 documents `kgov check --config rules.yaml --broker
    host:port --topic payments` for live topic scanning, but
    test_check_broker_scan_failure_exit_3 pins only the failure leg (exit 3)
    at the main() boundary. The success path — read_topic returning clean
    BrokerMessage values reaching the gate with real per-message violations
    and the documented exit 0/1 decision — is pinned only in cmd_check-level
    tests (fences), never through main(). A clean wire read must exit 0 with
    the full payload shape, a violating read must exit 1 with per-message
    violations carrying the wire position (partition/offset/key), and the
    exact-equality gate boundary must hold through the broker route too."""
    from datetime import datetime

    from kgov.broker import BrokerMessage

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    clean = {"payment_id": "pay_009", "amount_cents": 2500, "currency": "USD",
             "status": "ACCEPTED", "fee_cents": 25}
    viol = {"payment_id": "pay_008", "amount_cents": -5, "currency": "usd",
            "status": "PENDING", "fee_cents": 0}

    holder = {}
    monkeypatch.setattr(
        "kgov.broker.read_topic",
        lambda bootstrap, topic, value_decoder=None, **kw: holder["msgs"],
    )
    monkeypatch.setattr("kgov.broker.AvroRecordDecoder", lambda *a, **k: None)

    def scan(messages):
        holder["msgs"] = [
            BrokerMessage(topic="payments", partition=0, offset=i,
                          key=m["payment_id"], value=m)
            for i, m in enumerate(messages)
        ]

    out = tmp_path / "result.json"

    # clean wire read: gate passes, full payload shape, exit 0
    scan([clean])
    code = main(["check", "--config", rules,
                 "--broker", "127.0.0.1:9092", "--topic", "payments",
                 "-o", str(out)])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"ok", "gate", "violation_count", "rule", "topic",
                         "violations", "generated_at"}
    assert data["ok"] is True
    assert data["gate"] == 0
    assert data["violation_count"] == 0
    assert data["rule"] == "payments-constraints"
    assert data["topic"] == "payments"
    assert data["violations"] == []
    generated_at = data["generated_at"]
    parsed = datetime.fromisoformat(generated_at)
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    assert "ok=True violations=0 gate=0 -> PASS" in capsys.readouterr().err

    # one violating wire message: gate fails, exit 1, violations carry position
    scan([viol])
    code = main(["check", "--config", rules,
                 "--broker", "127.0.0.1:9092", "--topic", "payments",
                 "-o", str(out)])
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is False
    assert data["violation_count"] == 2
    assert data["topic"] == "payments"
    viols = data["violations"]
    assert len(viols) == 2
    assert sorted(v["field"] for v in viols) == ["amount_cents", "currency"]
    assert sorted(v["check"] for v in viols) == ["pattern", "range"]
    assert all(v["partition"] == 0 and v["offset"] == 0
               and v["key"] == "pay_008" for v in viols)
    assert "ok=False violations=2 gate=0 -> FAIL" in capsys.readouterr().err

    # exact-equality gate boundary holds through the broker route
    code = main(["check", "--config", rules,
                 "--broker", "127.0.0.1:9092", "--topic", "payments",
                 "--max-violations", "2", "-o", str(out)])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["gate"] == 2
    assert "ok=False violations=2 gate=2 -> PASS" in capsys.readouterr().err

    code = main(["check", "--config", rules,
                 "--broker", "127.0.0.1:9092", "--topic", "payments",
                 "--max-violations", "1", "-o", str(out)])
    assert code == 1
    assert "ok=False violations=2 gate=1 -> FAIL" in capsys.readouterr().err


def _payment_v1_schema() -> dict:
    """The payments-value schema registered as id 1 in the registry fixture."""
    return {
        "type": "record",
        "name": "Payment",
        "namespace": "com.acme",
        "fields": [
            {"name": "payment_id", "type": "string"},
            {"name": "amount_cents", "type": "long"},
            {"name": "currency", "type": "string"},
            {"name": "status", "type": "string"},
        ],
    }


def _confluent_wire(schema_id: int, record: dict, avro_schema: dict) -> bytes:
    """Confluent wire format: 0x00 magic + 4-byte big-endian schema id + datum."""
    import io
    import json

    from avro import io as avro_io
    from avro import schema as avro_schema_mod

    parsed = avro_schema_mod.parse(json.dumps(avro_schema))
    buf = io.BytesIO()
    writer = avro_io.DatumWriter(parsed)
    writer.write(record, avro_io.BinaryEncoder(buf))
    return b"\x00" + schema_id.to_bytes(4, byteorder="big") + buf.getvalue()


def test_check_broker_schema_registry_decodes_wire_avro(
    registry_server, tmp_path, capsys, monkeypatch
):
    """README.md:98-100 documents --schema-registry: wire messages in
    Confluent-wire Avro (0x00 magic + schema id + binary datum) are decoded
    against the registry before rules run, and decode failure / non-Avro
    values fall back to raw text / hex *without* interrupting the scan.
    test_check_broker_scan_success_path pins the broker route through main()
    with AvroRecordDecoder stubbed out and test_broker pins the decoder at
    library level, but no test drives genuine Avro wire bytes all the way
    through main() with the real decoder wired in. Here only read_topic is
    stubbed (a double applying the real _decode_value, as the live consumer
    path does) so cli._validate's genuine AvroRecordDecoder(SchemaRegistryClient)
    decodes against the live MockRegistry."""
    from datetime import datetime

    from kgov import broker as broker_mod
    from kgov.broker import BrokerMessage

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    schema = _payment_v1_schema()
    raws: list[tuple] = []
    seen: list = []

    def fake_read_topic(bootstrap, topic, value_decoder=None, **kw):
        decoded = []
        msgs = []
        for partition, offset, key, raw in raws:
            value = broker_mod._decode_value(raw, value_decoder)
            decoded.append(value)
            msgs.append(BrokerMessage(topic=topic, partition=partition,
                                      offset=offset, key=key, value=value))
        seen[:] = decoded
        return msgs

    monkeypatch.setattr("kgov.broker.read_topic", fake_read_topic)
    out = tmp_path / "result.json"
    url = registry_server.base_url
    argv = lambda: ["check", "--config", rules,  # noqa: E731
                    "--broker", "127.0.0.1:9092", "--topic", "payments",
                    "--schema-registry", url, "-o", str(out)]

    # (1) clean Avro record decodes to a dict through main() -> gate passes
    raws[:] = [(0, 0, "pay_009", _confluent_wire(
        1, {"payment_id": "pay_009", "amount_cents": 2500,
            "currency": "USD", "status": "ACCEPTED"}, schema))]
    code = main(argv())
    assert code == 0
    assert seen == [{"payment_id": "pay_009", "amount_cents": 2500,
                     "currency": "USD", "status": "ACCEPTED"}]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"ok", "gate", "violation_count", "rule", "topic",
                         "violations", "generated_at"}
    assert data["ok"] is True and data["gate"] == 0
    assert data["violation_count"] == 0
    assert data["rule"] == "payments-constraints" and data["topic"] == "payments"
    assert data["violations"] == []
    parsed = datetime.fromisoformat(data["generated_at"])
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    assert "ok=True violations=0 gate=0 -> PASS" in capsys.readouterr().err

    # (2) violating Avro record: gate fails, violations carry the wire position
    raws[:] = [(3, 7, "pay_008", _confluent_wire(
        1, {"payment_id": "pay_008", "amount_cents": -5,
            "currency": "usd", "status": "PENDING"}, schema))]
    code = main(argv())
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is False and data["gate"] == 0
    assert data["violation_count"] == 2
    assert sorted(v["field"] for v in data["violations"]) == ["amount_cents", "currency"]
    assert sorted(v["check"] for v in data["violations"]) == ["pattern", "range"]
    assert all(v["partition"] == 3 and v["offset"] == 7 and v["key"] == "pay_008"
               for v in data["violations"])
    assert "ok=False violations=2 gate=0 -> FAIL" in capsys.readouterr().err

    # (3) unregistered schema id: registry fetch fails -> raw-text fallback,
    # the scan is NOT interrupted, and the gate is judged on the fallback
    raws[:] = [(1, 2, "pay_x", b"\x00\x00\x00\x27\x0f" + b"garbage-datum")]
    code = main(argv())
    assert code == 1
    assert seen == ["\x00\x00\x00'\x0fgarbage-datum"]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["violation_count"] == 3
    assert sorted(v["field"] for v in data["violations"]) == [
        "amount_cents", "currency", "payment_id"]

    # (4) non-UTF-8 non-Avro bytes: hex fallback, scan still completes
    raws[:] = [(0, 9, "pay_y", b"\xff\xfe\x01")]
    code = main(argv())
    assert code == 1
    assert seen == [{"$bytes": "fffe01"}]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["violation_count"] == 3


def test_check_schema_registry_url_gate_at_main_boundary(monkeypatch) -> None:
    """README:98-100 documents --schema-registry as a Schema Registry URL for
    decoding Confluent-wire Avro. The valid decode path is pinned through
    main() (test_check_broker_schema_registry_decodes_wire_avro) and non-str
    junk is pinned at the handler level (test_cmd_check_junk_schema_registry_-
    fails_closed), but the scheme-less / whitespace STRING axis was never
    pinned through the parser: a truthy junk string ("" , ``"   "``,
    ``"not-a-url"``, ``"localhost:8081"``) used to pass the isinstance-str
    gate and reach SchemaRegistryClient as a dead base_url — every wire
    message then fell back to raw text/hex and a reachable broker passed the
    gate as if it had decoded everything (the false-pass the gate comment
    warns about). The gate (cli.py:275, same http(s)-scheme rule as
    cmd_registry cli.py:426) must reject every junk string BEFORE the lazy
    import and any wire call: clean CliError -> exit 3, empty stdout, no
    read_topic reach. A well-formed http(s) URL still wires the decoder and
    scans (fence, not broken)."""
    from kgov.broker import AvroRecordDecoder

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    calls: list = []

    def fake_read(bootstrap, topic, value_decoder=None, **kw):
        calls.append(value_decoder)
        return []

    monkeypatch.setattr("kgov.broker.read_topic", fake_read)
    base = ["check", "--config", rules, "--broker", "127.0.0.1:9092",
            "--topic", "payments"]

    # every junk string dies at the URL gate: exit 3, clean error, no wire call
    for junk in ("", "   ", "\t\n", "not-a-url", "abc", "localhost:8081",
                 "ftp://x", "http:", "https:x", "://x"):
        calls.clear()
        code, out, err = _run(*base, "--schema-registry", junk)
        assert code == 3, junk
        assert out == ""
        assert f"--schema-registry must be an http(s) URL, got {junk!r}" in err, junk
        assert "Traceback" not in err, junk
        assert calls == [], f"junk {junk!r} reached the wire"

    # a well-formed http(s) URL wires an AvroRecordDecoder and scans (fence)
    calls.clear()
    code, out, err = _run(*base, "--schema-registry", "http://127.0.0.1:8081")
    assert code == 0
    assert len(calls) == 1 and isinstance(calls[0], AvroRecordDecoder)
    assert json.loads(out)["ok"] is True

    # without the flag no decoder is wired (fence)
    calls.clear()
    code, out, _ = _run(*base)
    assert code == 0
    assert calls == [None]


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


def test_report_csv_full_row_values(tmp_path):
    """README.md:130-132 documents the report CSV contract (12-column header,
    one row per topic). test_report_csv only asserts the header prefix and
    test_examples.py pins header + topic order at the process boundary, but the
    per-topic data row VALUES (owner, tags joined by '|', partitions,
    replication_factor, schema_version/schema_state from --registry,
    last_consumed_at from --groups, the staleness decision and
    violation_count) are never pinned through main() against
    tests/fixtures/. This test pins the exact rendered rows plus the
    file-vs-stdout boundary: csv.DictWriter renders \\r\\n on stdout while the
    -o file write strips the \\r (raw identity file == out.replace('\\r\\n','\\n')),
    and both legs keep stderr empty."""
    base = [
        "report",
        "--topics", str(FIXTURES / "topics" / "topics.json"),
        "--registry", str(FIXTURES / "registry" / "registry.json"),
        "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
        "--format", "csv",
    ]
    # stdout leg: \r\n row endings and empty stderr
    code, out, err = _run(*base)
    assert code == 0
    assert err == ""
    assert out.endswith("\r\n")
    lines = out.splitlines()
    assert lines == [
        "name,owner,tags,partitions,replication_factor,schema_subject,"
        "schema_version,schema_state,latest_message_at,last_consumed_at,"
        "stale,violation_count",
        # registry latest version 2 + no breaking diff betwen v1/v2 -> COMPATIBLE
        "payments,payments-team,core|critical,12,3,payments-value,2,COMPATIBLE,"
        "2026-11-13T08:15:00+00:00,2026-11-14T07:00:00+00:00,True,0",
        # orders v1->v2 drops a field -> BREAKING; last consumed 2026-09-20 leaks.
        "orders,checkout-eng,core,6,3,orders-value,2,BREAKING,"
        "2026-11-10T11:30:00+00:00,2026-09-20T10:00:00+00:00,True,0",
        # no schema_subject -> empty pair + NONE state
        "inventory-events,warehouse,event-log,3,2,,,NONE,"
        "2026-10-01T00:00:00+00:00,2026-11-13T22:00:00+00:00,True,0",
    ]
    # -o leg: stdout stays empty and the file matches the stdout render with
    # the \r of each \r\n row ending stripped
    out_path = tmp_path / "report.csv"
    code, file_out, file_err = _run(*base, "-o", str(out_path))
    assert code == 0
    assert file_out == ""
    assert file_err == ""
    file_text = out_path.read_text(encoding="utf-8")
    assert file_text == out.replace("\r\n", "\n")
    assert "\r" not in file_text
    assert file_text.splitlines() == lines


def test_report_cli_output_file_success_writes_json(tmp_path, monkeypatch) -> None:
    """README.md:127: ``kgov report --topics topics.json [--registry ...]
    [--groups ...] [--format json|csv] [-o report.json|report.csv]`` documents
    the -o flag writing report output to a file. test_report_rules_messages_
    accumulate_violations already writes a default-format JSON file and the .csv
    legs are pinned by test_report_csv_full_row_values, but nothing pins the
    explicit ``--format json`` route to a file and checks it against the stdout
    render. This test does: exit 0 with the same rendered JSON (top-level
    ``{"topics", "total_topics", "generated_at"}``) in the file as the stdout
    route emits."""
    _freeze_clock(monkeypatch)
    base = ["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
            "--registry", str(FIXTURES / "registry" / "registry.json"),
            "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
            "--format", "json"]

    # stdout leg: the JSON payload with a trailing newline and empty stderr
    code, out, err = _run(*base)
    assert code == 0
    assert err == ""
    data = json.loads(out)
    assert set(data) == {"topics", "total_topics", "generated_at"}
    assert data["total_topics"] == 3
    assert out.endswith("\n")

    # -o leg: stdout stays empty, the file is the identical render minus the
    # trailing newline the stdout print adds
    out_path = tmp_path / "report.json"
    code, out_file, err_file = _run(*base, "-o", str(out_path))
    assert code == 0
    assert out_file == ""
    assert err_file == ""
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data
    assert file_text == out.rstrip("\n")


def test_report_fixture_output_mode_precedence(tmp_path):
    """README:127 documents report's ``[--format json|csv] [-o report.json|
    report.csv]`` output axes; when both signals are given they can conflict.
    report resolves deterministically (cli.py:525-528): a ``.csv`` **extension**
    on -o coerces the format to csv regardless of an explicit ``--format json``
    (the same json-wins style rule command register pins for schema-diff, here
    csv-wins), while an explicit ``--format csv`` is NOT undone by a ``.json``
    extension — only the ``.csv`` suffix coerces and the match is
    case-sensitive (``x.CSV`` falls through to json). Pins that precedence at
    the main() boundary (file content, not just the exit code) so a future
    reorder of the fmt/output branches cannot silently flip which output
    format wins when the user mixes signals."""
    base = ["report", "--topics", str(FIXTURES / "topics" / "topics.json"),
            "--registry", str(FIXTURES / "registry" / "registry.json"),
            "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json")]

    # reference CSV rows (report route) for content assertions
    _, csv_out, _ = _run(*base, "--format", "csv")
    csv_lines = csv_out.splitlines()
    assert csv_lines[0].startswith("name,")

    # --format csv -o x.json: the explicit format holds (json ext does NOT
    # coerce back); the file is the csv render with \n row endings
    mix_path = tmp_path / "precedence.json"
    code, out, err = _run(*base, "--format", "csv", "-o", str(mix_path))
    assert code == 0
    assert out == ""
    assert err == ""
    file_text = mix_path.read_text(encoding="utf-8")
    assert file_text.splitlines() == csv_lines
    assert "\r" not in file_text
    assert not file_text.lstrip().startswith("{")

    # --format json -o x.csv: the .csv extension wins (csv render)
    ext_path = tmp_path / "precedence.csv"
    code, out, err = _run(*base, "--format", "json", "-o", str(ext_path))
    assert code == 0
    assert out == ""
    assert err == ""
    ext_text = ext_path.read_text(encoding="utf-8")
    assert ext_text.splitlines() == csv_lines
    assert "\r" not in ext_text

    # no explicit format + .csv extension: csv is chosen by extension alone
    nofmt_path = tmp_path / "nofmt.csv"
    code, out, err = _run(*base, "-o", str(nofmt_path))
    assert code == 0
    assert out == ""
    assert err == ""
    assert nofmt_path.read_text(encoding="utf-8").splitlines() == csv_lines

    # case-sensitive suffix match: x.CSV is NOT csv (falls through to json)
    case_path = tmp_path / "uppercase.CSV"
    code, out, err = _run(*base, "--format", "json", "-o", str(case_path))
    assert code == 0
    assert out == ""
    assert err == ""
    assert case_path.read_text(encoding="utf-8").lstrip().startswith("{")


def test_export_report_output_mode_precedence_inherits(tmp_path):
    """cmd_export routes --type report to cmd_report (cli.cmd_export:539-572),
    so the export route must inherit report's csv-wins output-mode precedence
    (cli.py:525-528): a ``.csv`` -o extension coerces a conflicting explicit
    ``--format json`` to csv, and an explicit ``--format csv`` is not undone
    by a ``.json`` extension. The direct report route is pinned by
    test_report_fixture_output_mode_precedence; the two conflict legs through
    the export boundary never were."""
    exp = ["export", "--type", "report",
           "--topics", str(FIXTURES / "topics" / "topics.json"),
           "--registry", str(FIXTURES / "registry" / "registry.json"),
           "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json")]

    _, rep_csv, _ = _run("report",
                         "--topics", str(FIXTURES / "topics" / "topics.json"),
                         "--registry", str(FIXTURES / "registry" / "registry.json"),
                         "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
                         "--format", "csv")
    csv_lines = rep_csv.splitlines()

    ext_path = tmp_path / "export.csv"
    code, out, err = _run(*exp, "--format", "json", "-o", str(ext_path))
    assert code == 0
    assert out == ""
    assert err == ""
    assert ext_path.read_text(encoding="utf-8").splitlines() == csv_lines

    mix_path = tmp_path / "export.json"
    code, out, err = _run(*exp, "--format", "csv", "-o", str(mix_path))
    assert code == 0
    assert out == ""
    assert err == ""
    assert mix_path.read_text(encoding="utf-8").splitlines() == csv_lines


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


def test_report_violations_accumulates_per_topic_counts(tmp_path, capsys):
    """README.md:126 documents --violations violations.json as an alternate
    source to --rules --messages; the four existing --violations pins are all
    junk legs (non-UTF-8, non-string topic, wrapper/junk shapes) and never pin
    the positive path. A well-formed flat-list file must accumulate per-topic
    violation_count for matching topics only, and the dict-wrapper form must
    behave identically to the flat list (report._count_violations per-topic
    counting reached through the CLI boundary)."""
    topics = str(FIXTURES / "topics" / "topics.json")
    flat = tmp_path / "vio_flat.json"
    flat.write_text('[{"topic": "payments", "field": "amount_cents"}, '
                    '{"topic": "payments", "field": "currency"}, '
                    '{"topic": "orders", "field": "x"}]', encoding="utf-8")
    code = main(["report", "--topics", topics, "--violations", str(flat)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    counts = {t["name"]: t["violation_count"] for t in data["topics"]}
    assert counts == {"payments": 2, "orders": 1, "inventory-events": 0}

    wrap = tmp_path / "vio_wrap.json"
    wrap.write_text('{"violations": [{"topic": "payments", "message": "a"}, '
                    '{"topic": "payments", "message": "b"}]}', encoding="utf-8")
    code = main(["report", "--topics", topics, "--violations", str(wrap)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    counts = {t["name"]: t["violation_count"] for t in data["topics"]}
    assert counts == {"payments": 2, "orders": 0, "inventory-events": 0}


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
    """export --type schema-diff --format markdown -o must write the identical
    markdown render to the file as the stdout route emits (modulo the trailing
    newline the stdout print adds), with the -o leg keeping stdout empty and
    the gate summary on stderr — the same contract the JSON leg pins in
    test_export_schema_diff_json_full_payload_file_vs_stdout."""
    before = str(FIXTURES / "schemas" / "v1.json")
    after = str(FIXTURES / "schemas" / "v2.json")
    base = ["export", "--type", "schema-diff", "--format", "markdown",
            "--before", before, "--after", after]

    code, out, err = _run(*base)
    assert code == 0
    assert out.lstrip().startswith("#")
    assert "Additive" in out
    assert err == "SCHEMA DIFF: Additive\n"
    assert out.endswith("\n")

    out_path = tmp_path / "diff.md"
    code, out_file, err_file = _run(*base, "-o", str(out_path))
    assert code == 0
    assert out_file == ""  # -o leg keeps stdout empty
    assert err_file == "SCHEMA DIFF: Additive\n"
    body = out_path.read_text(encoding="utf-8")
    assert body.lstrip().startswith("#")
    assert not body.lstrip().startswith("{")
    assert "Additive" in body
    # raw texts differ only by the trailing newline the stdout print adds
    assert body == out.rstrip("\n")

    # breaking fixtures still write the -o file while exiting 1, keeping the
    # same stdout-empty/stderr-summary routing as the additive leg
    breaking = tmp_path / "breaking.md"
    base_break = ["export", "--type", "schema-diff", "--format", "markdown",
                  "--before", str(FIXTURES / "schemas" / "breaking_before.json"),
                  "--after", str(FIXTURES / "schemas" / "breaking_after.json")]
    code, out_br, err_br = _run(*base_break, "-o", str(breaking))
    assert code == 1
    assert out_br == ""
    assert err_br == "SCHEMA DIFF: Breaking (incompatible)\n"
    break_body = breaking.read_text(encoding="utf-8")
    assert break_body.lstrip().startswith("#")
    assert not break_body.lstrip().startswith("{")
    assert "Compatibility: **Breaking** (INCOMPATIBLE)" in break_body

    # --format md (p_exp choices, cli.py:658) is the shorthand for markdown:
    # byte-identical stdout render and the same -o file identity contract,
    # while a report export asking for markdown fails closed (exit 3) instead
    code, out_sh, err_sh = _run(*base, "--format", "md")
    assert code == 0
    assert out_sh == out
    assert err_sh == "SCHEMA DIFF: Additive\n"
    shorthand = tmp_path / "shorthand.md"
    code, out_shf, err_shf = _run(*base, "--format", "md", "-o", str(shorthand))
    assert code == 0
    assert out_shf == ""
    assert shorthand.read_text(encoding="utf-8") == out_sh.rstrip("\n")

    code, _, err_rep = _run("export", "--type", "report",
                            "--topics", str(FIXTURES / "topics" / "topics.json"),
                            "--format", "md")
    assert code == 3
    assert "report export supports --format csv|json" in err_rep


def test_export_schema_diff_json_full_payload_file_vs_stdout(tmp_path) -> None:
    """README.md's schema-diff section documents ``[-o diff.md|diff.json]``.
    test_export_schema_diff_md only pins the markdown file's "#" prefix, and no
    test covers the export route's default ``--format json`` stdout render or
    its -o file-vs-stdout identity at the main() boundary. This test pins the
    full JSON payload (kind/results/compatible), the empty-stdout + gate-summary
    stderr routing of the -o leg, the trailing-newline-only text difference
    between the stdout print and the raw file write, the identical rendering of
    the default format, and the breaking-fixture leg (rc 1 with the file still
    written)."""
    v1 = str(FIXTURES / "schemas" / "v1.json")
    v2 = str(FIXTURES / "schemas" / "v2.json")
    base = ["export", "--type", "schema-diff", "--format", "json",
            "--before", v1, "--after", v2]

    code, out, err = _run(*base)
    assert code == 0
    data = json.loads(out)
    assert data["kind"] == "Additive"
    assert data["compatible"] is True
    assert data["subject"] == "record"
    assert data["format"] == "avro"
    assert data["results"] == [{"kind": "Additive", "path": "email",
                                "message": "field added (optional): `email`"}]
    assert err == "SCHEMA DIFF: Additive\n"
    assert out.endswith("\n")  # stdout print adds a trailing newline

    out_path = tmp_path / "diff.json"
    code, out_file, err_file = _run(*base, "-o", str(out_path))
    assert code == 0
    assert out_file == ""  # -o leg keeps stdout empty
    assert err_file == "SCHEMA DIFF: Additive\n"
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data
    assert file_text == out.rstrip("\n")  # raw texts differ only by that newline

    # default --format json renders the identical payload on stdout
    code, out_default, _ = _run(*base[:3], *base[5:])  # drop --format json
    assert code == 0
    assert json.loads(out_default) == data

    # breaking fixtures still write the -o file while exiting 1
    breaking = tmp_path / "breaking.json"
    code, out_b, err_b = _run(*base[:-4],  # drop --before/--after values
                              "--before", str(FIXTURES / "schemas" / "breaking_before.json"),
                              "--after", str(FIXTURES / "schemas" / "breaking_after.json"),
                              "-o", str(breaking))
    assert code == 1
    assert json.loads(breaking.read_text(encoding="utf-8"))["kind"] == "Breaking"
    assert err_b == "SCHEMA DIFF: Breaking (incompatible)\n"


def test_export_report_csv(tmp_path):
    out = tmp_path / "report.csv"
    code = main(["export", "--type", "report", "--format", "csv",
                 "--topics", str(FIXTURES / "topics" / "topics.json"), "-o", str(out)])
    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("name,")


def test_export_report_csv_full_rows_identical_to_report_route(tmp_path) -> None:
    """README.md:149 documents ``kgov export --type report --topics topics.json
    --format csv -o report.csv``. test_export_report_csv only asserts the file's
    header prefix and test_examples pins stdout header + topic order at the
    process boundary, but nothing pins the export route's full per-topic data
    rows, nor the -o file against the stdout render. This test pins the exact
    12-column rows (mirroring test_report_csv_full_row_values for the direct
    report route) and asserts the export -o file writes the identical rows and
    the direct ``report --format csv`` route renders the same lines."""
    base = ["export", "--type", "report",
            "--topics", str(FIXTURES / "topics" / "topics.json"),
            "--registry", str(FIXTURES / "registry" / "registry.json"),
            "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
            "--format", "csv"]
    code, out, err = _run(*base)
    assert code == 0
    # CSV renders \r\n row endings at the stdout boundary (csv.DictWriter in
    # report.report_to_csv passes through _write_out unchanged), unlike JSON's
    # trailing "\n" — pin that divergence and the empty stderr leg
    assert err == ""
    assert out.endswith("\r\n")
    lines = out.splitlines()
    assert lines == [
        "name,owner,tags,partitions,replication_factor,schema_subject,"
        "schema_version,schema_state,latest_message_at,last_consumed_at,"
        "stale,violation_count",
        # registry latest version 2 + no breaking diff between v1/v2 -> COMPATIBLE
        "payments,payments-team,core|critical,12,3,payments-value,2,COMPATIBLE,"
        "2026-11-13T08:15:00+00:00,2026-11-14T07:00:00+00:00,True,0",
        # orders v1->v2 drops a field -> BREAKING; last consumed 2026-09-20 leaks.
        "orders,checkout-eng,core,6,3,orders-value,2,BREAKING,"
        "2026-11-10T11:30:00+00:00,2026-09-20T10:00:00+00:00,True,0",
        # no schema_subject -> empty pair + NONE state
        "inventory-events,warehouse,event-log,3,2,,,NONE,"
        "2026-10-01T00:00:00+00:00,2026-11-13T22:00:00+00:00,True,0",
    ]
    # -o file writes rows identical to the stdout render: the file write strips
    # nothing but the trailing newline's \r is removed (Path.write_text on the
    # csv \r\n stream -> \n file), so raw identity is text == out.replace(\r\n,\n)
    # and stdout must be empty on the -o leg
    out_path = tmp_path / "export_report.csv"
    code, file_out, file_err = _run(*base, "-o", str(out_path))
    assert code == 0
    assert file_out == ""
    assert file_err == ""
    file_text = out_path.read_text(encoding="utf-8")
    assert file_text == out.replace("\r\n", "\n")
    assert file_text.splitlines() == lines
    # cross-route equivalence: the direct report --format csv route (pinned
    # row-for-row by test_report_csv_full_row_values) renders the same rows
    code, rep_out, _ = _run("report",
                            "--topics", str(FIXTURES / "topics" / "topics.json"),
                            "--registry", str(FIXTURES / "registry" / "registry.json"),
                            "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
                            "--format", "csv")
    assert code == 0
    assert rep_out.splitlines() == lines


def test_export_report_cli_output_file_success_writes_json(tmp_path, monkeypatch) -> None:
    """cmd_export routes --type report to cmd_report (cli.cmd_export:539-572),
    so the export route must inherit report's file-vs-stdout contract at the
    main() boundary: byte-identical JSON (modulo the trailing newline a stdout
    print adds) between the -o file and the stdout render, stdout empty on the
    -o leg, and the default (no --format) export report -o file writing the
    same JSON. test_report_cli_output_file_success_writes_json pins only the
    direct report route, and test_export_report_csv_full_rows_identical_to_
    report_route covers only --format csv, never the export JSON -o leg."""
    _freeze_clock(monkeypatch)
    base = ["export", "--type", "report",
            "--topics", str(FIXTURES / "topics" / "topics.json"),
            "--registry", str(FIXTURES / "registry" / "registry.json"),
            "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
            "--format", "json"]
    code, out, err = _run(*base)
    assert code == 0
    assert err == ""  # stdout leg: stderr stays empty
    data = json.loads(out)  # unsorted, so json.loads order-independent below
    assert set(data) == {"generated_at", "topics", "total_topics"}
    assert data["total_topics"] == 3
    assert out.endswith("\n")

    out_path = tmp_path / "kgovern_report.json"
    code, out_file, err_file = _run(*base, "-o", str(out_path))
    assert code == 0
    assert out_file == ""  # -o route keeps stdout empty
    assert err_file == ""
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data
    assert out.rstrip("\n") == file_text
    assert out_path.read_text(encoding="utf-8").endswith("\n") is False

    # default format (no --format) export report -o file writes the same JSON
    default_path = tmp_path / "kgovern_default.json"
    code, _, _ = _run("export", "--type", "report",
                      "--topics", str(FIXTURES / "topics" / "topics.json"),
                      "--registry", str(FIXTURES / "registry" / "registry.json"),
                      "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
                      "-o", str(default_path))
    assert code == 0
    assert json.loads(default_path.read_text(encoding="utf-8")) == data

    # cross-route equivalence: the direct report --format json route renders
    # the same payload as the export route's -o file
    code, rep_out, _ = _run("report",
                            "--topics", str(FIXTURES / "topics" / "topics.json"),
                            "--registry", str(FIXTURES / "registry" / "registry.json"),
                            "--groups", str(FIXTURES / "subject_groups" / "consumer_groups.json"),
                            "--format", "json")
    assert code == 0
    assert json.loads(rep_out) == data


def test_export_check_stdout_no_attribute_error(capsys):
    """export --type check must not crash on the missing --quiet attribute."""
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    messages = FIXTURES / "messages" / "payments_messages.json"
    code = main(["export", "--type", "check", "--config", str(rules),
                 "--messages", str(messages)])
    assert code == 1  # fixture messages are intentionally dirty
    data = json.loads(capsys.readouterr().out)
    assert len(data["violations"]) == 9


def test_export_check_cli_output_file_success_writes_json(tmp_path, monkeypatch):
    """cmd_export routes --type check to cmd_check (cli.cmd_export:539-553), so
    the export route must inherit check's file-vs-stdout contract at the main()
    boundary: byte-identical JSON (modulo the trailing newline a stdout print
    adds) between the -o file and the stdout render, stdout empty on the -o
    leg, and the gate summary on stderr. test_export_check_stdout_no_attribute_
    error only pins the stdout leg; test_check_cli_output_file_success_writes_
    json pins the direct check route, never the export parser."""
    _freeze_clock(monkeypatch)
    rules = FIXTURES / "rules" / "rules_scale.yaml"
    msgs = FIXTURES / "messages" / "payments_messages.json"
    base = ["export", "--type", "check", "--format", "json",
            "--config", str(rules), "--messages", str(msgs)]

    code, out, err = _run(*base)
    assert code == 1  # fixture messages are intentionally dirty
    data_stdout = json.loads(out)
    assert err == ""  # stdout leg: summary stays off stderr
    assert out.endswith("\n")

    out_path = tmp_path / "kgovern_export.json"
    code, out_file, err_file = _run(*base, "-o", str(out_path))
    assert code == 1
    assert out_file == ""  # -o route keeps stdout empty
    assert err_file == "ok=False violations=9 gate=0 -> FAIL\n"
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data_stdout
    # raw texts differ only by the trailing newline the stdout print adds
    assert out.rstrip("\n") == file_text

    # --type violations is an alias: same gate, same -o contract
    alias_path = tmp_path / "kgovern_violations.json"
    code, _, _ = _run("export", "--type", "violations", "--format", "json",
                      "--config", str(rules), "--messages", str(msgs),
                      "-o", str(alias_path))
    assert code == 1
    assert json.loads(alias_path.read_text(encoding="utf-8"))["violation_count"] == 9

    # --max-violations 9 passes the gate and still writes the -o file
    pass_path = tmp_path / "kgovern_pass.json"
    code, out_pass, err_pass = _run(*base, "--max-violations", "9",
                                    "-o", str(pass_path))
    assert code == 0
    assert out_pass == ""
    assert err_pass == "ok=False violations=9 gate=9 -> PASS\n"
    assert pass_path.read_text(encoding="utf-8").endswith("\n") is False


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


def test_export_check_alias_format_ladder_at_main_boundary(tmp_path):
    """The export parser accepts --format json|csv|markdown|md (cli.py:662),
    so an unsupported value for a check-routed type is a *handler* failure,
    not a parser one. markdown/md pass the export parser (unlike the direct
    check parser, whose choices=[json] makes them a usage error exit 2,
    pinned in test_direct_subcommand_format_axes_pinned) and must be rejected
    by _require_export_format (cli.py:579-591) as a clean exit-3 CliError;
    out-of-choices values (xml/html/yaml) never leave the parser -> exit 2.
    The violations alias (cli.py:570 routes both check and violations to
    cmd_check) must honor --quiet and the -o leg exactly like the check route
    (pinned in test_check_quiet_suppresses_stdout_at_main_boundary), which
    only ever pinned the export --type check quiet leg."""
    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")

    # markdown/md are valid export-parser choices but check cannot render
    # them: exit 3 with a clean CliError naming the rendered formats.
    for typ in ("check", "violations"):
        for fmt in ("markdown", "md"):
            code, out, err = _run("export", "--type", typ, "--format", fmt,
                                  "--config", rules, "--messages", msgs)
            assert code == 3, (typ, fmt)
            assert out == ""
            assert f"{typ} export supports --format json, got {fmt!r}" in err
            assert "Traceback" not in err

    # out-of-choices values stop at the parser (exit 2, empty stdout).
    for typ in ("check", "violations"):
        for junk in ("xml", "html", "yaml"):
            code, out, err = _run("export", "--type", typ, "--format", junk,
                                  "--config", rules, "--messages", msgs)
            assert code == 2, (typ, junk)
            assert out == ""
            assert "invalid choice" in err and "Traceback" not in err

    # violations alias honors --quiet: stdout fully suppressed, exit 1 over
    # the gate, no stderr.
    code, out, err = _run("export", "--type", "violations", "--format", "json",
                          "--quiet", "--config", rules, "--messages", msgs)
    assert code == 1
    assert out == "" and err == ""

    # violations alias --quiet with -o: file still carries the payload,
    # stdout empty, summary stays on stderr.
    out_file = tmp_path / "violations_quiet.json"
    code, out, err = _run("export", "--type", "violations", "--format", "json",
                          "--quiet", "-o", str(out_file),
                          "--config", rules, "--messages", msgs)
    assert code == 1
    assert out == ""
    assert json.loads(out_file.read_text(encoding="utf-8"))["violation_count"] == 9
    assert "ok=False violations=9 gate=0 -> FAIL" in err


def test_export_check_wire_validation_at_main_boundary(monkeypatch):
    """export --type check/violations routes to cmd_check (cli.py:570), so it
    must inherit every wire-validation the direct check route enforces BEFORE
    any broker/registry contact. The direct-route legs are pinned at
    test_check_requires_data_source (802), test_check_topic_without_broker_
    exit_3 (808) and test_check_schema_registry_url_gate_at_main_boundary
    (1113); the export route was only ever pinned with a real --messages
    fixture (test_export_check_cli_output_file_success_writes_json, 1972), and
    test_export_check_quiet... only covered the --quiet flag. This pin proves
    the alias route fails closed identically: no data source -> exit-3
    CliError, --topic without --broker -> exit 3, a junk --schema-registry
    string dies at the URL gate (cli.py:275) with no read_topic reach, while
    a well-formed http(s) URL wires an AvroRecordDecoder (fence) and the
    no-flag fence wires None — all through the export parser."""
    from kgov.broker import AvroRecordDecoder

    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    calls: list = []

    def fake_read(bootstrap, topic, value_decoder=None, **kw):
        calls.append(value_decoder)
        return []

    monkeypatch.setattr("kgov.broker.read_topic", fake_read)
    base = ["export", "--type", "check", "--format", "json",
            "--config", rules]

    # no data source: export must fail closed like direct check (exit 3)
    code, out, err = _run(*base)
    assert code == 3
    assert out == ""
    assert "check needs a data source" in err
    assert "Traceback" not in err

    # violations alias: same no-data-source failure
    code, out, err = _run(*base, "--type", "violations")
    assert code == 3
    assert "check needs a data source" in err

    # --topic without --broker: exit 3, never a wire call
    calls.clear()
    code, out, err = _run(*base, "--topic", "payments")
    assert code == 3
    assert out == ""
    assert "--topic requires --broker" in err
    assert calls == []

    # every junk --schema-registry string dies at the URL gate before any
    # read_topic reach (same ladder as the direct route pin, cli.py:275)
    for junk in ("", "   ", "not-a-url", "localhost:8081", "http:", "https:x", "://x"):
        calls.clear()
        code, out, err = _run(*base, "--broker", "127.0.0.1:9092",
                              "--topic", "payments", "--schema-registry", junk)
        assert code == 3, junk
        assert out == ""
        assert f"--schema-registry must be an http(s) URL, got {junk!r}" in err, junk
        assert "Traceback" not in err, junk
        assert calls == [], f"junk {junk!r} reached the wire"

    # a well-formed http(s) URL wires an AvroRecordDecoder and scans (fence)
    calls.clear()
    code, out, err = _run(*base, "--broker", "127.0.0.1:9092",
                          "--topic", "payments",
                          "--schema-registry", "http://127.0.0.1:8081")
    assert code == 0
    assert len(calls) == 1 and isinstance(calls[0], AvroRecordDecoder)
    assert json.loads(out)["ok"] is True

    # without the flag no decoder is wired (fence)
    calls.clear()
    code, out, _ = _run(*base, "--broker", "127.0.0.1:9092",
                        "--topic", "payments")
    assert code == 0
    assert len(calls) == 1 and calls[0] is None


def test_export_missing_required_args_ladder_at_main_boundary():
    """The export parser (cli.py:659-679) makes every data flag optional --
    --config/--topics/--before/--after are not marked required like the
    direct-subcommand parsers (schema-diff:608-609, check:622, report:649) --
    so required-arg enforcement falls to the handlers as exit-3 CliError
    naming each requirement. The direct routes are pinned at
    test_schema_diff_missing_file (452), test_check_missing_config (816) and
    test_report_requires_topics (1393); the export route was only ever pinned
    with --format csv asserting a bare exit 3 (test_export_unknown_type_
    fails_closed, 2283-2286). This pin nails the exact exit-3 ladder for all
    four real export types: schema-diff needs --before and --after (partial/
    one-sided still fails), check/violations need --config <rules.yaml>, and
    report needs --topics <topics.json> -- always a clean CliError on stderr,
    never a Traceback, never exit 2 (the parser accepts every trailing flag)."""
    rules = str(FIXTURES / "rules" / "rules_scale.yaml")

    # schema-diff: both --before and --after required; a partial pair still
    # fails closed with the same message, not a late crash on the missing side
    for suffix in ([], ["--before", "x.json"], ["--after", "x.json"]):
        code, out, err = _run("export", "--type", "schema-diff",
                              "--format", "json", *suffix)
        assert code == 3, suffix
        assert out == ""
        assert "schema-diff requires --before and --after" in err
        assert "Traceback" not in err

    # check: --config is required; a missing file fails with the file-exists
    # variant, an absent flag with the : got None variant. The missing-file
    # leg must take precedence over the not-yet-checked data source.
    code, out, err = _run("export", "--type", "check", "--format", "json")
    assert code == 3
    assert out == ""
    assert "check requires --config <rules.yaml>: got None" in err
    assert "Traceback" not in err

    code, out, err = _run("export", "--type", "check", "--format", "json",
                          "--config", "/no/rules.yaml", "--messages", "m.json")
    assert code == 3
    assert out == ""
    assert "check requires --config <rules.yaml> (file must exist)" in err
    assert "Traceback" not in err

    # violations alias: identical --config enforcement as the check route
    code, out, err = _run("export", "--type", "violations", "--format", "json")
    assert code == 3
    assert out == ""
    assert "check requires --config <rules.yaml>: got None" in err

    # report: --topics required before any --registry/--rules/messages are
    # consulted; a fully valid-looking report invocation without --topics
    # still fails closed naming the requirement.
    code, out, err = _run("export", "--type", "report", "--format", "json")
    assert code == 3
    assert out == ""
    assert "report requires --topics <topics.json>: got None" in err
    assert "Traceback" not in err

    code, out, err = _run("export", "--type", "report", "--format", "csv",
                          "--registry", "r.json", "--rules", "ru.json",
                          "--messages", "m.json", "--violations", "v.json")
    assert code == 3
    assert out == ""
    assert "report requires --topics <topics.json>: got None" in err
    assert "Traceback" not in err


def test_export_max_violations_feed_ladder_at_main_boundary():
    """The export parser accepts --max-violations type=int (cli.py:676) and
    routes to cmd_check (cli.py:570), so the export route must inherit the
    same feed-gate ladder as direct check. test_check_usage_error_exit_2_and_
    gate_fail_closed (750) pins the direct route; only the pass leg is pinned
    on the export route (test_export_check_cli_output_file_success_writes_json,
    2010-2017). This pin closes the gap: junk values die at the parser as
    exit-2 usage errors BEFORE the handler (argparse type=int rejects first,
    so the same junk without a data source is still exit 2, never the exit-3
    data-source CliError), while valid gates keep the fail-closed semantics
    (cli.py:366): a negative or zero gate can never pass the dirty fixture
    (exit 1, gate and violation_count preserved), a large gate passes (exit 0,
    gate preserved), and the violations alias honors the identical ladder."""
    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")
    base = ["export", "--type", "check", "--format", "json",
            "--config", rules, "--messages", msgs]

    # exit 2: parser usage errors on the --max-violations axis
    for bad_argv in [["--max-violations", "abc"],
                     ["--max-violations", "3.5"],
                     ["--max-violations", "--quiet"],
                     ["--max-violations"]]:
        code, out, err = _run(*base, *bad_argv)
        assert code == 2, bad_argv
        assert out == ""
        assert "error:" in err
        assert "Traceback" not in err

        # argparse rejects before the handler: the same bad value without a
        # data source is still a usage error, never the exit-3 CliError
        code, out, err = _run("export", "--type", "check", "--format", "json",
                              "--config", rules, *bad_argv)
        assert code == 2, bad_argv
        assert out == ""
        assert "Traceback" not in err

    # fail-closed gate: a negative or zero gate can never pass dirty messages
    for junk_gate, expect in (("-3", 1), ("0", 1)):
        code, out, _ = _run(*base, "--max-violations", junk_gate)
        assert code == expect, junk_gate
        data = json.loads(out)
        assert data["gate"] == int(junk_gate)
        assert data["violation_count"] == 9
        assert data["ok"] is False

    # a large gate passes (9 violations within gate 999) with gate preserved
    code, out, _ = _run(*base, "--max-violations", "999")
    assert code == 0
    data = json.loads(out)
    assert data["gate"] == 999
    assert data["violation_count"] == 9
    assert data["ok"] is False

    # violations alias: same jail-on-junk, same fail-closed gate
    code, out, _ = _run("export", "--type", "violations", "--format", "json",
                        "--config", rules, "--messages", msgs,
                        "--max-violations", "8")
    assert code == 1
    data = json.loads(out)
    assert data["gate"] == 8
    assert data["violation_count"] == 9
    assert data["ok"] is False


def test_export_check_junk_fixture_ladder_at_main_boundary(tmp_path):
    """The export parser routes check/violations into cmd_check (cli.py:570)
    and schema-diff into cmd_schema_diff, so junk fixtures must fail closed
    with the same clean errors as the direct routes (pinned at
    test_check_non_utf8_rules_exit_3:832, test_check_scalar_messages_fixture_
    exit_3:821, test_check_non_numeric_range_bound_exit_3:844, test_check_non_
    utf8_messages_exit_3:857, test_schema_diff_non_utf8_exit_3:867) — never a
    raw UnicodeDecodeError/TypeError/ValueError traceback escaping main()."""
    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")

    bad_rules = tmp_path / "rules.yaml"
    bad_rules.write_bytes(b"{topic: \xff\xfe}")
    scalar_msgs = tmp_path / "msgs.json"
    scalar_msgs.write_text("null", encoding="utf-8")
    bad_range = tmp_path / "range.yaml"
    bad_range.write_text(
        "version: 1\ntopic: payments\nfields:\n"
        "  amount_cents:\n    type: integer\n"
        "    range: {minimum: abc}\n", encoding="utf-8")
    bad_msgs = tmp_path / "badmsgs.json"
    bad_msgs.write_bytes(b'{"messages": \xff\xfe[]}')
    bad_schema = tmp_path / "bad.json"
    bad_schema.write_bytes(b'\xff\xfe{"type": "record"}')
    after = str(FIXTURES / "schemas" / "v1.json")

    # export check route: every junk fixture exits 3 with its designed message
    for name, argv, want in [
        ("non-utf8 rules", ["export", "--type", "check", "--format", "json",
                            "--config", str(bad_rules), "--messages", msgs],
         "bad rules config"),
        ("scalar messages", ["export", "--type", "check", "--format", "json",
                             "--config", rules, "--messages", str(scalar_msgs)],
         "bad message fixture"),
        ("non-numeric range bound", ["export", "--type", "check", "--format",
                                     "json", "--config", str(bad_range),
                                     "--messages", msgs],
         "bad rules config"),
        ("non-utf8 messages", ["export", "--type", "check", "--format", "json",
                               "--config", rules, "--messages", str(bad_msgs)],
         "bad message fixture"),
        ("violations alias non-utf8 rules", ["export", "--type", "violations",
                                             "--format", "json", "--config",
                                             str(bad_rules), "--messages", msgs],
         "bad rules config"),
        ("schema-diff non-utf8 before", ["export", "--type", "schema-diff",
                                         "--format", "json", "--before",
                                         str(bad_schema), "--after", after],
         "not valid UTF-8"),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert want in err, (name, err)
        assert "Traceback" not in err, name


def test_export_report_junk_fixture_ladder_at_main_boundary(tmp_path):
    """The export parser reroutes report into cmd_report (cli.py:573-575), so
    junk fixtures must fail closed with the same clean errors as the direct
    report route (pinned at test_report_non_utf8_topics_exit_3:1433,
    test_report_non_utf8_rules_exit_3:1422, test_report_non_utf8_violations_
    exit_3:1442) — never a raw UnicodeDecodeError traceback escaping main()."""
    topics = str(FIXTURES / "topics" / "topics.json")

    bad_topics = tmp_path / "topics.json"
    bad_topics.write_bytes(b'\xff\xfe{"topics": []}')
    bad_rules = tmp_path / "rules.yaml"
    bad_rules.write_bytes(b"{topic: \xff\xfe}")
    bad_msgs = tmp_path / "msgs.json"
    bad_msgs.write_bytes(b'{"messages": \xff\xfe[]}')
    bad_vio = tmp_path / "vio.json"
    bad_vio.write_bytes(b'\xff\xfe[]')

    # export report route: every junk fixture exits 3 with its designed message
    for name, argv, want in [
        ("non-utf8 topics", ["export", "--type", "report", "--format", "json",
                             "--topics", str(bad_topics)],
         "fixture is not valid UTF-8"),
        ("non-utf8 rules and messages", ["export", "--type", "report",
                                         "--format", "json", "--topics", topics,
                                         "--rules", str(bad_rules),
                                         "--messages", str(bad_msgs)],
         "bad rules/messages"),
        ("non-utf8 violations", ["export", "--type", "report", "--format",
                                 "json", "--topics", topics,
                                 "--violations", str(bad_vio)],
         "not valid UTF-8"),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert want in err, (name, err)
        assert "Traceback" not in err, name


def test_export_directory_as_file_ladder_at_main_boundary(tmp_path):
    """The export parser reroutes check/violations into cmd_check (cli.py:570),
    report into cmd_report (cli.py:573) and schema-diff into cmd_schema_diff,
    so a directory passed in place of a file must fail closed with the same
    clean errors as the direct routes (pinned at test_report_check_directory_
    as_file_exit_3:1648: report --topics <dir>, check --config <dir>, report
    --registry <dir>, schema-diff --before <dir>) — never a raw
    IsADirectoryError traceback escaping main()."""
    adir = tmp_path / "adir"
    adir.mkdir()
    topics = str(FIXTURES / "topics" / "topics.json")
    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")
    after = str(FIXTURES / "schemas" / "v1.json")

    for name, argv, want in [
        ("report --topics <dir>", ["export", "--type", "report", "--format",
                                   "json", "--topics", str(adir)],
         "report requires --topics"),
        ("check --config <dir>", ["export", "--type", "check", "--format",
                                  "json", "--config", str(adir),
                                  "--messages", msgs],
         "check requires --config"),
        ("violations --config <dir>", ["export", "--type", "violations",
                                       "--format", "json", "--config",
                                       str(adir), "--messages", msgs],
         "check requires --config"),
        ("report --registry <dir>", ["export", "--type", "report", "--format",
                                     "json", "--topics", topics,
                                     "--registry", str(adir)],
         "--registry file missing"),
        ("schema-diff --before <dir>", ["export", "--type", "schema-diff",
                                        "--format", "json", "--before",
                                        str(adir), "--after", after],
         "not a file"),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert want in err, (name, err)
        assert "Traceback" not in err, name


def test_export_output_path_failure_ladder_at_main_boundary(tmp_path):
    """The export parser reroutes schema-diff into cmd_schema_diff (cli.py:545),
    check/violations into cmd_check (cli.py:570) and report into cmd_report
    (cli.py:573), so a bad -o target must fail closed through _write_out
    (cli.py:97-100: FileNotFoundError -> "no such directory", IsADirectoryError
    -> "path is a directory") exactly like the direct routes — pinned at
    test_output_to_missing_directory_exit_3:1673 (report -o /no/such/dir/
    out.json, check -o /tmp) — never a raw FileNotFoundError/IsADirectoryError
    traceback escaping main(). Each leg uses real inputs so only the output
    path is the failure point."""
    topics = str(FIXTURES / "topics" / "topics.json")
    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    msgs = str(FIXTURES / "messages" / "payments_messages.json")
    v1 = str(FIXTURES / "schemas" / "v1.json")
    v2 = str(FIXTURES / "schemas" / "v2.json")

    for name, argv, want in [
        ("report -o no-such-dir", ["export", "--type", "report", "--format",
                                   "json", "--topics", topics,
                                   "-o", "/no/such/dir/out.json"],
         "no such directory"),
        ("report -o dir", ["export", "--type", "report", "--format", "json",
                           "--topics", topics, "-o", "/tmp"],
         "path is a directory"),
        ("check -o no-such-dir", ["export", "--type", "check", "--format",
                                  "json", "--config", rules, "--messages",
                                  msgs, "-o", "/no/such/dir/out.json"],
         "no such directory"),
        ("check -o dir", ["export", "--type", "check", "--format", "json",
                          "--config", rules, "--messages", msgs, "-o", "/tmp"],
         "path is a directory"),
        ("violations -o no-such-dir", ["export", "--type", "violations",
                                       "--format", "json", "--config", rules,
                                       "--messages", msgs,
                                       "-o", "/no/such/dir/out.json"],
         "no such directory"),
        ("schema-diff -o no-such-dir", ["export", "--type", "schema-diff",
                                        "--format", "json", "--before", v1,
                                        "--after", v2,
                                        "-o", "/no/such/dir/out.json"],
         "no such directory"),
        ("schema-diff -o dir", ["export", "--type", "schema-diff",
                                "--format", "json", "--before", v1,
                                "--after", v2, "-o", "/tmp"],
         "path is a directory"),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert "cannot write output" in err, (name, err)
        assert want in err, (name, err)
        assert "Traceback" not in err, name


def test_invalid_json_feed_ladder_at_main_boundary(tmp_path):
    """A malformed JSON fixture must fail closed with the same clean error on
    every feed route: _load_json_or_exit maps json.JSONDecodeError to CliError
    "invalid JSON in {path}" (cli.py:139-140) for schema-diff --before/--after
    (cli.py:203) and report --violations (cli.py:509), and the export parser
    reroutes schema-diff into cmd_schema_diff (cli.py:545) and report into
    cmd_report (cli.py:573) so those legs must inherit the identical error —
    never a raw json.JSONDecodeError traceback escaping main(). The not-valid
    UTF-8 (cli.py:138) and file-not-found (cli.py:134) neighbors are pinned
    elsewhere (875/1439/1449 and 1498); this ladder pins the missing
    JSONDecodeError branch on both the direct and export routes."""
    bad = tmp_path / "bad.json"
    bad.write_text("{bad json", encoding="utf-8")
    after = str(FIXTURES / "schemas" / "v1.json")
    topics = str(FIXTURES / "topics" / "topics.json")

    for name, argv in [
        ("schema-diff --before", ["schema-diff", "--before", str(bad),
                                  "--after", after]),
        ("schema-diff --after", ["schema-diff", "--before", after,
                                 "--after", str(bad)]),
        ("report --violations", ["report", "--topics", topics,
                                 "--violations", str(bad)]),
        ("export schema-diff --before", ["export", "--type", "schema-diff",
                                         "--format", "json", "--before",
                                         str(bad), "--after", after]),
        ("export report --violations", ["export", "--type", "report",
                                        "--format", "json", "--topics",
                                        topics, "--violations", str(bad)]),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert f"invalid JSON in {bad}" in err, (name, err)
        assert "Traceback" not in err, name


def test_messages_file_missing_ladder_at_main_boundary(tmp_path):
    """A --messages path that does not exist must fail closed with the same
    clean error on every check/report route: cmd_check._validate raises
    CliError "--messages file missing: {path}" for a non-existent path
    (cli.py:262-264) and cmd_report the same (cli.py:496-498) when --rules and
    --messages are both present; the export parser reroutes check/violations
    into cmd_check (cli.py:570) and report into cmd_report (cli.py:573) so
    those legs must inherit the identical error — never a raw FileNotFoundError
    traceback escaping main(). The --rules neighbor axis is pinned at
    test_report_missing_rules_file_exit_3:1399; this ladder pins the --messages
    axis on the direct and export routes. Each leg uses a real --rules/--topics
    fixture so the missing --messages path is the only failure point."""
    topics = str(FIXTURES / "topics" / "topics.json")
    rules = str(FIXTURES / "rules" / "rules_scale.yaml")
    missing = str(tmp_path / "no_such_messages.json")

    for name, argv, want in [
        ("check --messages", ["check", "--config", rules, "--messages",
                              missing],
         f"--messages file missing: {missing}"),
        ("report --rules+--messages", ["report", "--topics", topics,
                                       "--rules", rules, "--messages", missing],
         f"--messages file missing: {missing}"),
        ("export check --messages", ["export", "--type", "check", "--format",
                                     "json", "--config", rules, "--messages",
                                     missing],
         f"--messages file missing: {missing}"),
        ("export violations --messages", ["export", "--type", "violations",
                                          "--format", "json", "--config",
                                          rules, "--messages", missing],
         f"--messages file missing: {missing}"),
        ("export report --messages", ["export", "--type", "report",
                                      "--format", "json", "--topics", topics,
                                      "--rules", rules, "--messages", missing],
         f"--messages file missing: {missing}"),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert want in err, (name, err)
        assert "Traceback" not in err, name


def test_feed_directory_as_file_ladder_at_main_boundary(tmp_path):
    """A directory passed to a JSON feed read must fail closed with the same
    clean error on every _load_json_or_exit route: IsADirectoryError maps to
    CliError "not a file: {path}" (cli.py:136) for schema-diff --after and
    report --violations, and the export parser reroutes schema-diff into
    cmd_schema_diff (cli.py:545) and report into cmd_report (cli.py:573) so
    those legs inherit the identical error — never a raw IsADirectoryError
    traceback escaping main(). The neighbor --before legs are pinned at
    test_report_check_directory_as_file_exit_3:1668 and
    test_export_directory_as_file_ladder_at_main_boundary:2500; this ladder
    pins the --after and --violations legs on the direct and export routes.
    report --topics <dir> and check --config <dir> fail earlier in their own
    argument validation (report requires --topics / check requires --config)
    and are pinned at 1655/1660 and 2485/2488. Each leg uses a real companion
    fixture so the directory is the only failure point."""
    adir = tmp_path / "adir"
    adir.mkdir()
    topics = str(FIXTURES / "topics" / "topics.json")
    after = str(FIXTURES / "schemas" / "v1.json")

    for name, argv in [
        ("schema-diff --after", ["schema-diff", "--before", after,
                                 "--after", str(adir)]),
        ("export schema-diff --after", ["export", "--type", "schema-diff",
                                        "--format", "json", "--before",
                                        after, "--after", str(adir)]),
        ("report --violations", ["report", "--topics", topics,
                                 "--violations", str(adir)]),
        ("export report --violations", ["export", "--type", "report",
                                        "--format", "json", "--topics",
                                        topics, "--violations", str(adir)]),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert f"not a file: {adir}" in err, (name, err)
        assert "Traceback" not in err, name


def test_feed_file_not_found_ladder_at_main_boundary(tmp_path):
    """A missing JSON feed path must fail closed with the same clean error on
    every _load_json_or_exit route: FileNotFoundError maps to CliError "file
    not found: {path}" (cli.py:134) for schema-diff --before/--after and
    report --violations, and the export parser reroutes schema-diff into
    cmd_schema_diff (cli.py:545) and report into cmd_report (cli.py:573) so
    those legs inherit the identical error — never a raw FileNotFoundError
    traceback escaping main(). The direct report --violations leg is pinned at
    test_report_violations_junk_shape_tolerated:1498 ("42" non-path value
    reaching the same branch); the schema-diff direct and export legs and the
    export report leg have no pin. Each leg uses a real companion fixture so
    the missing feed path is the only failure point."""
    missing = str(tmp_path / "no_such_feed.json")
    after = str(FIXTURES / "schemas" / "v1.json")
    topics = str(FIXTURES / "topics" / "topics.json")

    for name, argv in [
        ("schema-diff --before", ["schema-diff", "--before", missing,
                                  "--after", after]),
        ("schema-diff --after", ["schema-diff", "--before", after,
                                 "--after", missing]),
        ("export schema-diff --before", ["export", "--type", "schema-diff",
                                         "--format", "json", "--before",
                                         missing, "--after", after]),
        ("export schema-diff --after", ["export", "--type", "schema-diff",
                                        "--format", "json", "--before",
                                        after, "--after", missing]),
        ("export report --violations", ["export", "--type", "report",
                                        "--format", "json", "--topics",
                                        topics, "--violations", missing]),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert f"file not found: {missing}" in err, (name, err)
        assert "Traceback" not in err, name


def test_schema_parse_error_ladder_at_main_boundary(tmp_path):
    """A diff across schema formats must fail closed with the same clean error
    on every schema-diff route: diff_schemas raises SchemaParseError on
    cross-format input (schema_diff.py:744) and cmd_schema_diff wraps it as
    CliError "schema parse error: {exc}" (cli.py:229-230), so direct and export
    legs (export parser reroutes schema-diff into cmd_schema_diff, cli.py:545)
    both exit 3 with out=="" and no Traceback. The invalid-JSON axis is pinned
    at test_invalid_json_feed_ladder_at_main_boundary:2566 but the parse-formats
    axis (valid JSON, unparseable-as-single-format pairing) has no pin. Each
    leg pairs a JSON Schema file (has "$schema") against a real Avro record so
    the cross-format trigger is the only failure point."""
    js_schema = tmp_path / "jschema.json"
    js_schema.write_text(json.dumps({
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }), encoding="utf-8")
    avro = str(FIXTURES / "schemas" / "v1.json")

    for name, argv in [
        ("schema-diff --before json --after avro",
         ["schema-diff", "--before", str(js_schema), "--after", avro]),
        ("schema-diff --before avro --after json",
         ["schema-diff", "--before", avro, "--after", str(js_schema)]),
        ("export schema-diff --before json --after avro",
         ["export", "--type", "schema-diff", "--format", "json",
          "--before", str(js_schema), "--after", avro]),
        ("export schema-diff --before avro --after json",
         ["export", "--type", "schema-diff", "--format", "json",
          "--before", avro, "--after", str(js_schema)]),
    ]:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert "schema parse error: cannot diff across formats" in err, (name, err)
        assert "Traceback" not in err, name


def test_topics_failure_ladder_at_main_boundary(monkeypatch) -> None:
    """The topics subcommand's two failure surfaces have no main()-boundary
    pin: cmd_topics rejects an empty-string --bootstrap at its gate
    (cli.py:403-404, "topics requires --bootstrap <host:port>") — argparse
    requires the flag (cli.py:636) so only the empty string reaches it — and
    the -o failure legs reuse the shared _write_out branch contract
    (cli.py:97-100) reached only after list_topics succeeds (cli.py:417).

    test_cmd_topics_output_write_fails_closed pins the -o failures on the
    direct handler and test_topics_dispatch_exit_codes / _timeout pin the
    dispatch and wire-call axes; this ladder pins the empty-bootstrap gate and
    both -o failure legs through main(). Each leg must exit 3 with out=="",
    the clean message on stderr, and no Traceback."""
    from kgov.broker import _require_timeout

    def fake_list_topics(bootstrap, timeout=10.0):
        _require_timeout(timeout)
        return ["payments"]

    monkeypatch.setattr("kgov.broker.list_topics", fake_list_topics)

    legs = [
        ("topics --bootstrap ''",
         ["topics", "--bootstrap", ""],
         "topics requires --bootstrap <host:port>"),
        ("topics -o /no/such/dir/out.json",
         ["topics", "--bootstrap", "127.0.0.1:9092", "-o", "/no/such/dir/out.json"],
         "no such directory"),
        ("topics -o /tmp (directory target)",
         ["topics", "--bootstrap", "127.0.0.1:9092", "-o", "/tmp"],
         "path is a directory"),
    ]
    for name, argv, expected in legs:
        code, out, err = _run(*argv)
        assert code == 3, name
        assert out == "", name
        assert "cannot write output" in err or expected.startswith("topics"), (name, err)
        assert expected in err, (name, err)
        assert "Traceback" not in err, name


def test_export_unknown_type_fails_closed():
    """export --type accepts only schema-diff|check|violations|report
    (cli.py:656). An unknown type must fail closed on both boundaries: at the
    main() parser it is an argparse choices violation -> usage error exit 2
    with no Traceback, no matter what trailing args look valid; through a
    library Namespace (cmd_export) it raises CliError "unknown export type:
    <value>" (cli.py:572) for str junk, a missing type, and non-str values.
    test_direct_subcommand_format_axes_pinned pins the direct-subcommand
    --format axis but never the export --type axis."""
    import argparse

    from kgov.cli import CliError, cmd_export
    from kgov.cli import main as cli_main

    def go(argv):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_main(list(argv))
        except SystemExit as exc:  # pragma: no cover - safety net
            code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    # main() boundary: unknown --type is rejected by the parser regardless of
    # otherwise-valid trailing flags (--format csv, -o path, real inputs).
    for junk in ("bogus", "report22", "xml", ""):
        code, out, err = go(["export", "--type", junk, "--format", "csv",
                             "-o", "x.csv"])
        assert code == 2, junk
        assert out == ""
        assert "invalid choice" in err or "usage:" in err
        assert "Traceback" not in err

    # Valid choices must still route past the parser into their handlers: with
    # --format csv and no real data the four real types each fail closed in the
    # handler (schema-diff/check/violations reject csv, report requires
    # --topics) as exit 3 — never the parser exit-2 usage error reserved for
    # unknown types.
    for real in ("schema-diff", "check", "violations", "report"):
        code, out, err = go(["export", "--type", real, "--format", "csv"])
        assert code == 3, real
        assert "Traceback" not in err

    # library Namespace boundary: cmd_export fails closed on junk/missing/
    # non-str types instead of misrouting or raising AttributeError.
    def ns(**kw):
        return argparse.Namespace(**kw)

    with pytest.raises(CliError, match="unknown export type: bogus"):
        cmd_export(ns(type="bogus", format="json"))
    with pytest.raises(CliError, match="unknown export type: None"):
        cmd_export(ns(format="json"))
    with pytest.raises(CliError, match="unknown export type: 123"):
        cmd_export(ns(type=123, format="json"))
    with pytest.raises(CliError, match="unknown export type: .*"):
        cmd_export(ns(type=["schema-diff"], format="json"))


# ---------------------------------------------------------------------------
# registry (live mock server through the CLI)
# ---------------------------------------------------------------------------


def test_registry_url_gate_at_main_boundary(server, monkeypatch) -> None:
    """README:105-106 documents ``--registry-url`` as the Schema Registry base
    URL. test_registry_cli_full pins the live-fetch path; this test pins the
    URL GATE (cli.py:428) through the parser: every junk string must be
    rejected BEFORE the lazy SchemaRegistryClient import (cli.py:431) and any
    wire call, with a clean CliError -> exit 3, empty stdout, no urllib-internal
    leak. The gate previously read ``startswith("http")`` (loose), so
    ``http:``/``https:x``/``http:/x``/``HTTPS://x``/``" http://x"`` slipped
    through to SchemaRegistryClient and leaked ``registry request failed:
    cannot reach http:: no host given`` to stderr — the same fail-open the
    check --schema-registry gate (cli.py:275) had before it was tightened.
    A recorder installed in place of SchemaRegistryClient proves zero
    constructor reach for every junk leg; the live-server fence proves a
    well-formed URL still fetches (exit 0)."""
    from kgov.registry import SchemaRegistryClient

    calls: list = []

    class _WireBomb:
        def __init__(self, *a, **kw):
            calls.append(a)
            raise AssertionError("wire reached")

    # fence: a well-formed --registry-url passes the gate and fetches (exit 0)
    code, out, _ = _run("registry", "--registry-url", server.base_url)
    assert code == 0
    assert set(json.loads(out)["subjects"]) == {"orders-value", "payments-value"}

    monkeypatch.setattr("kgov.registry.SchemaRegistryClient", _WireBomb)

    # every junk string dies at the gate: exit 3, clean error, zero client reach
    for junk in ("", "   ", "\t\n", "not-a-url", "abc", "localhost:8081",
                 "ftp://x", "http:", "https:x", "http:/x", "://x",
                 "HTTPS://x", " http://x"):
        calls.clear()
        code, out, err = _run("registry", "--registry-url", junk)
        assert code == 3, junk
        assert out == ""
        assert "registry requires --registry-url <http://host:port>" in err, junk
        assert "Traceback" not in err, junk
        assert calls == [], f"junk {junk!r} reached SchemaRegistryClient"


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


def test_registry_cli_output_file_success_writes_full_dump(server, tmp_path, monkeypatch) -> None:
    """README:105: ``kgov registry --registry-url http://localhost:8081
    [-o registry.json]`` documents the -o flag writing the registry output to
    a file. The fail-closed legs are pinned by
    test_registry_cli_output_write_fails_closed; this test pins the success
    path: exit 0 with the same rendered full-dump JSON (top-level
    ``{"subjects", "generated_at"}``) in the file as the stdout route emits."""
    _freeze_clock(monkeypatch)
    # stdout leg: the full-dump payload with a trailing newline and empty
    # stderr
    code, out, err = _run("registry", "--registry-url", server.base_url)
    assert code == 0
    assert err == ""
    data = json.loads(out)
    assert set(data) == {"subjects", "generated_at"}
    assert set(data["subjects"]) == {"orders-value", "payments-value"}
    assert out.endswith("\n")

    # -o leg: stdout stays empty, the file is the identical render minus the
    # trailing newline the stdout print adds
    out_path = tmp_path / "registry.json"
    code, out_file, err_file = _run("registry", "--registry-url", server.base_url,
                                    "-o", str(out_path))
    assert code == 0
    assert out_file == ""
    assert err_file == ""
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data
    assert file_text == out.rstrip("\n")


def test_registry_cli_output_file_success_writes_subject(server, tmp_path) -> None:
    """The ``-o`` success path must also honor ``--subject``: the file then
    carries that subject's per-version shape instead of the full dump."""
    # stdout leg: the per-version subject payload with a trailing newline and
    # empty stderr
    code, out, err = _run("registry", "--registry-url", server.base_url,
                          "--subject", "payments-value")
    assert code == 0
    assert err == ""
    data = json.loads(out)
    assert set(data) == {"subject", "versions"}
    assert data["subject"] == "payments-value"
    assert out.endswith("\n")

    # -o leg: stdout stays empty, the file is the identical render minus the
    # trailing newline the stdout print adds
    out_path = tmp_path / "subject.json"
    code, out_file, err_file = _run("registry", "--registry-url",
                                    server.base_url,
                                    "--subject", "payments-value",
                                    "-o", str(out_path))
    assert code == 0
    assert out_file == ""
    assert err_file == ""
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data
    assert file_text == out.rstrip("\n")
    assert [v["version"] for v in data["versions"]] == [1, 2]


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


def test_topics_cli_output_file_success_writes_payload(monkeypatch, tmp_path) -> None:
    """README:115: ``kgov topics --bootstrap localhost:9092 [-o topics.json]``
    documents the -o flag writing topics output to a file. The fail-closed legs
    and the valid-path write are pinned on the direct handler by
    test_cmd_topics_output_write_fails_closed, and dispatch (without -o) by
    test_topics_dispatch_exit_codes; nothing pins the documented -o success
    route at the main() boundary. This test does: exit 0 with the same rendered
    payload (top-level ``{"topics", "generated_at"}``) in the file as the
    stdout route emits."""
    _freeze_clock(monkeypatch)

    def rich_list_topics(bootstrap, timeout=10.0):
        return ["payments", "orders"]

    monkeypatch.setattr("kgov.broker.list_topics", rich_list_topics)

    # stdout leg: the JSON payload with a trailing newline and empty stderr
    code, out, err = _run("topics", "--bootstrap", "127.0.0.1:9092")
    assert code == 0
    assert err == ""
    data = json.loads(out)
    assert set(data) == {"topics", "generated_at"}
    assert data["topics"] == ["payments", "orders"]
    assert out.endswith("\n")

    # -o leg: stdout stays empty, the file is the identical render minus the
    # trailing newline the stdout print adds
    out_path = tmp_path / "topics.json"
    code, out_file, err_file = _run("topics", "--bootstrap", "127.0.0.1:9092",
                                    "-o", str(out_path))
    assert code == 0
    assert out_file == ""
    assert err_file == ""
    file_text = out_path.read_text(encoding="utf-8")
    assert json.loads(file_text) == data
    assert file_text == out.rstrip("\n")


def test_topics_timeout_axis_at_main_boundary(monkeypatch) -> None:
    """README:115: ``kgov topics --bootstrap localhost:9092 [-o topics.json]``
    documents the topics command, whose --timeout flag the parser declares as
    type=float (cli.py:633). The registry route pins its own --timeout ladder
    (test_registry_cli_junk_timeout_exit_3: non-float -> exit 2 usage error,
    float-parseable junk -> clean exit 3); the topics --timeout axis was only
    ever pinned on the direct handler (test_cmd_topics_tolerates_junk_bootstrap
    and _options, ~2748), never crossing the main() boundary. This pin does:
    a valid float exits 0 and is wired through to the wire call unchanged, a
    falsy 0 is coerced to the documented default (never a false exit-3 failure),
    float-parseable junk (-1/nan/inf) fails closed via broker._require_timeout
    with a clean exit-3 CliError, and a non-float string is an argparse usage
    error (exit 2, empty stdout)."""
    import kgov.broker
    from kgov.broker import _require_timeout

    calls: dict = {}

    def fake_list_topics(bootstrap, timeout=10.0):
        # Mirror real list_topics: reject junk timeouts first, record the value
        # that reached the wire, and return a stable topic list.
        _require_timeout(timeout)
        calls["timeout"] = timeout
        return ["payments"]

    monkeypatch.setattr("kgov.broker.list_topics", fake_list_topics)

    # valid float: exit 0, the value reaches the wire unchanged
    code, out, err = _run("topics", "--bootstrap", "127.0.0.1:9092",
                          "--timeout", "7.5")
    assert code == 0
    assert calls["timeout"] == 7.5
    assert json.loads(out)["topics"] == ["payments"]

    # falsy 0 coerces to the default 10.0 (cmd_topics passes `or 10.0`), so a
    # zero timeout never turns into a false exit-3 failure
    calls.clear()
    code, out, err = _run("topics", "--bootstrap", "127.0.0.1:9092",
                          "--timeout", "0")
    assert code == 0
    assert calls["timeout"] == 10.0

    # float-parseable junk: _require_timeout rejects -> clean exit 3, never a
    # hang or a raw exception traceback
    for junk in ("-1", "nan", "inf"):
        code, out, err = _run("topics", "--bootstrap", "127.0.0.1:9092",
                              "--timeout", junk)
        assert code == 3, junk
        assert "failed to list topics from" in err, junk
        assert "timeout must be a positive number" in err, junk
        assert "Traceback" not in err, junk

    # non-float string: parser type=float usage error -> exit 2, empty stdout
    for junk in ("abc", "3.5.6"):
        code, out, err = _run("topics", "--bootstrap", "127.0.0.1:9092",
                              "--timeout", junk)
        assert code == 2, junk
        assert out == ""
        assert "error:" in err
        assert "Traceback" not in err


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

