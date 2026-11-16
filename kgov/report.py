"""Governance report: topic inventory + owners + staleness + violation counts.

Fully offline. Backed by fixture inputs:

- ``topics``  — list of ``{name, partitions, replication_factor, owner, tags[]}``
- ``consumer_groups`` — list of ``{group, topics[], last_consumed_at (iso) / lag}``
- ``messages`` per topic — to derive sane-but-optional violation counts
  (alternatively a precomputed violation_count may be given directly).

The final report is a list of rows, one per topic::

    name, owner, tags, partitions, replication, schema_subject,
    schema_version, schema_state (COMPATIBLE/PENDING/BREAKING/NONE),
    latest_message_at, last_consumed_at, stale (bool), violation_count

Staleness is evaluated against the current time when ``now`` is not provided.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .model import now_utc_iso


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


def _read_json(path: str | Path) -> Any:
    if not isinstance(path, (str, os.PathLike)):
        # junk values (ints, bytes, lists, dicts, None, tuples, ...) are not
        # file paths — pathlib itself would raise a raw TypeError on bytes;
        # raise a clean error instead
        raise ValueError(f"fixture must be a file path: {path!r}")
    try:
        data = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: fixture is not valid UTF-8") from exc
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON fixture: {exc}") from exc


@dataclass
class StalenessSettings:
    """Tunables determining the 'stale' flag."""

    consumer_grace_seconds: int = 86400 * 3      # 3 days without a consumer read
    producer_grace_seconds: int = 86400 * 2      # 2 days without a produced message
    no_consumer_since_epoch: bool = False        # when there are no consumer groups at all


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            # out-of-range / platform-unrepresentable timestamps carry no
            # usable time — treat as absent instead of crashing the report
            return None
    else:
        try:
            s = str(value)
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass
class TopicState:
    name: str
    owner: str = "unknown"
    tags: list[str] = field(default_factory=list)
    partitions: int = 1
    replication_factor: int = 1
    schema_subject: Optional[str] = None
    schema_version: Optional[int] = None
    schema_state: str = "NONE"            # COMPATIBLE | ADDITIVE | BREAKING | PENDING | NONE
    latest_message_at: Optional[str] = None
    last_consumed_at: Optional[str] = None
    stale: bool = False
    violation_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def build_topic_state(
    topic: dict,
    registry: dict,
    groups: dict[str, dict],
    rule_topic_violations: dict[str, int],
    now: Optional[datetime] = None,
    settings: Optional[StalenessSettings] = None,
) -> TopicState:
    """Derive a single topic's governance row from fixture inputs."""
    settings = settings or StalenessSettings()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if not isinstance(topic, dict) or not topic.get("name"):
        raise ValueError(
            f"topics fixture entry must be a mapping with a 'name': {topic!r}"
        )
    name = topic["name"]
    # only a string name identifies a topic; junk truthy values (lists,
    # dicts, ints, bytes, ...) would crash the dict lookups below on an
    # unhashable key and are meaningless as identifiers
    if not isinstance(name, str):
        raise ValueError(
            f"topics fixture entry 'name' must be a string: {name!r}"
        )
    ts = TopicState(name=name)
    ts.owner = topic.get("owner") or "unknown"
    tags = topic.get("tags", [])
    # only string elements describe a tag; junk elements (ints, dicts, ...)
    # would crash the CSV join below (TypeError) and are meaningless as tags
    ts.tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []

    def _to_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    ts.partitions = _to_int(topic.get("partitions"), 1)
    ts.replication_factor = _to_int(topic.get("replication_factor"), 1)

    # schema state from registry subject
    subject = topic.get("schema_subject")
    # only a string subject names a registry entry (and is JSON-safe); junk
    # values (lists, dicts, ints, bytes, ...) are unhashable or meaningless
    # as keys — treat as absent everywhere in the row
    if not isinstance(subject, str) or not subject:
        subject = None
    ts.schema_subject = subject
    subjects = registry.get("subjects", {}) if isinstance(registry, dict) else {}
    if not isinstance(subjects, dict):
        # a malformed registry (subjects entry that is not a mapping) carries
        # no resolvable versions — ignore it rather than crash on .get
        subjects = {}
    versions = subjects.get(subject, []) if subject else []
    # only a list of version entries describes a history; junk values
    # (ints, strings, dicts, ...) must not crash reversed()/iteration below
    if not isinstance(versions, list):
        versions = []
    # only mapping entries describe a version — ignore junk (strings, ints, ...)
    latest = next((v for v in reversed(versions) if isinstance(v, dict)), None)
    if latest:
        ts.schema_version = latest.get("version")
        compat = latest.get("compatibility")
        # only string compat values describe a state; junk (lists, dicts, ...)
        # would crash the mapping lookup on an unhashable key
        if isinstance(compat, str):
            ts.schema_state = {"COMPATIBLE": "COMPATIBLE", "BACKWARD": "COMPATIBLE",
                               "ADDITIVE": "ADDITIVE", "BREAKING": "BREAKING",
                               "NONE": "NONE"}.get(compat, "COMPATIBLE")
        else:
            ts.schema_state = "COMPATIBLE"

    # message + consumer timestamps
    latest_msg = _parse_dt(topic.get("latest_message_at"))
    ts.latest_message_at = latest_msg.isoformat() if latest_msg else None

    # a non-mapping groups container (a bare int, list, ... passed directly
    # to build_topic_state) carries no consumer state — ignore it
    if not isinstance(groups, dict):
        groups = {}
    group = groups.get(name)
    if not isinstance(group, dict):
        group = {}
    last_consumed = _parse_dt(group.get("last_consumed_at"))
    ts.last_consumed_at = last_consumed.isoformat() if last_consumed else None

    # staleness
    if group is not None and last_consumed is not None:
        consumed_delta = now - last_consumed
        ts.stale = consumed_delta.total_seconds() > settings.consumer_grace_seconds
    else:
        # No consumer group known for the topic.
        ts.stale = settings.no_consumer_since_epoch
        if not ts.stale and latest_msg is not None:
            produced_delta = now - latest_msg
            ts.stale = produced_delta.total_seconds() > settings.producer_grace_seconds

    ts.violation_count = int(rule_topic_violations.get(name, 0))
    return ts


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


@dataclass
class GovernanceReport:
    generated_at: str = field(default_factory=now_utc_iso)
    total_topics: int = 0
    topics: list[TopicState] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "total_topics": self.total_topics,
            "topics": [t.to_dict() for t in self.topics],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def build_governance_report(
    topics_fixture: str | Path | list,
    registry_fixture: Optional[str | Path | list] = None,
    groups_fixture: Optional[str | Path | list] = None,
    rule_violations: Optional[list] = None,
    now: Optional[datetime] = None,
) -> GovernanceReport:
    """Build a full governance report from fixtures (plus optional violations)."""
    topics = topics_fixture if isinstance(topics_fixture, list) else _read_json(topics_fixture)
    if isinstance(topics, dict):
        topics = topics.get("topics", [])
    if not isinstance(topics, list):
        raise ValueError(
            f"topics fixture must resolve to a list of topic mappings "
            f"(got {type(topics).__name__}): {topics!r}"
        )

    registry = {}
    if registry_fixture:
        # a list is already resolved data, not a path; a bare list has no
        # {'subjects': ...} mapping so it is dropped as malformed below
        registry = (
            registry_fixture
            if isinstance(registry_fixture, (dict, list))
            else _read_json(registry_fixture)
        )
    if not isinstance(registry, dict):
        # a malformed registry (e.g. a bare list) carries no subjects —
        # ignore it rather than crash on attribute access below
        registry = {}

    raw_groups = {}
    if groups_fixture:
        raw_groups = (
            groups_fixture
            if isinstance(groups_fixture, (dict, list))
            else _read_json(groups_fixture)
        )
    groups = raw_groups.get("groups") if isinstance(raw_groups, dict) and "groups" in raw_groups else raw_groups
    if isinstance(groups, list):
        # list-form groups may contain junk entries — keep only mappings and
        # string keys (a list/dict key would crash the dict comprehension)
        groups = {
            key: g
            for g in groups if isinstance(g, dict)
            for key in [(g.get("topic", g.get("group")))]
            if isinstance(key, str)
        }
    elif not isinstance(groups, dict):
        groups = {}

    # violation counts per topic
    counts: dict[str, int] = {}
    if isinstance(rule_violations, list):
        # only lists of violation entries describe violations; junk (a bare
        # int, a mapping, ...) would crash the iteration below
        for vio in rule_violations:
            topic = vio.get("topic") if isinstance(vio, dict) else getattr(vio, "topic", None)
            # only string names can match a topic row; junk values (lists, dicts,
            # ints, ...) are ignored rather than crashing on an unhashable key
            if isinstance(topic, str) and topic:
                counts[topic] = counts.get(topic, 0) + 1

    now = now or datetime.now(timezone.utc)
    states = [
        build_topic_state(t, registry, groups, counts, now=now)
        for t in topics
    ]
    return GovernanceReport(total_topics=len(states), topics=states)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def report_to_csv(report: GovernanceReport) -> str:
    import csv
    import io

    buf = io.StringIO()
    cols = [
        "name", "owner", "tags", "partitions", "replication_factor",
        "schema_subject", "schema_version", "schema_state",
        "latest_message_at", "last_consumed_at", "stale", "violation_count",
    ]
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for t in report.topics:
        row = t.to_dict()
        row["tags"] = "|".join(t.tags)
        writer.writerow(row)
    return buf.getvalue()
