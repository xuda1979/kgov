"""Schema evolution / diff engine: Avro and JSON Schema.

Classifies changes between two schemas as one of:

- **Breaking**: readers of the old schema will reject data produced with the
  new schema (field removed, type changed incompatibly, required field added).
- **Additive**: new optional fields / new enum symbols / new union branches —
  data produced with the new schema is readable by old readers.
- **Non-Breaking**: changes that neither remove information nor require new
  input from producers (e.g. primitive widening int->long, adding a format).

The overall report kind is **Breaking** if any breaking change is found,
otherwise **Additive** if any additive change is found, otherwise Non-Breaking.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .model import (
    CompatibilityKind,
    SchemaDiffReport,
    SchemaFormat,
)


class SchemaParseError(ValueError):
    """Raised when a schema string is not valid for its declared format."""


_MISSING = object()


def _printable(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _desc(value: Any) -> str:
    """Short text description of a schema node for messages."""
    if isinstance(value, str):
        return f"`{value}`"
    if isinstance(value, list):
        return " | ".join(_desc(v) for v in value)
    if isinstance(value, dict):
        t = value.get("type")
        if isinstance(t, str) and t not in ("record", "enum", "fixed", "array", "map"):
            return f"`{t}`"
        name = value.get("name")
        if name:
            return f"`{name}` ({t or 'type'})"
        if t:
            return f"`{t}`"
        return "object"
    return "object"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_PRIMITIVE_PRIORITY = {
    "null": 0,
    "boolean": 1,
    "int": 2,
    "long": 3,
    "float": 4,
    "double": 5,
    "bytes": 6,
    "string": 7,
}


def parse_schema(text: Any, format: Optional[str] = None) -> tuple[str, Any]:
    """
    Parse a schema given as JSON text (or dict).

    Returns ``(format, schema)`` where ``schema`` is the parsed structure.
    ``format`` is normalized to SchemaFormat.AVRO or SchemaFormat.JSON,
    raised to SchemaParseError if unparseable.
    """
    if isinstance(text, str):
        stripped = text.strip()
        if not stripped:
            raise SchemaParseError("schema is empty")
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SchemaParseError(f"invalid JSON: {exc}") from exc
    elif isinstance(text, dict):
        obj = text
    else:
        raise SchemaParseError(f"unsupported schema input: {type(text).__name__}")

    fmt = (format or "").lower()
    if fmt in (SchemaFormat.AVRO, SchemaFormat.JSON):
        return fmt, obj

    # Guess from structure.
    if isinstance(obj, dict):
        if obj.get("$schema"):
            return SchemaFormat.JSON, obj
        t = obj.get("type")
        if isinstance(t, list):
            return SchemaFormat.AVRO, obj
        if t in _PRIMITIVE_PRIORITY:
            # "-record" style AVRO also valid: "string"
            return SchemaFormat.AVRO, obj
        if t in ("record", "enum", "fixed", "array", "map", "error"):
            return SchemaFormat.AVRO, obj
        if obj.get("properties") is not None or obj.get("$id"):
            return SchemaFormat.JSON, obj
        return SchemaFormat.AVRO, obj
    if isinstance(obj, list):
        return SchemaFormat.AVRO, obj
    return SchemaFormat.JSON, obj


def parse_schema_text(text: str, format: Optional[str] = None) -> tuple:
    return parse_schema(text, format)


def _name_of(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return _name_of(node[0]) if node else ""
    if isinstance(node, dict):
        return str(node.get("name") or node.get("type") or "")
    return ""


def _type_names(node: Any) -> set:
    """Set of Avro type names a node may produce (named types included)."""
    if isinstance(node, str):
        return {node}
    if isinstance(node, list):
        out: set = set()
        for b in node:
            out |= _type_names(b)
        return out
    if isinstance(node, dict):
        if isinstance(node.get("type"), list):
            return _type_names(node["type"])
        return {node.get("type") or "record"}
    return set()


def _types_assignable(a: Any, b: Any) -> bool:
    """True if every value producible under *b* fits under *a* (b ⊆ a)."""
    for tb in _type_names(b):
        if tb == "null" or tb in _PRIMITIVE_PRIORITY:
            for ta in _type_names(a):
                if ta == tb:
                    break
                if ta in _PRIMITIVE_PRIORITY and tb in _PRIMITIVE_PRIORITY:
                    if _PRIMITIVE_PRIORITY[ta] > _PRIMITIVE_PRIORITY[tb]:
                        break
            else:
                return False
        elif tb not in _type_names(a):
            return False
    return True


def _is_nullable(node: Any) -> bool:
    return isinstance(node, list) and "null" in [x if isinstance(x, str) else x.get("type") for x in node]


# ---------------------------------------------------------------------------
# Avro diff
# ---------------------------------------------------------------------------

def _join(p: str, child: str) -> str:
    return f"{p}.{child}" if p else child


def _avro_diff(a: Any, b: Any, path: str = "", results: Optional[list] = None) -> list:
    if results is None:
        results = []

    def add(kind: str, p: str, msg: str) -> None:
        results.append({"kind": kind, "path": p, "message": msg})

    # --- unions (lists at type position) ---
    if isinstance(a, list) or isinstance(b, list):
        if not isinstance(a, list):
            a = [a]
        if not isinstance(b, list):
            b = [b]
        a_left = list(a)
        for member_b in b:
            # match by name (named types) or identity (primitives)
            idx = None
            for i, member_a in enumerate(a_left):
                if _name_of(member_a) == _name_of(member_b) and _name_of(member_a):
                    idx = i
                    break
                if isinstance(member_a, str) and isinstance(member_b, str) and member_a == member_b:
                    idx = i
                    break
            if idx is not None:
                matched_a = a_left.pop(idx)
                if _name_of(member_a) == _name_of(member_b):  # both named, recurse
                    if isinstance(matched_a, dict) and isinstance(member_b, dict) and matched_a is not member_b:
                        _avro_diff(matched_a, member_b, path, results)
            else:
                add(CompatibilityKind.ADDITIVE, path,
                    f"union branch added: {_desc(member_b)}")
        for leftover in a_left:
            add(CompatibilityKind.BREAKING, path,
                f"union branch removed: {_desc(leftover)}")
        return results

    # --- both primitives / named refs (strings) ---
    if isinstance(a, str) and isinstance(b, str):
        if a == b:
            return results
        if a in _PRIMITIVE_PRIORITY and b in _PRIMITIVE_PRIORITY:
            if _PRIMITIVE_PRIORITY[b] < _PRIMITIVE_PRIORITY[a]:
                add(CompatibilityKind.BREAKING, path,
                    f"type narrowed: `{a}` → `{b}` (old readers can't read wider values)")
            else:
                add(CompatibilityKind.NON_BREAKING, path,
                    f"type widened: `{a}` → `{b}`")
        else:
            add(CompatibilityKind.BREAKING, path,
                f"type changed: `{a}` → `{b}`")
        return results

    # --- one is a bare string, the other structured ---
    if isinstance(a, str) != isinstance(b, str):
        ca = _type_names([a] if isinstance(a, str) else a)
        cb = _type_names([b] if isinstance(b, str) else b)
        if _types_assignable_set(cb, ca):
            add(CompatibilityKind.NON_BREAKING, path,
                f"type widened: {_desc(a)} → {_desc(b)}")
        elif _types_assignable_set(ca, cb):
            add(CompatibilityKind.NON_BREAKING, path,
                f"type narrowed compatibly: {_desc(a)} → {_desc(b)}")
        else:
            add(CompatibilityKind.BREAKING, path,
                f"type changed: {_desc(a)} → {_desc(b)}")
        return results

    if not isinstance(a, dict) or not isinstance(b, dict):
        if a == b:
            return results
        add(CompatibilityKind.BREAKING, path,
            f"type changed: {_desc(a)} → {_desc(b)}")
        return results

    ta, tb = a.get("type"), b.get("type")
    if isinstance(ta, list) or isinstance(tb, list):
        _avro_diff(ta, tb, path, results)
        return results

    # named type rename
    if a.get("name") and b.get("name") and a["name"] != b["name"]:
        add(CompatibilityKind.BREAKING, path,
            f"named type renamed: `{a['name']}` → `{b['name']}`")
        return results

    if ta != tb:
        # different avro type tokens
        if ta in _PRIMITIVE_PRIORITY and tb in _PRIMITIVE_PRIORITY:
            if _PRIMITIVE_PRIORITY[tb] < _PRIMITIVE_PRIORITY[ta]:
                add(CompatibilityKind.BREAKING, path,
                    f"type narrowed: `{ta}` → `{tb}`")
            else:
                add(CompatibilityKind.NON_BREAKING, path,
                    f"type widened: `{ta}` → `{tb}`")
        else:
            add(CompatibilityKind.BREAKING, path,
                f"type changed: `{ta}` → `{tb}`")
        return results

    # --- same type token ---
    if ta in ("record", "error"):
        # a malformed `fields` list may contain junk entries (strings, dicts
        # without a string name, ...) — keep only usable field mappings so the
        # diff tolerates bad input instead of crashing on f["name"]
        old_fields = [f for f in (a.get("fields") or [])
                      if isinstance(f, dict) and isinstance(f.get("name"), str)]
        new_fields = [f for f in (b.get("fields") or [])
                      if isinstance(f, dict) and isinstance(f.get("name"), str)]
        od = {f["name"]: f for f in old_fields}
        nd = {f["name"]: f for f in new_fields}

        for fa in old_fields:
            nf = nd.get(fa["name"])
            if nf is None:
                add(CompatibilityKind.BREAKING, _join(path, fa["name"]),
                    f"field removed: `{fa['name']}`")
                continue
            _avro_diff(fa.get("type"), nf.get("type"), _join(path, fa["name"]), results)
            old_default = fa.get("default", _MISSING)
            new_default = nf.get("default", _MISSING)
            if old_default is _MISSING and new_default is not _MISSING:
                add(CompatibilityKind.ADDITIVE, _join(path, fa["name"]),
                    f"default added for existing field `{fa['name']}`")
            elif old_default is not _MISSING and new_default is _MISSING:
                add(CompatibilityKind.NON_BREAKING, _join(path, fa["name"]),
                    f"default removed from existing field `{fa['name']}`")

        for fb in new_fields:
            if fb["name"] in od:
                continue
            is_nullable = _is_nullable(fb.get("type"))
            has_default = fb.get("default", _MISSING) is not _MISSING
            if has_default or is_nullable:
                add(CompatibilityKind.ADDITIVE, _join(path, fb["name"]),
                    f"field added (optional): `{fb['name']}`")
            else:
                add(CompatibilityKind.BREAKING, _join(path, fb["name"]),
                    f"required field added without default: `{fb['name']}`")

    elif ta == "enum":
        # symbols must be strings; junk entries (dicts, lists) would be
        # unhashable in set arithmetic — keep only string symbols
        old_syms = {s for s in (a.get("symbols") or []) if isinstance(s, str)}
        new_syms = {s for s in (b.get("symbols") or []) if isinstance(s, str)}
        for s in old_syms - new_syms:
            add(CompatibilityKind.BREAKING, _join(path, s),
                f"enum symbol removed: `{s}`")
        for s in new_syms - old_syms:
            add(CompatibilityKind.ADDITIVE, _join(path, s),
                f"enum symbol added: `{s}`")

    elif ta == "array":
        _avro_diff(a.get("items"), b.get("items"), _join(path, "items"), results)

    elif ta == "map":
        _avro_diff(a.get("values"), b.get("values"), _join(path, "values"), results)

    elif ta == "fixed":
        if a.get("size") != b.get("size"):
            add(CompatibilityKind.BREAKING, path,
                f"fixed size changed: {a.get('size')} → {b.get('size')}")

    elif ta in _PRIMITIVE_PRIORITY and isinstance(b, dict) and b.get("type") == ta:
        pass  # extra attributes (doc/aliases) — no-op

    return results


def _types_assignable_set(a: set, b: set) -> bool:
    """Every type in set a is assignable from some type in set b."""
    for tb in b:
        if tb in _PRIMITIVE_PRIORITY:
            for ta in a:
                if ta == tb or (ta in _PRIMITIVE_PRIORITY and _PRIMITIVE_PRIORITY[ta] > _PRIMITIVE_PRIORITY[tb]):
                    break
            else:
                return False
        elif tb not in a:
            return False
    return True


# ---------------------------------------------------------------------------
# JSON Schema diff
# ---------------------------------------------------------------------------

# Rank of JSON Schema primitive "production shapes".
_JSON_RANK = {"null": 0, "boolean": 1, "integer": 2, "number": 3, "string": 4}


def _js_string_types(node: Any) -> set:
    """Approximate set of JSON types a schema node accepts."""
    if node is True or node == {}:
        return {"any"}
    if node is False:
        return set()
    if not isinstance(node, dict):
        return {"any"}
    t = node.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        # only string entries describe a type; junk (dicts, lists, ...) would
        # be unhashable in set building below
        return {x for x in t if isinstance(x, str)}
    out: set = set()
    for key in ("anyOf", "oneOf"):
        branches = node.get(key)
        # only a list of branches describes accepted types; junk values
        # (ints, strings, dicts, ...) would crash the iteration below
        if isinstance(branches, list):
            for sub in branches:
                out |= _js_string_types(sub)
    if "enum" in node:
        return { "enum" }
    if "properties" in node or "additionalProperties" in node or "required" in node:
        out.add("object")
    if "items" in node:
        out.add("array")
    if "const" in node:
        return {"const"}
    if out:
        return out
    return {"any"}


def _js_scalar_rank(name: str) -> Optional[int]:
    return _JSON_RANK.get(name, None)


def _js_strings_assignable(children: set, parent: set) -> bool:
    """True if every accepted type in ``children`` is also accepted by ``parent``.

    ``number`` acts as a supertype of ``integer``.
    """
    if "any" in parent:
        return True
    for tchild in children:
        if tchild == "any":
            return False
        matched = False
        for tpar in parent:
            if tpar == "any":
                matched = True
                break
            if tpar == tchild:
                matched = True
                break
            if tpar == "number" and tchild == "integer":
                matched = True
                break
        if not matched:
            return False
    return True


def _js_diff(a: Any, b: Any, path: str = "", results: Optional[list] = None) -> list:
    if results is None:
        results = []

    def add(kind: str, p: str, msg: str) -> None:
        results.append({"kind": kind, "path": p, "message": msg})

    if isinstance(a, bool) or isinstance(b, bool):
        if a == b:
            return results
        # True -> False removes accepted values (breaking); False -> True adds them.
        if a is True and b is False:
            add(CompatibilityKind.BREAKING, path, "schema narrowed to reject everything")
        else:
            add(CompatibilityKind.ADDITIVE, path, "schema widened to accept everything")
        return results

    if not isinstance(a, dict) or not isinstance(b, dict):
        add(CompatibilityKind.BREAKING, path, "schema node structure changed")
        return results

    sa, sb = _js_string_types(a), _js_string_types(b)
    if not _js_strings_assignable(sa, sb):
        # new schema accepts a strict subset => Breaking
        add(CompatibilityKind.BREAKING, path,
            f"accepted types narrowed: {sorted(sa)} → {sorted(sb)}")
    elif sa != sb:
        # new schema accepts a strict superset => Additive
        add(CompatibilityKind.ADDITIVE, path,
            f"accepted types widened: {sorted(sa)} → {sorted(sb)}")

    # required arrays — only a list describes property names; junk values
    # (ints, strings, dicts, ...) must not crash iteration below
    a_req = a.get("required") if isinstance(a.get("required"), list) else []
    b_req = b.get("required") if isinstance(b.get("required"), list) else []
    ra = {p for p in a_req if isinstance(p, str)}
    rb = {p for p in b_req if isinstance(p, str)}
    for prop in ra - rb:
        add(CompatibilityKind.ADDITIVE, _join(path, prop),
            f"property no longer required: `{prop}`")
    for prop in rb - ra:
        add(CompatibilityKind.BREAKING, _join(path, prop),
            f"property now required: `{prop}`")

    # properties
    pa = a.get("properties") if isinstance(a.get("properties"), dict) else {}
    pb = b.get("properties") if isinstance(b.get("properties"), dict) else {}
    for k, av in pa.items():
        bv = pb.get(k)
        if bv is None:
            add(CompatibilityKind.BREAKING, _join(path, k),
                f"property removed: `{k}`")
            continue
        _js_diff(av, bv, _join(path, k), results)
    for k, bv in pb.items():
        if k in pa:
            continue
        # additive if not required
        add(CompatibilityKind.ADDITIVE, _join(path, k),
            f"property added: `{k}`")

    # additionalProperties
    aa, ab = a.get("additionalProperties"), b.get("additionalProperties")
    if aa != ab:
        if ab is False and aa is not False:
            add(CompatibilityKind.BREAKING, path,
                "additional properties now disallowed")
        elif aa is False and ab is not False:
            add(CompatibilityKind.ADDITIVE, path,
                "additional properties now allowed")

    # enum (values may be unhashable — objects/arrays — so compare by a
    # canonical serialization rather than by set membership on raw values)
    if "enum" in a or "enum" in b:
        def _enum_key(v):
            if isinstance(v, (dict, list)):
                return json.dumps(v, sort_keys=True, default=_printable)
            return repr(v)
        # only a list of allowed values describes an enum; junk values
        # (ints, dicts, strings, ...) must not crash iteration below
        a_enum = {_enum_key(v): v for v in (a.get("enum", []) if isinstance(a.get("enum"), list) else [])}
        b_enum = {_enum_key(v): v for v in (b.get("enum", []) if isinstance(b.get("enum"), list) else [])}
        for key, v in a_enum.items():
            if key not in b_enum:
                add(CompatibilityKind.BREAKING, path,
                    f"enum value removed: {_printable(v)}")
        for key, v in b_enum.items():
            if key not in a_enum:
                add(CompatibilityKind.ADDITIVE, path,
                    f"enum value added: {_printable(v)}")

    for kw in ("items",):
        if kw in a or kw in b:
            _js_diff(a.get(kw, True), b.get(kw, True), _join(path, kw), results)

    # format / minLength / max are Non-Breaking breadcrumbs unless tightened.
    # Only numeric bounds are comparable; junk bounds (strings, lists, ...)
    # must not crash on `<`/`>` between mismatched types.
    def _is_number(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for kw in ("minLength", "minItems", "minimum"):
        av, bv = a.get(kw), b.get(kw)
        if _is_number(av) and _is_number(bv) and bv > av:
            add(CompatibilityKind.NON_BREAKING, _join(path, kw),
                f"lower bound tightened: {av} → {bv}")
        if _is_number(av) and _is_number(bv) and bv < av:
            add(CompatibilityKind.ADDITIVE, _join(path, kw),
                f"lower bound relaxed: {av} → {bv}")
    for kw in ("maxLength", "maxItems", "maximum"):
        av, bv = a.get(kw), b.get(kw)
        if _is_number(av) and _is_number(bv) and bv < av:
            add(CompatibilityKind.NON_BREAKING, _join(path, kw),
                f"upper bound tightened: {av} → {bv}")
        if _is_number(av) and _is_number(bv) and bv > av:
            add(CompatibilityKind.ADDITIVE, _join(path, kw),
                f"upper bound relaxed: {av} → {bv}")

    return results


def diff_json_schema(
    old: Any,
    new: Any,
    subject: str = "record",
    before_version: int = 1,
    after_version: int = 2,
    before_id: Optional[int] = None,
    after_id: Optional[int] = None,
) -> SchemaDiffReport:
    results = _js_diff(old, new)
    kind = classify(results)
    return SchemaDiffReport(
        format=SchemaFormat.JSON,
        subject=subject,
        before_version=before_version,
        after_version=after_version,
        before_id=before_id,
        after_id=after_id,
        kind=kind,
        results=[dict(r) for r in results],
    )


def diff_avro(
    old: Any,
    new: Any,
    subject: str = "record",
    before_version: int = 1,
    after_version: int = 2,
    before_id: Optional[int] = None,
    after_id: Optional[int] = None,
) -> SchemaDiffReport:
    results = _avro_diff(old, new)
    kind = classify(results)
    return SchemaDiffReport(
        format=SchemaFormat.AVRO,
        subject=subject,
        before_version=before_version,
        after_version=after_version,
        before_id=before_id,
        after_id=after_id,
        kind=kind,
        results=[dict(r) for r in results],
    )


def classify(results: list) -> str:
    if any(r.get("kind") == CompatibilityKind.BREAKING for r in results):
        return CompatibilityKind.BREAKING
    if any(r.get("kind") == CompatibilityKind.ADDITIVE for r in results):
        return CompatibilityKind.ADDITIVE
    return CompatibilityKind.NON_BREAKING


def diff_schemas(
    old_text: str | dict,
    new_text: str | dict,
    subject: str = "record",
    before_version: int = 1,
    after_version: int = 2,
    before_id: Optional[int] = None,
    after_id: Optional[int] = None,
    format: Optional[str] = None,
) -> SchemaDiffReport:
    """High-level entry: diff any two schemas (Avro or JSON Schema).

    Format is auto-detected, or forced via ``format`` ("avro"/"json").
    """
    fmt_old, old = parse_schema(old_text, format)
    fmt_new, new = parse_schema(new_text, format)
    if fmt_old != fmt_new:
        raise SchemaParseError(
            f"cannot diff across formats: {fmt_old} vs {fmt_new}")
    if fmt_old == SchemaFormat.AVRO:
        return diff_avro(old, new, subject, before_version, after_version,
                         before_id, after_id)
    return diff_json_schema(old, new, subject, before_version, after_version,
                            before_id, after_id)
