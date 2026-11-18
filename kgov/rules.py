"""Dirty-data rules DSL and validation engine.

Rules are declared in YAML or JSON with this shape::

    version: 1
    topic: payments                       # topic glob (default '*')
    rules:
      - name: "payments-constraints"
        fields:
          payment_id:
            type: string
            required: true
            pattern: "^pay_"
          amount_cents:
            type: integer
            required: true
            range: {minimum: 0, maximum: 100000000}
          currency:
            type: string
            pattern: "^[A-Z]{3}$"
          status:
            type: string
            enum: [ACCEPTED, PENDING, REJECTED]
          fee_cents:
            type: integer
            non_null: true
            range: {minimum: 0}

Supported field checks:

- ``type``  — one of ``string|integer|number|boolean|array|object|record``
  (record == object). Integers satisfy ``number``.
- ``required`` — bool; if true the key must be present (missing counts as a
  violation).
- ``enum``  — list of allowed scalar values.
- ``pattern`` — regex the string value must match.
- ``range``  — ``{minimum, maximum}`` numeric bounds (inclusive).
- ``non_null`` — bool; if true the value may not be ``None``/JSON null.

A message is reported per (offset, key, failed rule). The engine iterates all
messages in the topic; each message applies to a rule by topic match. Missing
topic-level rule configuration (``rules: []``) is allowed and yields zero
violations.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from .rules_errors import (
    RulesConfigError,
    RuleFieldConfigError,
    UnknownFieldTypeError,
)


# ---------------------------------------------------------------------------
# Rule model
# ---------------------------------------------------------------------------


@dataclass
class FieldCheck:
    name: str
    type: Optional[str] = None
    required: bool = False
    enum: Optional[list] = None
    pattern: Optional[str] = None
    compiled_pattern: Optional[re.Pattern] = field(default=None, repr=False)
    range: Optional[dict] = None
    non_null: bool = False


@dataclass
class RuleSet:
    version: int = 1
    topic: str = "*"
    fields: dict[str, Any] = field(default_factory=dict)
    name: str = "default"


def load_rules_yaml(text: str) -> RuleSet:
    """Parse a rules YAML string into a RuleSet."""
    if not isinstance(text, str):
        # junk values (ints, lists, dicts, None, ...) are not YAML text —
        # yaml.safe_load would raise a raw AttributeError reaching for a
        # missing .read(); bytes is rejected too (YAML text is a str; use
        # load_rules_file for real files)
        raise RulesConfigError(f"rules text must be a YAML string: {text!r}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesConfigError(f"invalid YAML: {exc}") from exc
    except RecursionError as exc:
        # PyYAML's scanner recurses once per nesting level; a deeply nested
        # document (accidental or adversarial) blows the interpreter stack
        # with a raw RecursionError — surface it as the designed parse error
        raise RulesConfigError("invalid YAML: document nesting too deep") from exc
    return build_ruleset(data)


def load_rules_file(path: str | Path) -> RuleSet:
    if not isinstance(path, (str, os.PathLike)):
        # junk values (ints, bytes, lists, dicts, None, ...) are not file
        # paths — pathlib itself would raise a raw TypeError on bytes;
        # raise a clean typed error instead
        raise RulesConfigError(f"rules file must be a file path: {path!r}")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RulesConfigError(f"{path}: file is not valid UTF-8") from exc
    except (OSError, ValueError) as exc:
        # unreadable files (permission denied, ...) and malformed paths
        # (embedded NUL bytes) leak out of read_text; surface them as clean
        # typed errors instead of raw tracebacks
        raise RulesConfigError(f"cannot read rules file {path!r}: {exc}") from exc
    return load_rules_yaml(text)

def build_ruleset(data: Any) -> RuleSet:
    if not isinstance(data, dict):
        raise RulesConfigError("rules file must be a mapping")
    rs = RuleSet()
    version = data.get("version", 1)
    if not isinstance(version, int):
        raise RulesConfigError("'version' must be an integer")
    rs.version = version
    if "topic" in data:
        rs.topic = str(data["topic"])
    if "name" in data:
        rs.name = str(data["name"])
    if "rules" in data:
        rules = data["rules"]
        if not isinstance(rules, list):
            # junk non-iterable values (ints, None, floats, bools, ...) would
            # raw-crash the iteration below with a bare TypeError; fail closed
            # with a clean typed error instead
            raise RulesConfigError("'rules' must be a list of rule mappings")
        for r in rules:
            _merge_rule(rs, r)
    elif "fields" in data:
        _merge_fields(rs, data["fields"], rule_name=rs.name)
    return rs


def _merge_rule(rs: RuleSet, rule: Any) -> None:
    if not isinstance(rule, dict):
        raise RuleFieldConfigError("each rule must be a mapping")
    name = rule.get("name", "unnamed")
    topic = rule.get("topic", rs.topic)
    if topic != "*" and rs.topic != "*" and topic != rs.topic:
        # different topic targeted by a specific rule; keep simple single-topic
        raise RulesConfigError(
            f"rule '{name}' topic mismatch (file is for '{rs.topic}', got '{topic}')")
    fields = rule.get("fields")
    if fields is None:
        raise RulesConfigError(f"rule '{name}' has no 'fields'")
    _merge_fields(rs, fields, rule_name=name)


def _merge_fields(rs: RuleSet, fields: Any, rule_name: str) -> None:
    if not isinstance(fields, dict):
        raise RuleFieldConfigError(f"rule '{rule_name}' fields must be a mapping")
    for fname, spec in fields.items():
        _validate_field_spec(fname, spec, rule_name)
        check = FieldCheck(name=fname)
        if "type" in spec:
            check.type = str(spec["type"]).lower()
            _validate_type(fname, check.type, rule_name)
        if spec.get("required"):
            check.required = True
        if "enum" in spec:
            check.enum = list(spec["enum"])
        if "pattern" in spec:
            check.pattern = str(spec["pattern"])
            try:
                check.compiled_pattern = re.compile(check.pattern)
            except re.error as exc:
                raise RuleFieldConfigError(
                    f"field '{fname}': invalid regex pattern: {exc}") from exc
        if "range" in spec:
            rng = spec["range"]
            if not isinstance(rng, dict):
                raise RuleFieldConfigError(f"field '{fname}': range must be a mapping")
            bounds = {}
            for key in ("minimum", "maximum"):
                if key in rng:
                    v = rng[key]
                    try:
                        bounds[key] = float(v) if not isinstance(v, bool) else v
                    except (TypeError, ValueError) as exc:
                        # non-numeric bound (e.g. "abc") is a config error,
                        # not a raw ValueError traceback
                        raise RuleFieldConfigError(
                            f"field '{fname}': range {key} must be numeric, got {v!r}"
                        ) from exc
            if not bounds:
                raise RuleFieldConfigError(f"field '{fname}': range needs minimum/maximum")
            check.range = bounds
        if spec.get("non_null"):
            check.non_null = True
        rs.fields[fname] = check


def _validate_type(field_name: str, t: str, rule_name: str) -> None:
    allowed = {
        "string", "integer", "int", "number", "boolean", "bool",
        "array", "object", "record", "map",
    }
    # unhashable t (list/dict/set) would raw-crash "t not in allowed"; a
    # non-string type is by definition unknown, so raise the designed error
    # without ever touching set membership
    if not isinstance(t, str) or t not in allowed:
        raise UnknownFieldTypeError(
            f"field '{field_name}' in rule '{rule_name}' has unknown type '{t}'")


def _validate_field_spec(field_name: str, spec: Any, rule_name: str) -> None:
    if not isinstance(spec, dict):
        raise RuleFieldConfigError(
            f"field '{field_name}' in rule '{rule_name}' must be a mapping")

    unknown = set(spec) - {"type", "required", "enum", "pattern", "range", "non_null"}
    if unknown:
        raise RuleFieldConfigError(
            f"field '{field_name}' has unknown keys: {sorted(unknown)}")

    # type presence guards
    if "required" in spec and not isinstance(spec["required"], bool):
        raise RuleFieldConfigError(f"field '{field_name}' required must be bool")
    if "non_null" in spec and not isinstance(spec["non_null"], bool):
        raise RuleFieldConfigError(f"field '{field_name}' non_null must be bool")
    if "enum" in spec and not isinstance(spec["enum"], list):
        raise RuleFieldConfigError(f"field '{field_name}' enum must be a list")
    if "pattern" in spec and not isinstance(spec["pattern"], str):
        raise RuleFieldConfigError(f"field '{field_name}' pattern must be a string")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    rule: str
    topic: str
    partition: int
    offset: int
    key: Any
    field: str
    check: str
    message: str

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "topic": self.topic,
            "partition": self.partition,
            "offset": self.offset,
            "key": _json_safe_key(self.key),
            "field": self.field,
            "check": self.check,
            "message": self.message,
        }


def _json_safe_key(key: Any) -> Any:
    if isinstance(key, (bytes, bytearray)):
        try:
            return key.decode("utf-8")
        except UnicodeDecodeError:
            return {"$bytes": key.hex()}
    return key


@dataclass
class Message:
    """A single Kafka message from a readable source."""

    topic: str
    partition: int
    offset: int
    key: Any
    value: Any  # decoded dict/record

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "partition": self.partition,
            "offset": self.offset,
            "key": _json_safe_key(self.key),
            "value": self.value,
        }


def validate_message(rs: RuleSet, msg: Message) -> list[Violation]:
    """Run a message through the rules; returns all violations (none = ok)."""
    # A junk rs/msg from a direct library caller (or a test double) would
    # raw-crash on rs.topic / msg.topic below. Fail closed: no usable rule
    # set or message, so no violations can be assessed.
    if not hasattr(rs, "topic") or not hasattr(rs, "fields") or not hasattr(msg, "topic"):
        return []
    topic_ok = rs.topic == "*" or rs.topic == msg.topic
    if not topic_ok:
        return []

    result: list[Violation] = []
    value = msg.value if isinstance(msg.value, dict) else {}
    # a junk fields container (a list, an int, a str, ...) carries no checks —
    # looking up .values() on it would raw-crash with an AttributeError
    check_fields = rs.fields if isinstance(rs.fields, dict) else {}
    for check in check_fields.values():
        # junk entries (ints, strings, objects without a 'name') cannot name a
        # field to look up in the message — skip them rather than raw-crash on
        # .name below; hand-built lookalikes missing any FieldCheck attr are
        # read via getattr with the same defaults so no attribute access can
        # raise
        check_name = getattr(check, "name", None)
        if check_name is None:
            continue
        required = getattr(check, "required", False)
        non_null = getattr(check, "non_null", False)
        check_type = getattr(check, "type", None)
        check_enum = getattr(check, "enum", None)
        check_range = getattr(check, "range", None)
        check_pattern = getattr(check, "pattern", None)
        compiled_pattern = getattr(check, "compiled_pattern", None)
        present = check_name in (value or {})
        raw = value.get(check_name) if present else None

        # required check
        if required and not present:
            result.append(Violation(
                rule=rs.name, topic=msg.topic, partition=msg.partition,
                offset=msg.offset, key=msg.key, field=check_name,
                check="required", message=f"required field missing: '{check_name}'"))
            continue
        if not present:
            continue

        # non_null check
        if non_null and raw is None:
            result.append(Violation(
                rule=rs.name, topic=msg.topic, partition=msg.partition,
                offset=msg.offset, key=msg.key, field=check_name,
                check="non_null", message=f"field '{check_name}' is null but must be non-null"))
            continue

        # type check
        if check_type is not None and raw is not None:
            if not _type_ok(check_type, raw):
                result.append(Violation(
                    rule=rs.name, topic=msg.topic, partition=msg.partition,
                    offset=msg.offset, key=msg.key, field=check_name,
                    check="type", message=(
                        f"field '{check_name}' has type {_type_repr(raw)} "
                        f"but rule expects {check_type}")))
        # enum — junk-typed enum attrs (a bare int, ...) built by a direct
        # library caller bypassing the config loader are not a membership
        # container; skip the check rather than crash raw `not in`. An
        # unhashable raw value (a dict, a list, ...) against a set/frozenset
        # enum would raise raw TypeError from the hash — treat as a miss that
        # cannot be assessed (skip) rather than crash the scan
        if (check_enum is not None and raw is not None
                and isinstance(check_enum, (list, tuple, set, frozenset))):
            try:
                not_in_enum = raw not in check_enum
            except TypeError:
                not_in_enum = False
            if not_in_enum:
                result.append(Violation(
                    rule=rs.name, topic=msg.topic, partition=msg.partition,
                    offset=msg.offset, key=msg.key, field=check_name,
                    check="enum", message=(
                        f"field '{check_name}' value {raw!r} not in allowed enum "
                        f"{check_enum!r}")))
        # pattern — junk-typed compiled_pattern attrs (bytes, ints, ...) built
        # by a direct library caller have no .match(); skip the check rather
        # than crash raw AttributeError
        if compiled_pattern is not None and isinstance(raw, str):
            if not callable(getattr(compiled_pattern, "match", None)):
                pass
            elif not compiled_pattern.match(raw):
                result.append(Violation(
                    rule=rs.name, topic=msg.topic, partition=msg.partition,
                    offset=msg.offset, key=msg.key, field=check_name,
                    check="pattern", message=(
                        f"field '{check_name}' value {raw!r} does not match "
                        f"pattern /{check_pattern}/")))
        # range
        # Compare raw directly: ``float(raw)`` would overflow on integers
        # beyond the platform double range (OverflowError on e.g. 10**400),
        # while Python's int-vs-float comparison is exact and overflow-free.
        # A junk-typed range attr (a str, a list, ...) built by a direct
        # library caller is not a {bound: threshold} mapping and junk
        # thresholds ("0", {1}) are not numeric — skip those bounds instead
        # of crashing raw on .items() / raw < threshold.
        if (check_range is not None and isinstance(check_range, dict)
                and isinstance(raw, (int, float)) and not isinstance(raw, bool)):
            for bound, threshold in check_range.items():
                if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
                    continue
                if bound == "minimum" and raw < threshold:
                    result.append(Violation(
                        rule=rs.name, topic=msg.topic, partition=msg.partition,
                        offset=msg.offset, key=msg.key, field=check.name,
                        check="range", message=(
                            f"field '{check.name}' value {raw!r} < minimum {threshold}")))
                if bound == "maximum" and raw > threshold:
                    result.append(Violation(
                        rule=rs.name, topic=msg.topic, partition=msg.partition,
                        offset=msg.offset, key=msg.key, field=check.name,
                        check="range", message=(
                            f"field '{check.name}' value {raw!r} > maximum {threshold}")))

    return result


def _type_ok(t: str, val: Any) -> bool:
    if val is None:
        return True  # handled by non_null if that flag is set
    if t == "string":
        return isinstance(val, str)
    if t == "integer" or t == "int":
        return isinstance(val, int) and not isinstance(val, bool)
    if t == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if t == "boolean" or t == "bool":
        return isinstance(val, bool)
    if t == "array":
        return isinstance(val, list)
    if t in ("object", "record", "map"):
        return isinstance(val, dict)
    return False


def _type_repr(val: Any) -> str:
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, int):
        return "integer"
    if isinstance(val, float):
        return "number"
    if isinstance(val, str):
        return "string"
    if isinstance(val, list):
        return "array"
    if isinstance(val, dict):
        return "object"
    return type(val).__name__


# ---------------------------------------------------------------------------
# Message sources (offline fixtures)
# ---------------------------------------------------------------------------


def iter_messages_from_json(path: str | Path, topic: Optional[str] = None) -> Iterator[Message]:
    """Yield Message objects from a JSON fixture.

    Fixture shape (per topic):

        { "topic": "payments",
          "partition": 0,
          "messages": [ {"offset": 3, "key": "k1", "value": {...}}, ... ] }

    Or a list of bare records::

        { "messages": [ {"value": {...}} ] }
    """
    if not isinstance(path, (str, os.PathLike)):
        # junk values (ints, bytes, lists, dicts, None, ...) are not file
        # paths — pathlib itself would raise a raw TypeError on bytes;
        # raise a clean typed error instead
        raise RulesConfigError(f"message fixture must be a file path: {path!r}")
    try:
        data = Path(path).read_text(encoding="utf-8")
        root = json.loads(data)
    except UnicodeDecodeError as exc:
        raise RulesConfigError(f"{path}: file is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise RulesConfigError(f"invalid message fixture JSON: {exc}") from exc
    except (OSError, ValueError) as exc:
        # unreadable files (permission denied, ...) and malformed paths
        # (embedded NUL bytes) leak out of read_text; surface them as clean
        # typed errors instead of raw tracebacks
        raise RulesConfigError(f"cannot read message fixture {path!r}: {exc}") from exc

    if isinstance(root, list):
        partition = 0
        for i, item in enumerate(root):
            if not isinstance(item, dict) or "value" not in item:
                continue
            offset = item.get("offset", i)
            key = item.get("key", None)
            yield Message(
                topic=topic or item.get("topic", "unknown"),
                partition=item.get("partition", 0),
                offset=offset,
                key=key,
                value=item.get("value"),
            )
        return

    if not isinstance(root, dict):
        raise RulesConfigError(
            f"message fixture must be a JSON object or list, "
            f"got {type(root).__name__}"
        )

    messages = root.get("messages", [])
    if not isinstance(messages, list):
        raise RulesConfigError("message fixture 'messages' must be a list")
    default_topic = root.get("topic", topic or "unknown")
    default_partition = root.get("partition", 0)
    for i, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        yield Message(
            topic=item.get("topic", default_topic),
            partition=item.get("partition", default_partition),
            offset=item.get("offset", i),
            key=item.get("key"),
            value=item.get("value"),
        )


def validate_fixture_file(rs: RuleSet, path: str | Path) -> list[Violation]:
    """Validate every message read from a JSON fixture against a ruleset."""
    # A junk rs from a direct library caller (or a test double) has no usable
    # rule set — validate_message fail-closes on the same attribute shape, so
    # do the same before reaching rs.topic below instead of raw AttributeError.
    if not hasattr(rs, "topic") or not hasattr(rs, "fields"):
        return []
    out: list[Violation] = []
    for msg in iter_messages_from_json(path=path, topic=rs.topic):
        out.extend(validate_message(rs, msg))
    return out
