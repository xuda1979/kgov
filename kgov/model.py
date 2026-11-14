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
        return d

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
        if not self.results:
            lines.append("_No structural changes detected._")
            return "\n".join(lines)
        for r in self.results:
            icon = {"Breaking": "-", "Non-Breaking": "~", "Additive": "+"}.get(r.get("kind"), "?")
            path = r.get("path") or "(root)"
            lines.append(f"- {icon} **{r['kind']}** `{path}` — {r['message']}")
        return "\n".join(lines)
