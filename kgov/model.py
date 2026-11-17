"""Shared data model for kgov.

Kept dependency-free (only stdlib + dataclasses) so higher-level modules
can import it without pulling in avro/jsonschema.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Optional


def now_utc_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_default(obj: Any) -> Any:
    """json.dumps default for dataclass/decimal/bytes values."""
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj.hex()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (int, float)):
        try:
            f = float(obj)
            if not math.isfinite(f):
                return str(obj)
        except (TypeError, ValueError):
            pass
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return vars(obj)
    return repr(obj)


def _json_safe(value: Any, _seen: Optional[set] = None) -> Any:
    """Recursively coerce non-JSON-safe values (bytes, bytearray, memoryview,
    set, tuple) into plain JSON-safe forms so that ``to_dict()`` output can go
    straight to ``json.dumps`` with no ``default=`` hook. Unchanged leaves
    stay as-is.

    Self-referencing containers (a dict/list/set holding itself, directly or
    transitively) would recurse forever and exit as a raw RecursionError on
    the export path; cycle-tracking turns them back into a string leaf so
    they serialize like any other junk value.
    """
    if isinstance(value, memoryview):
        # memoryview is byte-like but not bytes/bytearray, so the byte path
        # below would not see it; materialize the buffer into bytes so it is
        # decoded (utf-8) or hexed like any other raw byte container
        value = bytes(value)
    if isinstance(value, dict):
        if _seen is None:
            _seen = set()
        vid = id(value)
        if vid in _seen:
            # a cyclic container would recurse forever (RecursionError on the
            # export path); hand back its repr — already a JSON-safe leaf
            return repr(value)
        _seen.add(vid)
        try:
            # dict keys are NOT coerced by the recursion below; byte/tuple
            # keys would make json.dumps raw-crash ("keys must be str, int,
            # float, bool or None"). Coerce non-JSON-safe keys like
            # rules._json_safe_key.
            return {_json_safe_key(k): _json_safe(v, _seen) for k, v in value.items()}
        finally:
            _seen.discard(vid)
    if isinstance(value, (list, tuple, set)):
        if _seen is None:
            _seen = set()
        vid = id(value)
        if vid in _seen:
            return repr(value)
        _seen.add(vid)
        try:
            return [_json_safe(v, _seen) for v in value]
        finally:
            _seen.discard(vid)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value


def _json_safe_key(key: Any) -> Any:
    """Coerce a dict key to a type json.dumps accepts (str, int, float, bool,
    None). Bytes/bytearray decode like their value counterparts (utf-8, or a
    hex fallback); anything else that is not already JSON-safe is stringified
    so the exported dict stays dumpable instead of raw-crashing."""
    if isinstance(key, (bytes, bytearray)):
        try:
            return key.decode("utf-8")
        except UnicodeDecodeError:
            # a {$bytes: hex} dict is unhashable, so it can't be a dict key —
            # fall back to a hashable JSON-safe string marker instead
            return f"$bytes:{key.hex()}"
    if isinstance(key, (str, int, float, bool)) or key is None:
        return key
    return str(key)


# ---------------------------------------------------------------------------
# Schema diff
# ---------------------------------------------------------------------------

class SchemaFormat:
    AVRO = "avro"
    JSON = "json"  # JSON Schema


class CompatibilityKind:
    BREAKING = "Breaking"
    NON_BREAKING = "Non-Breaking"
    ADDITIVE = "Additive"

    ALL = (BREAKING, NON_BREAKING, ADDITIVE)


@dataclass(frozen=True)
class DiffResult:
    kind: str            # CompatibilityKind
    path: str            # dot path within the schema, "" = root
    message: str         # human readable explanation

    def to_dict(self) -> dict:
        # Build straight from declared fields instead of asdict(): asdict()
        # deep-copies every value, and un-picklable leaves (memoryview, ...)
        # raise raw TypeError from copy.deepcopy. Reading fields directly
        # keeps junk leaves as-is so callers (json.dumps with default=, ...)
        # can apply their own coercion.
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class SchemaDiffReport:
    format: str                     # SchemaFormat
    subject: str
    before_version: int
    after_version: int
    before_id: Optional[int] = None
    after_id: Optional[int] = None
    kind: str = CompatibilityKind.ADDITIVE  # overall classification
    results: list[dict] = field(default_factory=list)
    generated_at: str = field(default_factory=now_utc_iso)

    def to_dict(self) -> dict:
        # build from declared fields instead of asdict(): asdict() deep-copies
        # every value and un-picklable leaves (memoryview, ...) raise raw
        # TypeError from copy.deepcopy before any coercion could run. Reading
        # fields directly keeps junk leaves as-is for _json_safe below.
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["compatible"] = self.kind != CompatibilityKind.BREAKING
        # asdict() is raw: junk results entries (bytes, sets, non-string keys,
        # ...) would crash json.dumps on the export path. Coerce to JSON-safe
        # leaves, consistent with to_markdown()'s junk tolerance.
        return _json_safe(d)

    def to_markdown(self) -> str:
        lines = [
            f"# Schema diff — `{self.subject}`",
            "",
            f"- Format: **{self.format}**",
            f"- Version {self.before_version} → {self.after_version}",
            f"- Compatibility: **{self.kind}**"
            + (" (compatible)" if self.kind != CompatibilityKind.BREAKING else " (INCOMPATIBLE)"),
            "",
            "## Changes",
            "",
        ]
        # only an iterable of results can be rendered; junk values (an int, a
        # string, a mapping, ...) built by a direct caller would crash the
        # iteration below and carry no changes — fall back to the empty path
        if not isinstance(self.results, (list, tuple)) or not self.results:
            lines.append("_No structural changes detected._")
            return "\n".join(lines)
        icon_map = {"Breaking": "-", "Non-Breaking": "~", "Additive": "+"}
        for r in self.results:
            # only mapping entries describe a change; junk (None, ints,
            # strings, ...) would crash .get() below and is meaningless here
            if not isinstance(r, dict):
                continue
            raw_kind = r.get("kind")
            # only string kinds map to an icon; junk (lists, dicts, ...) is
            # unhashable or meaningless — fall back to the unknown marker
            kind = raw_kind if isinstance(raw_kind, str) else "?"
            icon = icon_map.get(kind, "?")
            raw_path = r.get("path")
            path = raw_path if isinstance(raw_path, str) and raw_path else "(root)"
            raw_message = r.get("message")
            message = raw_message if isinstance(raw_message, str) else ""
            lines.append(f"- {icon} **{kind}** `{path}` — {message}")
        return "\n".join(lines)
