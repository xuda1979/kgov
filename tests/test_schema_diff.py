"""P0 — schema diff engine tests."""

from __future__ import annotations

import json

import pytest

from kgov.model import CompatibilityKind, SchemaFormat
from kgov.schema_diff import (
    SchemaParseError,
    classify,
    diff_avro,
    diff_json_schema,
    diff_schemas,
    parse_schema,
)


def _schema(s: dict) -> dict:
    return json.loads(json.dumps(s))


def test_avro_additive_optional_field():
    old = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "long"}]}
    new = {
        "type": "record",
        "name": "User",
        "fields": [
            {"name": "id", "type": "long"},
            {"name": "email", "type": ["null", "string"], "default": None},
        ],
    }
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.ADDITIVE
    assert rep.format == SchemaFormat.AVRO
    findings = {(r["path"], r["kind"]) for r in rep.results}
    assert ("email", CompatibilityKind.ADDITIVE) in findings


def test_avro_breaking_removed_field():
    old = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "long"}, {"name": "x", "type": "string"}]}
    new = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "long"}]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING
    findings = {(r["path"], r["kind"]) for r in rep.results}
    assert ("x", CompatibilityKind.BREAKING) in findings


def test_avro_breaking_required_field_added_without_default():
    old = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "long"}]}
    new = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "long"}, {"name": "new_req", "type": "string"}]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING
    findings = {(r["path"], r["kind"]) for r in rep.results}
    assert ("new_req", CompatibilityKind.BREAKING) in findings


def test_avro_non_breaking_widen():
    old = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "int"}]}
    new = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "long"}]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.NON_BREAKING
    assert len(rep.results) >= 1


def test_avro_breaking_narrow():
    old = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "long"}]}
    new = {"type": "record", "name": "User", "fields": [{"name": "id", "type": "int"}]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_avro_enum_symbol_added_additive():
    old = {"type": "enum", "name": "Color", "symbols": ["RED", "GREEN"]}
    new = {"type": "enum", "name": "Color", "symbols": ["RED", "GREEN", "BLUE"]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.ADDITIVE


def test_avro_enum_symbol_removed_breaking():
    old = {"type": "enum", "name": "Color", "symbols": ["RED", "GREEN"]}
    new = {"type": "enum", "name": "Color", "symbols": ["RED"]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_avro_record_renamed_breaking():
    old = {"type": "record", "name": "Old", "fields": [{"name": "id", "type": "long"}]}
    new = {"type": "record", "name": "New", "fields": [{"name": "id", "type": "long"}]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_avro_fixed_size_change_breaking():
    old = {"type": "fixed", "name": "F", "size": 8}
    new = {"type": "fixed", "name": "F", "size": 16}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_avro_union_branch_removed_breaking():
    old = {"type": "record", "name": "U", "fields": [{"name": "v", "type": ["null", "string", "int"]}]}
    new = {"type": "record", "name": "U", "fields": [{"name": "v", "type": ["null", "string"]}]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_json_add_optional_property():
    old = {"type": "object", "properties": {"id": {"type": "integer"}}}
    new = {"type": "object", "properties": {"id": {"type": "integer"}, "email": {"type": "string"}}}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.ADDITIVE
    assert rep.format == SchemaFormat.JSON


def test_json_property_now_required_breaking():
    old = {"type": "object", "properties": {"id": {"type": "integer"}}}
    new = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_json_property_removed_breaking():
    old = {"type": "object", "properties": {"id": {"type": "integer"}, "x": {"type": "string"}}}
    new = {"type": "object", "properties": {"id": {"type": "integer"}}}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_json_widen_number_additive():
    old = {"type": "object", "properties": {"v": {"type": "integer"}}}
    new = {"type": "object", "properties": {"v": {"type": "number"}}}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.ADDITIVE


def test_json_narrow_integer_breaking():
    old = {"type": "object", "properties": {"v": {"type": "number"}}}
    new = {"type": "object", "properties": {"v": {"type": "integer"}}}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_json_enum_removed_breaking():
    old = {"type": "string", "enum": ["a", "b"]}
    new = {"type": "string", "enum": ["a"]}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING


def test_json_enum_added_additive():
    old = {"type": "string", "enum": ["a", "b"]}
    new = {"type": "string", "enum": ["a", "b", "c"]}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.ADDITIVE


def test_json_enum_object_value_removed_breaking():
    """Enum entries may be objects; comparing them must not crash on
    unhashable entries and must still classify removals as Breaking."""
    old = {"type": "string", "enum": [{"a": 1}, {"a": 2}]}
    new = {"type": "string", "enum": [{"a": 1}]}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING
    assert any("{\"a\": 2}" in r["message"] for r in rep.results)


def test_json_enum_object_value_added_additive():
    old = {"type": "string", "enum": [{"a": 1}]}
    new = {"type": "string", "enum": [{"a": 1}, [1, 2]]}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.ADDITIVE


def test_parse_schema_auto_detects_format():
    fmt, _ = parse_schema(json.dumps({"type": "record", "name": "A", "fields": []}))
    assert fmt == SchemaFormat.AVRO
    fmt2, _ = parse_schema(json.dumps({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}))
    assert fmt2 == SchemaFormat.JSON


def test_parse_schema_invalid_json():
    with pytest.raises(SchemaParseError):
        parse_schema("{not valid json")


def test_avro_junk_fields_tolerated():
    """A record whose `fields` list contains junk entries (strings, dicts
    without a string name) must not crash on f[\"name\"] indexing."""
    old = {"type": "record", "name": "A", "fields": ["garbage", {"type": "long"}, {"name": "id", "type": "long"}]}
    new = {"type": "record", "name": "A", "fields": [42, {"name": "id", "type": "long"}, {"name": "email", "type": "string"}]}
    rep = diff_avro(_schema(old), _schema(new))
    findings = {(r["path"], r["kind"]) for r in rep.results}
    # the junk entries were skipped and did not crash; the usable comparison
    # (email added without a default) is still reported as Breaking
    assert findings == {("email", CompatibilityKind.BREAKING)}


def test_avro_enum_junk_symbols_tolerated():
    """An enum whose `symbols` list contains unhashable junk (dicts/lists)
    must not crash on set arithmetic."""
    old = {"type": "enum", "name": "C", "symbols": ["RED", "GREEN", {"x": 1}]}
    new = {"type": "enum", "name": "C", "symbols": ["RED", ["x", 1]]}
    rep = diff_avro(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.BREAKING  # GREEN removed
    messages = " ".join(r["message"] for r in rep.results)
    assert "GREEN" in messages


def test_json_required_junk_tolerated():
    """A JSON Schema `required` array containing non-string junk must not
    crash on set membership (unhashable entries)."""
    old = {"$schema": "http://json-schema.org/draft-07/schema#",
           "type": "object", "required": ["id", {"x": 1}], "properties": {"id": {"type": "integer"}}}
    new = {"$schema": "http://json-schema.org/draft-07/schema#",
           "type": "object", "required": ["id", {"x": 1}], "properties": {"id": {"type": "integer"}, "email": {"type": "string"}}}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.ADDITIVE
    assert any(r["path"] == "email" for r in rep.results)


def test_json_properties_list_tolerated():
    """A JSON Schema whose `properties` value is a list (not a mapping) must
    not crash on .items()."""
    old = {"$schema": "http://json-schema.org/draft-07/schema#",
           "type": "object", "properties": ["garbage", {"id": {}}]}
    new = {"$schema": "http://json-schema.org/draft-07/schema#",
           "type": "object", "properties": ["garbage"]}
    rep = diff_json_schema(_schema(old), _schema(new))
    # nothing usable to compare, but no crash
    assert rep.kind in (CompatibilityKind.NON_BREAKING, CompatibilityKind.ADDITIVE)


def test_json_junk_bounds_tolerated():
    """Non-numeric bound values (string vs int) in minLength/minItems must not
    crash on `>` between mismatched types; numeric tightening still works."""
    old = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "string", "minLength": "abc"}
    new = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "string", "minLength": 5}
    rep = diff_json_schema(_schema(old), _schema(new))
    assert rep.kind == CompatibilityKind.NON_BREAKING

    old2 = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "string", "minLength": 3}
    new2 = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "string", "minLength": 5}
    rep2 = diff_json_schema(_schema(old2), _schema(new2))
    messages = " ".join(r["message"] for r in rep2.results)
    assert "tightened" in messages and "3" in messages and "5" in messages


def test_diff_schemas_forwarding():
    old = {"type": "record", "name": "A", "fields": [{"name": "x", "type": "int"}]}
    new = {"type": "record", "name": "A", "fields": [{"name": "x", "type": "long"}]}
    rep = diff_schemas(json.dumps(old), json.dumps(new), subject="s", before_version=1, after_version=2)
    assert rep.kind == CompatibilityKind.NON_BREAKING


def test_diff_across_formats_rejects():
    with pytest.raises(SchemaParseError):
        diff_schemas(
            json.dumps({"type": "record", "name": "A", "fields": []}),
            json.dumps({"type": "object", "properties": {}}),
        )


def test_classify_priority():
    results = [
        {"kind": CompatibilityKind.ADDITIVE, "path": "a", "message": "x"},
        {"kind": CompatibilityKind.BREAKING, "path": "b", "message": "y"},
    ]
    assert classify(results) == CompatibilityKind.BREAKING
    assert classify([{"kind": CompatibilityKind.ADDITIVE, "path": "a", "message": "x"}]) == CompatibilityKind.ADDITIVE
    assert classify([{"kind": CompatibilityKind.NON_BREAKING, "path": "a", "message": "x"}]) == CompatibilityKind.NON_BREAKING


def test_evolution_fixture_series(avro_series, json_series):
    """Validate the schema-evolution fixture series classifies as intended."""
    expected = {
        "avro_removed_field": CompatibilityKind.BREAKING,
        "avro_added_required_field": CompatibilityKind.BREAKING,
        "avro_added_optional_field": CompatibilityKind.ADDITIVE,
        "json_removed_property": CompatibilityKind.BREAKING,
        "json_added_required": CompatibilityKind.BREAKING,
        "json_added_optional": CompatibilityKind.ADDITIVE,
        "json_widened": CompatibilityKind.ADDITIVE,
        "json_narrowed": CompatibilityKind.BREAKING,
    }
    for fmt, fname in (("avro_series", "series"), ("json_series", "series")):
        pass
    for coll in (avro_series, json_series):
        for block in coll["series"]:
            for sub in block.get("series", []):
                name = sub["name"]
                if name not in expected:
                    continue
                before = sub["compare"]["before"]["schema"]
                after = sub["compare"]["after"]["schema"]
                rep = diff_schemas(before, after, subject=name)
                assert rep.kind == expected[name], f"{name}: got {rep.kind}"
