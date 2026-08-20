"""Working out the shape of a collection by looking at it.

This is the thing Compass does that nothing else does, and it is the reason
people open Compass rather than a shell: a document store has no declared schema,
so the only way to know what is in a collection is to sample it and count.

**Sampled, not scanned.** `$sample` asks the server for a pseudo-random subset
and is the only honest way to do this on a collection of any size — a full scan
of forty million documents to draw a bar chart is not a feature, it is an
incident. The sample size is reported alongside the result so nobody mistakes
"3% of documents have no email" for a fact about the whole collection when it was
measured on a thousand.

**Types are counted per field, not assumed.** The interesting output is precisely
the field where 98% of values are strings and 2% are integers, because that is
usually a bug someone has been living with. A schema view that reported the most
common type and moved on would hide exactly what it exists to reveal.
"""
import collections
import datetime
import re

from bson import Decimal128, ObjectId
from bson.binary import Binary
from pymongo.errors import PyMongoError

from . import mongo

# How many documents to look at unless asked otherwise. Compass defaults to a
# thousand; so does this. Large enough to be representative, small enough that
# $sample stays cheap.
DEFAULT_SAMPLE = 1000
MAX_SAMPLE = 10000

# Nested objects are walked to this depth. Deeper than this the field list stops
# being readable and starts being a document dump, which is what the Documents
# tab is for.
MAX_DEPTH = 4

# Distinct values are only worth showing when there are few of them. A field with
# forty thousand distinct values is an identifier and the list tells you nothing.
MAX_DISTINCT = 12


def _type_of(value):
    """The BSON type name, as the shell and Compass both name them."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"          # before int: bool IS an int in Python
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, ObjectId):
        return "objectId"
    if isinstance(value, datetime.datetime):
        return "date"
    if isinstance(value, Decimal128):
        return "decimal"
    if isinstance(value, Binary):
        return "binData"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, re.Pattern):
        return "regex"
    return type(value).__name__


class _Field:
    __slots__ = ("path", "present", "types", "values", "too_many_values",
                 "min_value", "max_value")

    def __init__(self, path):
        self.path = path
        self.present = 0
        self.types = collections.Counter()
        self.values = collections.Counter()
        self.too_many_values = False
        self.min_value = None
        self.max_value = None

    def observe(self, value):
        self.present += 1
        kind = _type_of(value)
        self.types[kind] += 1

        # Only scalars are worth counting by value. An object or array as a
        # dictionary key is both meaningless and unbounded.
        if kind in ("string", "int", "double", "boolean", "objectId", "date", "decimal"):
            key = value.isoformat() if kind == "date" else str(value)
            if not self.too_many_values:
                self.values[key] += 1
                if len(self.values) > MAX_DISTINCT * 8:
                    # Stop counting rather than grow without bound. The flag is
                    # reported, so the UI can say "too many to list" instead of
                    # showing an arbitrary twelve and implying they are the lot.
                    self.too_many_values = True
                    self.values.clear()

        if kind in ("int", "double", "decimal", "date"):
            try:
                comparable = float(str(value)) if kind == "decimal" else value
                if self.min_value is None or comparable < self.min_value:
                    self.min_value = comparable
                if self.max_value is None or comparable > self.max_value:
                    self.max_value = comparable
            except (TypeError, ValueError):
                pass

    def report(self, sampled):
        types = [
            {"type": t, "count": n, "percent": round(100.0 * n / self.present, 1)}
            for t, n in self.types.most_common()
        ]
        out = {
            "path": self.path,
            "present": self.present,
            # The number people actually want: how often the field is MISSING.
            # A schema view that only reports what is there cannot answer
            # "which documents have no email address".
            "presence_percent": round(100.0 * self.present / sampled, 1) if sampled else 0.0,
            "missing": sampled - self.present,
            "types": types,
            # More than one type in a field is the single most useful signal this
            # produces. Flagged rather than left for someone to spot.
            "mixed_types": len(types) > 1,
        }
        if self.min_value is not None:
            out["min"] = _render_scalar(self.min_value)
            out["max"] = _render_scalar(self.max_value)
        if self.too_many_values:
            out["distinct"] = None
            out["too_many_distinct"] = True
        elif self.values:
            out["distinct"] = [
                {"value": v, "count": n, "percent": round(100.0 * n / self.present, 1)}
                for v, n in self.values.most_common(MAX_DISTINCT)
            ]
            out["distinct_total"] = len(self.values)
        return out


def _render_scalar(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def _walk(doc, fields, prefix="", depth=0):
    if depth > MAX_DEPTH:
        return
    for key, value in doc.items():
        path = f"{prefix}{key}"
        field = fields.get(path)
        if field is None:
            field = fields[path] = _Field(path)
        field.observe(value)

        if isinstance(value, dict):
            _walk(value, fields, path + ".", depth + 1)
        elif isinstance(value, list):
            # Array ELEMENTS are recorded under `path[]` rather than merged into
            # the array field itself. "tags is an array" and "tags contains
            # strings" are different facts and both matter.
            element_path = path + "[]"
            element = fields.get(element_path)
            if element is None:
                element = fields[element_path] = _Field(element_path)
            for item in value[:100]:      # a 50k-element array is not a schema
                element.observe(item)
                if isinstance(item, dict):
                    _walk(item, fields, element_path + ".", depth + 1)


def analyse(connection, database, collection, *, sample=DEFAULT_SAMPLE,
            filter=None, max_time_ms=30_000):
    """Sample a collection and describe its shape."""
    sample = max(1, min(int(sample), MAX_SAMPLE))

    pipeline = []
    if filter:
        # The filter is applied BEFORE sampling, so "the shape of last month's
        # orders" is answerable. $sample after $match is what Compass does too.
        pipeline.append({"$match": mongo.decode(filter)})
    pipeline.append({"$sample": {"size": sample}})

    def go():
        col = mongo.client_for(connection)[database][collection]
        fields = {}
        seen = 0
        for doc in col.aggregate(pipeline, maxTimeMS=max_time_ms, allowDiskUse=False):
            seen += 1
            _walk(doc, fields)

        try:
            total = col.estimated_document_count()
        except PyMongoError:
            total = None

        reported = [f.report(seen) for f in fields.values()]
        # Most-present first: the fields every document has are the shape, and
        # the sparse ones are the exceptions. Alphabetical would bury both.
        reported.sort(key=lambda f: (-f["present"], f["path"]))

        return {
            "sampled": seen,
            "requested_sample": sample,
            "collection_total": total,
            # Said explicitly. A percentage measured on 1 000 of 40 000 000
            # documents is an estimate, and the UI must be able to say so rather
            # than presenting it as a count.
            "is_estimate": bool(total and seen < total),
            "fields": reported,
        }

    return mongo._guard(go)
