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
import os
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
    if path and not isinstance(path, (str, os.PathLike)):
        # pathlib.Path() rejects non-str/PathLike values (bytes, int, list,
        # dict, ...) with a raw TypeError; treat a junk output value as absent.
        path = None
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
        # junk text (None, ints, bytes, containers, ...) from a direct library
        # caller would raw-crash on endswith(); render it via str() instead of
        # enforcing newline normalization
        try:
            needs_newline = not text.endswith("\n")
        except (AttributeError, TypeError):
            # junk text (None, ints, bytes, containers, ...) from a direct
            # library caller would raw-crash on endswith(); render it via
            # str() instead of enforcing newline normalization. bytes needs a
            # bytes arg (TypeError, not AttributeError), so catch both.
            print(str(text))
            return
        print(text, end="" if not needs_newline else "\n")


def _load_json_or_exit(path: str):
    if not isinstance(path, (str, os.PathLike)):
        raise CliError(f"cannot read {path!r}: not a file path")
    try:
        data = Path(path).read_text(encoding="utf-8")
        return json.loads(data)
    except FileNotFoundError as exc:
        raise CliError(f"file not found: {path}") from exc
    except IsADirectoryError as exc:
        raise CliError(f"not a file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise CliError(f"not valid UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid JSON in {path}: {exc}") from exc
    except (OSError, ValueError) as exc:
        # unreadable files (permission denied, ...) and malformed paths
        # (embedded NUL bytes) leak out of read_text; surface them as clean
        # runtime errors instead of raw tracebacks
        raise CliError(f"cannot read {path!r}: {exc}") from exc


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
    if not isinstance(ref, str):
        raise CliError(f"cannot load schema from {ref!r}: not a path/URL/JSON ref")
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
    before_p = getattr(args, "before", None)
    after_p = getattr(args, "after", None)
    if not before_p or not after_p:
        raise CliError("schema-diff requires --before and --after")
    fmt = getattr(args, "format", None)
    try:
        before_text, _ = _load_schema_text(before_p, fmt)
        after_text, _ = _load_schema_text(after_p, fmt)
    except CliError:
        raise
    try:
        rep = diff_schemas(
            before_text,
            after_text,
            subject=getattr(args, "subject", None) or "record",
            before_version=getattr(args, "before_version", 1),
            after_version=getattr(args, "after_version", 2),
            before_id=getattr(args, "before_id", None),
            after_id=getattr(args, "after_id", None),
            format=fmt,
        )
    except SchemaParseError as exc:
        raise CliError(f"schema parse error: {exc}") from exc

    out_text = ""
    ext = ""
    output = getattr(args, "output", None)
    if isinstance(output, str):
        ext = output.lower()
    want_json = bool(getattr(args, "json", False)) or ext.endswith(".json")
    want_md = bool(getattr(args, "markdown", False)) or ext.endswith(".md")
    if want_json:
        out_text = json.dumps(rep.to_dict(), indent=2, ensure_ascii=False)
    else:
        out_text = rep.to_markdown()

    _write_out(output, out_text)
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
        if not isinstance(messages_path, (str, os.PathLike)):
            raise CliError(f"--messages file missing: {messages_path!r}")
        if not Path(messages_path).is_file():
            raise CliError(f"--messages file missing: {messages_path}")
        return validate_fixture_file(rs, messages_path)
    if broker_url or topic:
        # Truthy junk broker/topic values (lists, dicts, ints, bytes) must be
        # rejected here, before the lazy import and any live client: a junk
        # bootstrap passed into read_topic would hit a real AdminClient /
        # Consumer (hang class fixed in cmd_topics).
        if not isinstance(broker_url, str) or not broker_url:
            raise CliError(f"--broker must be <host:port>, got {broker_url!r}")
        if not isinstance(topic, str) or not topic:
            raise CliError(f"--topic must be a topic name, got {topic!r}")
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
        try:
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
        except (TypeError, AttributeError) as exc:
            # The contract is read_topic() -> list[BrokerMessage]. A broken
            # wire or test-double handing back non-list containers or
            # non-message entries would otherwise traceback raw here (and a
            # None/empty junk read must never pass the gate vacuously) — fail
            # closed with a clean error instead.
            raise CliError(f"broker scan returned invalid messages: {exc}") from exc
    return None


def cmd_check(args) -> int:
    config = getattr(args, "config", None)
    if not isinstance(config, (str, os.PathLike)):
        raise CliError(f"check requires --config <rules.yaml>: got {config!r}")
    if not config or not Path(config).is_file():
        raise CliError("check requires --config <rules.yaml> (file must exist)")
    try:
        rs = load_rules_file(config)
    except RulesConfigError as exc:
        raise CliError(f"bad rules config: {exc}") from exc

    # A CI gate must never silently pass because no messages were supplied.
    topic = getattr(args, "topic", None)
    broker = getattr(args, "broker", None)
    messages = getattr(args, "messages", None)
    if topic and not broker:
        raise CliError("--topic requires --broker <bootstrap.servers>")
    if not messages and not (broker and topic):
        raise CliError(
            "check needs a data source: pass --messages <file> or "
            "--broker <bootstrap> --topic <name>"
        )

    try:
        violation_list = _validate(rs, messages, broker, topic,
                                   getattr(args, "schema_registry", None))
    except RulesConfigError as exc:
        raise CliError(f"bad message fixture: {exc}") from exc
    except ImportError as exc:
        raise CliError(str(exc)) from exc

    violates = len(violation_list) > 0
    max_violations = getattr(args, "max_violations", None)
    if max_violations is not None and not isinstance(max_violations, int):
        # A junk max-violations value (str/dict/list/...) can't participate in
        # the numeric gate comparison; fail closed so the gate never passes.
        max_v = 0
    else:
        max_v = max_violations if max_violations is not None else 0
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
    if getattr(args, "output", None):
        _write_out(args.output, out_text, force=True)
        print(
            f"ok={results['ok']} violations={len(violation_list)} "
            f"gate={max_v} -> {'PASS' if not gate_failed else 'FAIL'}",
            file=sys.stderr,
        )
    elif not getattr(args, "quiet", False):
        print(out_text)

    return EXIT_OK if not gate_failed else EXIT_FAIL


# ---------------------------------------------------------------------------
# topics (broker metadata)
# ---------------------------------------------------------------------------


def cmd_topics(args) -> int:
    bootstrap = getattr(args, "bootstrap", None)
    if not isinstance(bootstrap, str) or not bootstrap:
        raise CliError("topics requires --bootstrap <host:port>")
    try:
        from .broker import list_topics
    except ImportError:
        raise CliError("broker features require the 'broker' extra (confluent-kafka)")

    try:
        topics = list_topics(bootstrap, timeout=getattr(args, "timeout", None) or 10.0)
    except Exception as exc:
        raise CliError(f"failed to list topics from {bootstrap}: {exc}") from exc

    payload = {"topics": topics, "generated_at": now_utc_iso()}
    out_text = json.dumps(payload, indent=2, ensure_ascii=False)
    _write_out(getattr(args, "output", None), out_text, force=bool(getattr(args, "output", None)))
    return EXIT_OK


# ---------------------------------------------------------------------------
# registry (Schema Registry client)
# ---------------------------------------------------------------------------


def cmd_registry(args) -> int:
    registry_url = getattr(args, "registry_url", None)
    if not isinstance(registry_url, str) or not registry_url.startswith("http"):
        raise CliError("registry requires --registry-url <http://host:port>")

    try:
        from .registry import SchemaRegistryClient
    except ImportError:
        raise CliError("registry module unavailable")

    client = SchemaRegistryClient(registry_url, timeout=getattr(args, "timeout", None) or 10.0)
    subject = getattr(args, "subject", None)
    if subject:
        # Single subject: emit subject details (id/version/schema/../compat).
        try:
            versions = client.get_versions(subject)
            payload = {"subject": subject, "versions": []}
            for v in versions:
                info = client.get_version(subject, v)
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

    try:
        out_text = json.dumps(payload, indent=2, ensure_ascii=False)
    except TypeError as exc:
        raise CliError(f"registry response not JSON-serializable: {exc}") from exc
    _write_out(getattr(args, "output", None), out_text, force=bool(getattr(args, "output", None)))
    return EXIT_OK


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def cmd_report(args) -> int:
    topics = getattr(args, "topics", None)
    if not isinstance(topics, (str, os.PathLike)):
        raise CliError(f"report requires --topics <topics.json>: got {topics!r}")
    if not topics or not Path(topics).is_file():
        raise CliError("report requires --topics <topics.json>")
    registry = getattr(args, "registry", None)
    groups = getattr(args, "groups", None)
    for label, path in (("--registry", registry), ("--groups", groups)):
        if path is not None and not isinstance(path, (str, os.PathLike)):
            raise CliError(f"{label} file missing: {path!r}")
        if path and not Path(path).is_file():
            raise CliError(f"{label} file missing: {path}")
    violations: list = []

    rules = getattr(args, "rules", None)
    messages = getattr(args, "messages", None)
    if rules and messages:
        if not isinstance(messages, (str, os.PathLike)):
            raise CliError(f"--messages file missing: {messages!r}")
        if not Path(messages).is_file():
            raise CliError(f"--messages file missing: {messages}")
        if not isinstance(rules, (str, os.PathLike)):
            raise CliError(f"--rules file missing: {rules!r}")
        if not Path(rules).is_file():
            raise CliError(f"--rules file missing: {rules}")
        try:
            rs = load_rules_file(rules)
            violations = validate_fixture_file(rs, messages)
        except RulesConfigError as exc:
            raise CliError(f"bad rules/messages: {exc}") from exc
    elif getattr(args, "violations", None):
        vio = _load_json_or_exit(args.violations)
        if isinstance(vio, list):
            violations = vio
        elif isinstance(vio, dict) and isinstance(vio.get("violations"), list):
            violations = vio["violations"]

    try:
        rep = build_governance_report(
            topics_fixture=topics,
            registry_fixture=registry,
            groups_fixture=groups,
            rule_violations=violations,
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc

    fmt = getattr(args, "format", None) or "json"
    output = getattr(args, "output", None)
    if output and str(output).endswith(".csv"):
        fmt = "csv"
    if fmt == "csv":
        text = report_to_csv(rep)
        _write_out(output, text, force=bool(output))
    else:
        text = rep.to_json()
        _write_out(output, text, force=bool(output))
    return EXIT_OK


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def cmd_export(args) -> int:
    export_type = getattr(args, "type", None)
    if export_type == "schema-diff":
        # The export parser's --format describes *output* format; schema-diff's
        # --format describes the *schema* language. Translate to JSON/MD flags.
        export_fmt = getattr(args, "format", "json")
        if export_fmt in ("markdown", "md"):
            args.markdown = True
            args.json = False
        elif export_fmt == "json":
            args.json = True
            args.markdown = False
        else:
            # schema-diff can only render JSON or Markdown; an export that asks
            # for anything else (e.g. csv) must fail cleanly, not crash later
            # on the missing --json/--markdown attributes.
            raise CliError(
                f"schema-diff export supports --format json|markdown, got {export_fmt!r}"
            )
        for attr, default in (
            ("before_version", 1), ("after_version", 2),
            ("before_id", None), ("after_id", None),
        ):
            if not hasattr(args, attr):
                setattr(args, attr, default)
        args.format = getattr(args, "schema_format", None)
        return cmd_schema_diff(args)
    if export_type in ("check", "violations"):
        _require_export_format(args, {"json"})
        return cmd_check(args)
    if export_type == "report":
        _require_export_format(args, {"json", "csv"})
        return cmd_report(args)
    raise CliError(f"unknown export type: {export_type}")


def _require_export_format(args, supported: set[str]) -> None:
    """Reject an export --format the target command cannot render.

    ``check`` only renders JSON and ``report`` only JSON/CSV; asking for
    anything else (e.g. ``--format markdown``) would otherwise be silently
    ignored and emit the wrong payload with exit 0.
    """
    export_fmt = getattr(args, "format", "json")
    if not isinstance(export_fmt, str) or export_fmt not in supported:
        raise CliError(
            f"{getattr(args, 'type', None)} export supports --format {'|'.join(sorted(supported))}, "
            f"got {export_fmt!r}"
        )


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
    if argv is not None:
        # A CLI argv must be a list/tuple of argument strings; junk containers
        # (an int, a string, bytes, a dict, ...) or non-str entries inside the
        # list would crash argparse with a raw TypeError (list(args) on the
        # container, arg_string[0] on a non-str entry). Reject or drop them
        # up front so dispatch always exits cleanly with a usage code.
        if not isinstance(argv, (list, tuple)):
            print("kgov: usage: argv must be a list of argument strings", file=sys.stderr)
            return EXIT_USAGE
        argv = [a for a in argv if isinstance(a, str)]
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
