"""Local mock Schema Registry — serves the Confluent wire on loopback.

Useful for offline development / tests without a live registry::

    server = MockRegistry(registry_fixture={"subjects": {...}})
    server.start()
    client = SchemaRegistryClient(server.base_url)
    ...
    server.stop()

Fixture shape is the same JSON the report module consumes
(``{"subjects": {"<subject>": [{"id", "version", "compatibility",
"schema"}, ...]}}``). The server is read-only (no registration endpoint).
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .registry import SchemaRegistryClient


class MockRegistryError(RuntimeError):
    """The mock server refused to start (e.g. ephemeral port exhausted)."""


def _as_int(value: Any, default: Any) -> Any:
    """Best-effort int conversion; junk values fall back to ``default``."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # out-of-range floats (inf, values beyond the platform double range)
        # raise OverflowError from int(); treat as absent
        return default


class _Unset:
    """Sentinel marking "no usable value" distinct from None."""


_UNSET = _Unset()


def _version_entries(versions: Any) -> list[dict]:
    """Keep only dict version entries; junk entries (ints, strings, lists,
    None, ...) would crash ``.get()``/``int()`` in the accessors below."""
    if not isinstance(versions, list):
        return []
    return [v for v in versions if isinstance(v, dict)]


def _wire_schema(schema_value: Any) -> str:
    """Best-effort Confluent-wire schema serialization.

    Junk values JSON cannot encode (bytes, sets, objects) fall back to their
    string form so a handler thread never dies mid-response.
    """
    try:
        return json.dumps(schema_value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(schema_value)


_JSON_HEADERS = {
    "Content-Type": "application/vnd.schemaregistry.v1+json, application/json",
    "Access-Control-Allow-Origin": "*",
}


class RegistryHandler(BaseHTTPRequestHandler):
    registry: "MockRegistry"

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # keep the test output quiet

    # -- helpers ---------------------------------------------------------

    def _reply(self, code: int, payload: Any) -> None:
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError):
            # junk payload shapes (bytes, sets, objects, non-string keys, ...)
            # fire before any wfile io; fall back to a serializable envelope
            # so the handler thread never dies mid-response
            body = json.dumps(str(payload)).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            for k, v in _JSON_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (AttributeError, OSError):
            # a handler without a real wfile (direct call / test double) or a
            # dropped client socket must not take the handler thread down
            return

    def _error(self, code: int, message: str, error_code: int = 40401) -> None:
        self._reply(code, {"error_code": error_code, "message": message})

    def _path_parts(self) -> list[str]:
        # only a string request line can be parsed as a URL; junk values
        # (ints, lists, dicts, ...) would raw-crash urlparse, and a missing
        # path attribute means "no path" — both must not take the handler down
        path = getattr(self, "path", None)
        path = path if isinstance(path, str) else ""
        path = unquote(urlparse(path).path)
        return [p for p in path.split("/") if p]

    def _subject_from_path(self) -> Optional[str]:
        parts = self._path_parts()
        # /subjects/{subject}/versions[/{version}]
        if len(parts) >= 2 and parts[0] == "subjects":
            return parts[1]
        if len(parts) >= 2 and parts[0] in ("config", "compatibility"):
            return parts[1]
        return None

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parts = self._path_parts()
        if not parts:
            self._reply(200, [])
            return
        head = parts[0]

        if not hasattr(self, "registry"):
            # a handler built without a registry bound (a direct call or test
            # double) has no backing data — fail closed to a clean 404 rather
            # than raw-crashing on attribute access mid-route
            self._error(404, f"No endpoint for {self.path}", 40401)
            return

        if head == "subjects":
            if len(parts) == 1:
                self._reply(200, self.registry.list_subjects())
                return
            subject = parts[1]
            if len(parts) == 3 and parts[2] == "versions":
                # /subjects/{subject}/versions
                versions = self.registry.versions(subject)
                if versions is None:
                    self._error(404, f"Subject '{subject}' not found.", 40401)
                    return
                self._reply(200, versions)
                return
            if len(parts) == 4 and parts[2] == "versions":
                # /subjects/{subject}/versions/{version}
                version: Any = parts[3]
                info = self.registry.version(subject, version)
                if info is None:
                    self._error(
                        404,
                        f"Version {version} not found for subject '{subject}'.",
                        40402,
                    )
                    return
                self._reply(200, info)
                return

        if head == "config":
            subject = parts[1] if len(parts) > 1 else None
            level = self.registry.compatibility_level(subject)
            if level is None:
                self._error(404, "Config not found.", 40401)
                return
            self._reply(200, {"compatibilityLevel": level})
            return

        if head == "schemas":
            # /schemas/ids/{id}
            if len(parts) >= 3 and parts[1] == "ids":
                try:
                    schema_id = int(parts[2])
                except ValueError:
                    self._error(404, "Schema not found.", 40403)
                    return
                schema = self.registry.schema_by_id(schema_id)
                if schema is None:
                    self._error(404, f"Schema {schema_id} not found.", 40403)
                    return
                self._reply(200, {"schema": _wire_schema(schema)})
                return

        if head == "mode":
            self._reply(200, {"mode": "READWRITE"})

        self._error(404, f"No endpoint for {self.path}", 40401)

    def do_POST(self) -> None:  # noqa: N802
        # Read-only mock: registration endpoints are intentionally absent.
        self._error(404, "Mock registry is read-only.", 40401)

    def do_PUT(self) -> None:  # noqa: N802
        self._error(405, "Method Not Allowed", 40501)

    def do_DELETE(self) -> None:  # noqa: N802
        self._error(405, "Method Not Allowed", 40501)


class MockRegistry:
    """An in-process Confluent-wire Schema Registry backed by a fixture dict."""

    def __init__(self, fixture: dict | None = None):
        self._fixture: dict[str, list[dict]] = {}
        if isinstance(fixture, dict):
            subjects = fixture.get("subjects", {})
            if isinstance(subjects, dict):
                for name, versions in subjects.items():
                    self._fixture[name] = _version_entries(versions)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._bound_port: int = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self, port: int = 0) -> str:
        """Bind on loopback (0 => OS-assigned ephemeral port); return base URL."""
        if self._server:
            return self.base_url
        handler = type("BoundHandler", (RegistryHandler,), {"registry": self})
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except (OSError, OverflowError, TypeError, ValueError) as exc:
            # junk ports (non-ints, out-of-range, non-tuples, ...) surface as
            # raw TypeError/OverflowError from the socketserver bind; report
            # them as a clean start failure like any other bind error
            raise MockRegistryError(f"mock registry failed to bind: {exc}") from exc
        self._bound_port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="mock-registry"
        )
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
            self._thread = None
        self._bound_port = 0

    def __enter__(self) -> "MockRegistry":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        if self._bound_port:
            return f"http://127.0.0.1:{self._bound_port}"
        return "http://127.0.0.1:0"

    @property
    def client(self) -> SchemaRegistryClient:
        """A ready-to-use SchemaRegistryClient pointed at this server."""
        return SchemaRegistryClient(self.base_url)

    # -- data access (wired into the handler above) --------------------------

    def set_fixture(self, fixture: dict) -> None:
        """Swap the backing fixture (for multi-phase tests)."""
        self._fixture = {}
        if isinstance(fixture, dict):
            subjects = fixture.get("subjects", {})
            if isinstance(subjects, dict):
                for name, versions in subjects.items():
                    self._fixture[name] = _version_entries(versions)

    def add_subject(self, subject: Any, *versions: dict) -> None:
        """Append a subject (or extend an existing one) with one or more versions."""
        if not isinstance(subject, str):
            # unhashable subject arguments (lists, dicts, sets, ...) would
            # crash setdefault with a raw TypeError; coerce to a stable key
            subject = str(subject)
        existing = self._fixture.setdefault(subject, [])
        # route through the same dict-only filter as __init__/set_fixture —
        # junk non-dict entries would crash .get()/int() in the accessors
        existing.extend(_version_entries(list(versions)))

    def list_subjects(self) -> list[str]:
        # key=str keeps heterogeneous (int/str) fixture keys sortable instead
        # of raising TypeError on the wire
        return sorted(self._fixture.keys(), key=str)

    def versions(self, subject: Any) -> Optional[list[int]]:
        if not isinstance(subject, str):
            subject = str(subject)
        versions = self._fixture.get(subject)
        if versions is None:
            return None
        return sorted(_as_int(v.get("version"), i) for i, v in enumerate(versions, start=1))

    def version(self, subject: Any, version: Any = "latest") -> Optional[dict]:
        if not isinstance(subject, str):
            subject = str(subject)
        versions = self._fixture.get(subject)
        if not versions:
            return None
        if version == "latest":
            info = versions[-1]
        else:
            target = _as_int(version, _UNSET)
            if target is _UNSET:
                return None
            info = next(
                (v for v in versions if _as_int(v.get("version"), -1) == target), None
            )
            if info is None:
                return None
        compatibility = info.get("compatibility")
        if not isinstance(compatibility, str):
            # junk-typed compatibility (set, bytes, dict, ...) would crash
            # json.dumps in the reply envelope; coerce to a safe string
            compatibility = None if compatibility is None else str(compatibility)
        return {
            "subject": subject,
            "version": _as_int(info.get("version"), len(versions)),
            "id": _as_int(info["id"], None) if "id" in info else None,
            "compatibility": compatibility,
            "schema": _wire_schema(info.get("schema")),
        }

    def compatibility_level(self, subject: Optional[str]) -> Optional[str]:
        if subject is None:
            return "BACKWARD"
        if not isinstance(subject, str):
            subject = str(subject)
        versions = self._fixture.get(subject)
        if not versions:
            return None
        pending = [v.get("compatibility") for v in versions if v.get("compatibility")]
        # Use the *newest* explicit compatibility marker, falling back to BACKWARD.
        if pending:
            return str(pending[-1])
        return "BACKWARD"

    def schema_by_id(self, schema_id: int) -> Optional[Any]:
        for versions in self._fixture.values():
            for v in versions:
                if _as_int(v.get("id"), -1) == _as_int(schema_id, -1):
                    return v.get("schema")
        return None
