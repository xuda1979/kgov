"""kgov CLI — the governance assistant's command-line surface.

Commands
--------

    kgov schema-diff --before sc1.json --after sc2.json [--format avro|json]
        [--subject NAME] [-o out.json|out.md]

    kgov check --config rules.yaml --messages messages.json [--gate N|--max-violations N]
        Exit code 0 = no violations (or within gate), 1 = violations found
        (or gate exceeded), 2 = usage/config error, 3 = runtime error.

    kgov topics --bootstrap BROKER [-o topics.json]
        Pull topic metadata from a real broker (requires kgov[broker]).

    kgov registry --registry-url URL [-o registry.json]
        Pull subject/schema metadata from a (mock or live) Schema Registry.

    kgov report --topics topics.json [--registry registry.json]
        [--groups consumer_groups.json] [--rules rules.yaml]
        [--messages payments_messages.json] [--format csv|json] [-o out]

    kgov export --type schema-diff|report|violations ...
        Thin alias of the above with an explicit --format.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - PyYAML is a hard dependency
    yaml = None  # type: ignore

from . import __version__
from .model import CompatibilityKind, now_utc_iso
from .report import (
    GovernanceReport,
    build_governance_report,
    report_to_csv,
)
from .rules import (
    load_rules_file,
    validate_fixture_file,
)
from .rules_errors import RulesConfigError
from .schema_diff import SchemaParseError, diff_schemas

EXIT_OK = 0
EXIT_FAIL = 1        # gate crossed
EXIT_USAGE = 2
EXIT_RUNTIME = 3


class CliError(Exception):
    pass


def _write_out(path: Optional[str], text: str, force: bool = False) -> None:
    if path:
        try:
            Path(path).write_text(text, encoding="utf-8")
        except FileNotFoundError as exc:
            raise CliError(f"cannot write output: no such directory: {path}") from exc
        except IsADirectoryError as exc:
            raise CliError(f"cannot write output: path is a directory: {path}") from exc
        except OSError as exc:
            raise CliError(f"cannot write output {path!r}: {exc}") from exc
    elif force:
        raise CliError(f"no output path given (required for this export): {path!r}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")


def _load_json_or_exit(path: str):
    try:
        data = Path(path).read_text(encoding="utf-8")
        return json.loads(data)
    except FileNotFoundError as exc:
        raise CliError(f"file not found: {path}") from exc
    except IsADirectoryError as exc:
        raise CliError(f"not a file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid JSON in {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# schema-diff
# ---------------------------------------------------------------------------


def _load_schema_text(ref: str, fmt: Optional[str]):
    """Fetch a schema by path, URL, registry-version ref, or raw JSON.

    Return (schema_text, format). Live registry references use a local HTTP
    round-trip: we can't hand a live URL to ``diff_schemas``, so we fetch the
    schema payload and return the *text* (plus a format hint — a version ref
    product is always Avro, so a bare ``--format`` can stay authoritative).
    """
    if ref.startswith(("http://", "https://")):
        import urllib.request

        try:
            with urllib.request.urlopen(ref, timeout=10) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise CliError(f"cannot fetch schema from {ref}: {exc}") from exc
        if isinstance(data, str):
            body = data
        elif "schema" in data:
            # Confluent wire for /subjects/{subject}/versions/{version}
            body = data["schema"]
        else:
            body = json.dumps(data, ensure_ascii=False)
        return body, fmt

    if ref.count(":") == 1 and not Path(ref).exists():
        # Shorthand ``[http://]registry-url/subject/version`` in a single arg.
        try:
            url, subject, version = ref.split("/", 2)
            base = url
            if not url.startswith("http"):
                base = f"http://{url}"
            from .registry import SchemaRegistryClient

            client = SchemaRegistryClient(base)
            body_data = client.get_version(subject, version)
            if "schema" in body_data:
                body = body_data["schema"]
            else:
                body = json.dumps(body_data, ensure_ascii=False)
        except Exception as exc:
            raise CliError(f"cannot fetch schema from {ref!r}: {exc}") from exc
        return body, "avro" if fmt in (None, "avro") else fmt

    data = _load_json_or_exit(ref)
    return json.dumps(data), fmt


def cmd_schema_diff(args) -> int:
    before_p, after_p = args.before, args.after
    if not before_p or not after_p:
        raise CliError("schema-diff requires --before and --after")
    fmt = args.format
    try:
        before_text, _ = _load_schema_text(before_p, fmt)
        after_text, _ = _load_schema_text(after_p, fmt)
    except CliError:
        raise
    try:
        rep = diff_schemas(
            before_text,
            after_text,
            subject=args.subject or "record",
            before_version=args.before_version,
            after_version=args.after_version,
            before_id=args.before_id,
            after_id=args.after_id,
            format=fmt,
        )
    except SchemaParseError as exc:
        raise CliError(f"schema parse error: {exc}") from exc

    out_text = ""
    ext = (args.output or "").lower()
    want_json = bool(args.json) or ext.endswith(".json")
    want_md = bool(args.markdown) or ext.endswith(".md")
    if want_json:
        out_text = json.dumps(rep.to_dict(), indent=2, ensure_ascii=False)
    else:
        out_text = rep.to_markdown()

    _write_out(args.output, out_text)
    if rep.kind == CompatibilityKind.BREAKING:
        print(f"SCHEMA DIFF: {rep.kind} (incompatible)", file=sys.stderr)
        return EXIT_FAIL
    print(f"SCHEMA DIFF: {rep.kind}", file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _validate(rs, messages_path: Optional[str], broker_url: Optional[str], topic: Optional[str],
              registry_url: Optional[str] = None):
    """Produce a violation list from messages/broker, or None if no source."""
    if messages_path:
        if not Path(messages_path).is_file():
            raise CliError(f"--messages file missing: {messages_path}")
        return validate_fixture_file(rs, messages_path)
    if broker_url or topic:
        # Imported lazily: requires kgov[broker].
        from .broker import AvroRecordDecoder, read_topic  # type: ignore
        from .rules import Message, validate_message

        if not topic:
            raise CliError("--broker requires --topic")
        decoder = None
        if registry_url:
            from .registry import SchemaRegistryClient

            decoder = AvroRecordDecoder(SchemaRegistryClient(registry_url))
        try:
            msgs = read_topic(broker_url, topic, value_decoder=decoder)
        except Exception as exc:
            # Unreachable broker, transport failure, missing confluent-kafka:
            # surface as a clean runtime error instead of a raw traceback and
            # never let an empty read pass the gate silently.
            raise CliError(f"broker scan failed: {exc}") from exc
        return [
            v
            for m in msgs
            for v in validate_message(
                rs,
                Message(
                    topic=m.topic,
                    partition=m.partition,
                    offset=m.offset,
                    key=m.key,
                    value=m.value,
                ),
            )
        ]
    return None


def cmd_check(args) -> int:
    if not args.config or not Path(args.config).is_file():
        raise CliError("check requires --config <rules.yaml> (file must exist)")
    try:
        rs = load_rules_file(args.config)
    except RulesConfigError as exc:
        raise CliError(f"bad rules config: {exc}") from exc

    # A CI gate must never silently pass because no messages were supplied.
    if args.topic and not args.broker:
        raise CliError("--topic requires --broker <bootstrap.servers>")
    if not args.messages and not (args.broker and args.topic):
        raise CliError(
            "check needs a data source: pass --messages <file> or "
            "--broker <bootstrap> --topic <name>"
        )

    try:
        violation_list = _validate(rs, args.messages, args.broker, args.topic,
                                   getattr(args, "schema_registry", None))
    except RulesConfigError as exc:
        raise CliError(f"bad message fixture: {exc}") from exc
    except ImportError as exc:
        raise CliError(str(exc)) from exc

    violates = len(violation_list) > 0
    max_v = args.max_violations if args.max_violations is not None else 0
    gate_failed = violates and (max_v <= 0 or len(violation_list) > max_v)

    results = {
        "ok": not violates,
        "gate": max_v,
        "violation_count": len(violation_list),
        "rule": rs.name,
        "topic": rs.topic,
        "violations": [v.to_dict() for v in violation_list],
        "generated_at": now_utc_iso(),
    }

    out_text = json.dumps(results, indent=2, ensure_ascii=False)
    if args.output:
        _write_out(args.output, out_text, force=True)
        print(
            f"ok={results['ok']} violations={len(violation_list)} "
            f"gate={max_v} -> {'PASS' if not gate_failed else 'FAIL'}",
            file=sys.stderr,
        )
    elif not args.quiet:
        print(out_text)

    return EXIT_OK if not gate_failed else EXIT_FAIL


# ---------------------------------------------------------------------------
# topics (broker metadata)
# ---------------------------------------------------------------------------


def cmd_topics(args) -> int:
    if not args.bootstrap:
        raise CliError("topics requires --bootstrap <host:port>")
    try:
        from .broker import list_topics
    except ImportError:
        raise CliError("broker features require the 'broker' extra (confluent-kafka)")

    try:
        topics = list_topics(args.bootstrap, timeout=args.timeout or 10.0)
    except Exception as exc:
        raise CliError(f"failed to list topics from {args.bootstrap}: {exc}") from exc

    payload = {"topics": topics, "generated_at": now_utc_iso()}
    out_text = json.dumps(payload, indent=2, ensure_ascii=False)
    _write_out(args.output, out_text, force=bool(args.output))
    return EXIT_OK


# ---------------------------------------------------------------------------
# registry (Schema Registry client)
# ---------------------------------------------------------------------------


def cmd_registry(args) -> int:
    if not args.registry_url or not args.registry_url.startswith("http"):
        raise CliError("registry requires --registry-url <http://host:port>")

    try:
        from .registry import SchemaRegistryClient
    except ImportError:
        raise CliError("registry module unavailable")

    client = SchemaRegistryClient(args.registry_url, timeout=args.timeout or 10.0)
    if args.subject:
        # Single subject: emit subject details (id/version/schema/../compat).
        try:
            versions = client.get_versions(args.subject)
            payload = {"subject": args.subject, "versions": []}
            for v in versions:
                info = client.get_version(args.subject, v)
                payload["versions"].append(
                    {
                        "id": info["id"],
                        "version": info["version"],
                        "compatibility": info["compatibility"],
                        "schema": info["schema"],
                    }
                )
        except ImportError:
            raise
        except Exception as exc:
            raise CliError(f"registry request failed: {exc}") from exc
    else:
        try:
            data = client.to_fixture_dict()
            payload = {"subjects": data["subjects"], "generated_at": now_utc_iso()}
        except Exception as exc:
            raise CliError(f"registry request failed: {exc}") from exc

    out_text = json.dumps(payload, indent=2, ensure_ascii=False)
    _write_out(args.output, out_text, force=bool(args.output))
    return EXIT_OK


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def cmd_report(args) -> int:
    if not args.topics or not Path(args.topics).is_file():
        raise CliError("report requires --topics <topics.json>")
    registry = args.registry
    groups = args.groups
    for label, path in (("--registry", registry), ("--groups", groups)):
        if path and not Path(path).is_file():
            raise CliError(f"{label} file missing: {path}")
    violations: list = []

    if args.rules and args.messages:
        if not Path(args.messages).is_file():
            raise CliError(f"--messages file missing: {args.messages}")
        if not Path(args.rules).is_file():
            raise CliError(f"--rules file missing: {args.rules}")
        try:
            rs = load_rules_file(args.rules)
            violations = validate_fixture_file(rs, args.messages)
        except RulesConfigError as exc:
            raise CliError(f"bad rules/messages: {exc}") from exc
    elif args.violations:
        vio = _load_json_or_exit(args.violations)
        if isinstance(vio, list):
            violations = vio
        elif isinstance(vio, dict) and isinstance(vio.get("violations"), list):
            violations = vio["violations"]

    try:
        rep = build_governance_report(
            topics_fixture=args.topics,
            registry_fixture=registry,
            groups_fixture=groups,
            rule_violations=violations,
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc

    fmt = args.format or "json"
    if args.output and str(args.output).endswith(".csv"):
        fmt = "csv"
    if fmt == "csv":
        text = report_to_csv(rep)
        _write_out(args.output, text, force=bool(args.output))
    else:
        text = rep.to_json()
        _write_out(args.output, text, force=bool(args.output))
    return EXIT_OK


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def cmd_export(args) -> int:
    if args.type == "schema-diff":
        # The export parser's --format describes *output* format; schema-diff's
        # --format describes the *schema* language. Translate to JSON/MD flags.
        if getattr(args, "format", "json") in ("markdown", "md"):
            args.markdown = True
            args.json = False
        elif getattr(args, "format", "json") == "json":
            args.json = True
            args.markdown = False
        for attr, default in (
            ("before_version", 1), ("after_version", 2),
            ("before_id", None), ("after_id", None),
        ):
            if not hasattr(args, attr):
                setattr(args, attr, default)
        args.format = getattr(args, "schema_format", None)
        return cmd_schema_diff(args)
    if args.type in ("check", "violations"):
        return cmd_check(args)
    if args.type == "report":
        return cmd_report(args)
    raise CliError(f"unknown export type: {args.type}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kgov",
        description="Kafka topic governance assistant (schema diff + dirty-data rules).",
    )
    parser.add_argument("--version", action="version", version=f"kgov {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sd = sub.add_parser("schema-diff", help="diff two schemas (Avro / JSON Schema)")
    p_sd.add_argument("--before", required=True)
    p_sd.add_argument("--after", required=True)
    p_sd.add_argument("--format", choices=["avro", "json"], default=None)
    p_sd.add_argument("--subject", default="record")
    p_sd.add_argument("--before-version", type=int, default=1)
    p_sd.add_argument("--after-version", type=int, default=2)
    p_sd.add_argument("--before-id", type=int, default=None)
    p_sd.add_argument("--after-id", type=int, default=None)
    p_sd.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    p_sd.add_argument("--markdown", action="store_true")
    p_sd.add_argument("-o", "--output", default=None)
    p_sd.set_defaults(func=cmd_schema_diff)

    p_chk = sub.add_parser("check", help="validate messages against rules (CI gate)")
    p_chk.add_argument("--config", required=True, help="rules YAML/JSON config")
    p_chk.add_argument("--messages", default=None, help="JSON message fixture")
    p_chk.add_argument("--broker", default=None, help="bootstrap servers for live reading")
    p_chk.add_argument("--topic", default=None, help="topic to read (requires --broker)")
    p_chk.add_argument("--schema-registry", default=None,
                       help="Schema Registry URL for decoding Avro wire values (requires --broker)")
    p_chk.add_argument("--max-violations", type=int, default=None,
                       help="fail if violations exceed N (0 = fail on any)")
    p_chk.add_argument("--format", choices=["json"], default="json")
    p_chk.add_argument("-q", "--quiet", action="store_true")
    p_chk.add_argument("-o", "--output", default=None)
    p_chk.set_defaults(func=cmd_check)

    p_top = sub.add_parser("topics", help="pull topic metadata from a broker")
    p_top.add_argument("--bootstrap", required=True, help="broker bootstrap servers")
    p_top.add_argument("--timeout", type=float, default=10.0)
    p_top.add_argument("-o", "--output", default=None)
    p_top.set_defaults(func=cmd_topics)

    p_reg = sub.add_parser("registry", help="pull subject/schema metadata from a Schema Registry")
    p_reg.add_argument("--registry-url", required=True, help="Schema Registry base URL")
    p_reg.add_argument("--subject", default=None, help="restrict to one subject")
    p_reg.add_argument("--timeout", type=float, default=10.0)
    p_reg.add_argument("-o", "--output", default=None)
    p_reg.set_defaults(func=cmd_registry)

    p_rep = sub.add_parser("report", help="build governance report from fixtures")
    p_rep.add_argument("--topics", required=True, help="topics.json fixture")
    p_rep.add_argument("--registry", default=None)
    p_rep.add_argument("--groups", default=None)
    p_rep.add_argument("--rules", default=None)
    p_rep.add_argument("--messages", default=None)
    p_rep.add_argument("--violations", default=None)
    p_rep.add_argument("--format", choices=["json", "csv"], default="json")
    p_rep.add_argument("-o", "--output", default=None)
    p_rep.set_defaults(func=cmd_report)

    p_exp = sub.add_parser("export", help="export data (alias with JSON/CSV/MD output)")
    p_exp.add_argument("--type", choices=["schema-diff", "check", "violations", "report"],
                       required=True)
    p_exp.add_argument("--format", choices=["json", "csv", "markdown", "md"], default="json")
    p_exp.add_argument("--before", default=None)
    p_exp.add_argument("--after", default=None)
    p_exp.add_argument("--subject", default="record")
    p_exp.add_argument("--config", default=None)
    p_exp.add_argument("--messages", default=None)
    p_exp.add_argument("--broker", default=None)
    p_exp.add_argument("--topic", default=None)
    p_exp.add_argument("--schema-registry", default=None)
    p_exp.add_argument("--topics", default=None)
    p_exp.add_argument("--registry", default=None)
    p_exp.add_argument("--groups", default=None)
    p_exp.add_argument("--rules", default=None)
    p_exp.add_argument("--violations", default=None)
    p_exp.add_argument("--max-violations", type=int, default=None)
    p_exp.add_argument("-q", "--quiet", action="store_true")
    p_exp.add_argument("-o", "--output", default=None)
    p_exp.set_defaults(func=cmd_export)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on usage errors
        return int(exc.code) if exc.code is not None else EXIT_OK

    try:
        return args.func(args)
    except CliError as exc:
        print(f"kgov: error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
