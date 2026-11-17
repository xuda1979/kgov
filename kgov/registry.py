"""Confluent-wire Schema Registry client + fixture conversion.

Speaks the Confluent Schema Registry REST API with plain stdlib (urllib) so the
core package stays broker-free::

    GET /subjects                                  -> ["payments-value", ...]
    GET /subjects/{subject}/versions               -> [1, 2, 3]
    GET /subjects/{subject}/versions/{version}     -> {"subject", "version", "id", "schema"}
    GET /subjects/{subject}/versions/latest
    GET /config/{subject}                          -> {"compatibilityLevel": "BACKWARD"}
    GET /schemas/ids/{id}                          -> {"schema": "..."}

The exact schema payload on the wire is a JSON *string*; this module normalizes
it to a parsed dict so the result is directly usable by :func:`kgov.report.build_governance_report`
as a registry fixture (``{"subjects": {...}}``).
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Optional


class RegistryError(ValueError):
    """Raised when a Schema Registry request fails (connect / HTTP / parse)."""


def _parse_schema_string(schema: Any) -> Any:
    """The Confluent wire returns schema as a JSON string; normalize to a dict."""
    if schema is None:
        return None
    if isinstance(schema, (dict, list)):
        return schema
    try:
        return json.loads(str(schema))
    except json.JSONDecodeError:
        # Not parseable as JSON — keep the raw string (e.g. a bare primitive).
        return str(schema)


class SchemaRegistryClient:
    """Minimal read-only client for a Confluent-compatible Schema Registry."""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 10.0):
        self.base_url = str(base_url).rstrip("/")
        self.token = token
        self.timeout = timeout
        self._headers = {
            "Accept": "application/vnd.schemaregistry.v1+json, application/json",
        }
        if token:
            self._headers["Authorization"] = token

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RegistryError(f"registry {exc.code} for {path}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RegistryError(f"cannot reach {self.base_url}: {exc.reason}") from exc
        except http.client.HTTPException as exc:
            # RemoteDisconnected / IncompleteRead / BadStatusLine from a
            # server that died mid-response are not ValueErrors
            raise RegistryError(f"registry dropped the connection for {path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise RegistryError(f"registry returned invalid UTF-8 for {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RegistryError(f"registry returned invalid JSON for {path}: {exc}") from exc
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            # junk/out-of-range timeouts (non-numeric, NaN, too-large floats)
            # raise raw TypeError/ValueError/OverflowError from
            # socket.settimeout before any connection attempt, and raw OSErrors
            # surfaced outside URLError/HTTPException stay unbundled — funnel
            # them all through the designed RegistryError contract
            raise RegistryError(f"registry request for {path} failed: {exc}") from exc

    def is_reachable(self) -> bool:
        try:
            self.list_subjects()
            return True
        except RegistryError:
            return False

    # ------------------------------------------------------------------
    # Confluent wire API
    # ------------------------------------------------------------------

    def list_subjects(self) -> list[str]:
        data = self._get("/subjects")
        if not isinstance(data, list):
            raise RegistryError("registry /subjects did not return a list")
        return [str(s) for s in data]

    def get_versions(self, subject: str) -> list[int]:
        data = self._get(f"/subjects/{_quote_path(subject)}/versions")
        if not isinstance(data, list):
            raise RegistryError(f"/subjects/{subject}/versions did not return a list")
        try:
            return sorted(int(v) for v in data)
        except (TypeError, ValueError) as exc:
            raise RegistryError(f"/subjects/{subject}/versions malformed: {data!r}") from exc

    def get_version(self, subject: str, version: int | str = "latest") -> dict:
        data = self._get(f"/subjects/{_quote_path(subject)}/versions/{version}")
        if not isinstance(data, dict):
            raise RegistryError(f"/subjects/{subject}/versions/{version} malformed")
        compat = self._compatibility_level(subject)
        return {
            "id": data.get("id"),
            "version": data.get("version", version),
            "compatibility": compat,
            "schema": _parse_schema_string(data.get("schema")),
        }

    def get_latest(self, subject: str) -> dict:
        return self.get_version(subject, "latest")

    def get_schema_by_id(self, schema_id: int) -> dict:
        try:
            schema_id_int = int(schema_id)
        except (TypeError, ValueError) as exc:
            raise RegistryError(f"invalid schema id {schema_id!r}") from exc
        data = self._get(f"/schemas/ids/{schema_id_int}")
        if not isinstance(data, dict):
            raise RegistryError(f"/schemas/ids/{schema_id_int} malformed")
        return {"id": schema_id_int, "schema": _parse_schema_string(data.get("schema"))}

    def _compatibility_level(self, subject: str) -> Optional[str]:
        try:
            data = self._get(f"/config/{_quote_path(subject)}")
        except RegistryError:
            return None
        level = data.get("compatibilityLevel") if isinstance(data, dict) else None
        return str(level) if level else None

    # ------------------------------------------------------------------
    # kgov fixture conversion (report.py consumes this shape)
    # ------------------------------------------------------------------

    def to_fixture_dict(self) -> dict:
        """Return the offline registry fixture shape ``{"subjects": {...}}``."""
        subjects: dict[str, list[dict]] = {}
        for subject in sorted(self.list_subjects()):
            versions = []
            for v in self.get_versions(subject):
                info = self.get_version(subject, v)
                versions.append({
                    "id": info["id"],
                    "version": info["version"],
                    "compatibility": info["compatibility"],
                    "schema": info["schema"],
                })
            subjects[subject] = versions
        return {"subjects": subjects}


def _quote_path(segment: str) -> str:
    from urllib.parse import quote

    return quote(str(segment), safe="")
