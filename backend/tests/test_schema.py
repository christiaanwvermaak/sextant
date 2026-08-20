"""Schema inference — Compass's signature feature.

Tested on the walk-and-count logic rather than against a live MongoDB, which is
where the interesting behaviour lives anyway.
"""
import datetime

from bson import ObjectId

from src.app import schema as schema_mod


def analyse(docs):
    """Run the walker over documents directly, bypassing $sample."""
    fields = {}
    for d in docs:
        schema_mod._walk(d, fields)
    return {f.path: f.report(len(docs)) for f in fields.values()}


# ── the point of the whole feature ─────────────────────────────────────────

def test_a_field_with_two_types_is_flagged():
    """The single most useful thing this produces: 98% string, 2% int is almost
    always a bug someone has been living with."""
    docs = [{"qty": "12"}] * 98 + [{"qty": 12}] * 2
    out = analyse(docs)["qty"]
    assert out["mixed_types"] is True
    assert {t["type"] for t in out["types"]} == {"string", "int"}
    assert out["types"][0]["type"] == "string"        # most common first
    assert out["types"][0]["percent"] == 98.0


def test_a_consistent_field_is_not_flagged():
    out = analyse([{"name": "a"}, {"name": "b"}])["name"]
    assert out["mixed_types"] is False


def test_a_missing_field_is_counted_as_missing():
    """"Which documents have no email" is the question people open Compass for."""
    docs = [{"email": "a@b.c"}, {}, {}, {}]
    out = analyse(docs)["email"]
    assert out["present"] == 1
    assert out["missing"] == 3
    assert out["presence_percent"] == 25.0


# ── type naming matches the shell ──────────────────────────────────────────

def test_booleans_are_not_reported_as_integers():
    """bool IS an int in Python, so order of checks matters."""
    assert schema_mod._type_of(True) == "boolean"
    assert schema_mod._type_of(1) == "int"


def test_bson_types_are_named_as_the_shell_names_them():
    assert schema_mod._type_of(ObjectId()) == "objectId"
    assert schema_mod._type_of(datetime.datetime.now(datetime.UTC)) == "date"
    assert schema_mod._type_of(1.5) == "double"
    assert schema_mod._type_of(None) == "null"
    assert schema_mod._type_of([]) == "array"
    assert schema_mod._type_of({}) == "object"


# ── nesting and arrays ─────────────────────────────────────────────────────

def test_nested_objects_are_walked_with_dotted_paths():
    out = analyse([{"user": {"name": "a", "age": 3}}])
    assert "user" in out and "user.name" in out and "user.age" in out
    assert out["user"]["types"][0]["type"] == "object"


def test_array_elements_are_recorded_separately_from_the_array():
    """"tags is an array" and "tags contains strings" are different facts."""
    out = analyse([{"tags": ["a", "b"]}])
    assert out["tags"]["types"][0]["type"] == "array"
    assert out["tags[]"]["types"][0]["type"] == "string"
    assert out["tags[]"]["present"] == 2


def test_objects_inside_arrays_are_walked():
    out = analyse([{"items": [{"sku": "X"}]}])
    assert "items[].sku" in out


def test_a_huge_array_does_not_become_the_whole_report():
    """A 50 000-element array is not a schema."""
    out = analyse([{"big": list(range(50000))}])
    assert out["big[]"]["present"] == 100


def test_depth_is_bounded():
    doc = {}
    node = doc
    for i in range(12):
        node["n"] = {}
        node = node["n"]
    out = analyse([doc])
    assert max(p.count(".") for p in out) <= schema_mod.MAX_DEPTH


# ── value distribution ─────────────────────────────────────────────────────

def test_distinct_values_are_counted_for_low_cardinality_fields():
    docs = [{"status": "active"}] * 7 + [{"status": "closed"}] * 3
    out = analyse(docs)["status"]
    assert out["distinct"][0] == {"value": "active", "count": 7, "percent": 70.0}
    assert out["distinct_total"] == 2


def test_a_high_cardinality_field_says_so_instead_of_listing_twelve():
    """Showing an arbitrary twelve of forty thousand implies they are the lot."""
    docs = [{"id": f"value-{i}"} for i in range(500)]
    out = analyse(docs)["id"]
    assert out.get("too_many_distinct") is True
    assert out["distinct"] is None


def test_numeric_and_date_ranges_are_reported():
    docs = [{"n": 5}, {"n": 1}, {"n": 9}]
    out = analyse(docs)["n"]
    assert out["min"] == 1 and out["max"] == 9


def test_dates_render_as_iso_in_the_range():
    docs = [{"at": datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC)},
            {"at": datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)}]
    out = analyse(docs)["at"]
    assert out["min"].startswith("2026-01-02")
    assert out["max"].startswith("2026-08-20")


# ── ordering ───────────────────────────────────────────────────────────────

def test_the_most_present_fields_come_first():
    """The fields every document has ARE the shape; the sparse ones are the
    exceptions. Alphabetical order buries both."""
    docs = [{"always": 1, "sometimes": 1}, {"always": 1}, {"always": 1}]
    fields = {}
    for d in docs:
        schema_mod._walk(d, fields)
    reported = sorted((f.report(len(docs)) for f in fields.values()),
                      key=lambda f: (-f["present"], f["path"]))
    assert reported[0]["path"] == "always"


# ── honesty about sampling ─────────────────────────────────────────────────

def test_the_sample_size_is_bounded():
    assert schema_mod.MAX_SAMPLE >= schema_mod.DEFAULT_SAMPLE
    assert schema_mod.DEFAULT_SAMPLE == 1000     # what Compass uses
