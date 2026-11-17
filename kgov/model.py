"""Shared data model for kgov.

Kept dependency-free (only stdlib + dataclasses) so higher-level modules
can import it without pulling in avro/jsonschema.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
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


def _json_safe(value: Any) -> Any:
    """Recursively coerce non-JSON-safe values (bytes, bytearray, set, tuple)
    into plain JSON-safe forms so ``to_dict()`` output can go straight to
    ``json.dumps`` with no ``default=`` hook. Unchanged leaves stay as-is."""
    if isinstance(value, dict):
        # dict keys are NOT coerced by the recursion below; byte/tuple keys
        # would make json.dumps raw-crash ("keys must be str, int, float,
        # bool or None"). Coerce non-JSON-safe keys like rules._json_safe_key.
        return {_json_safe_key(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
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
        return asdict(self)


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
        d = asdict(self)
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
