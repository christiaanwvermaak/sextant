"""Talking to MongoDB, and getting BSON in and out of a browser intact.

**Extended JSON is not optional here.** A browser speaks JSON, which has no
ObjectId, no Decimal128, no BSON date and no 64-bit integer that survives
`JSON.parse`. Round-tripping documents through plain JSON silently corrupts every
one of those: an `_id` becomes a string, so an update filter built from it
matches nothing, and the edit appears to succeed while changing no document.

`bson.json_util` with the *canonical* representation is the fix, and it is what
Compass itself uses. `{"$oid": "..."}` survives the round trip; `"..."` does not.
"""
import re

from bson import json_util
from bson.json_util import JSONOptions, JSONMode
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Canonical, not relaxed. Relaxed mode renders an ObjectId as a plain string,
# which is friendlier to read and useless to send back.
_OPTS = JSONOptions(json_mode=JSONMode.CANONICAL)

# Databases MongoDB owns. Compass hides these behind a toggle; so do we, rather
# than pretending they are not there.
SYSTEM_DATABASES = {"admin", "local", "config"}


class MongoError(Exception):
    """Something went wrong. Message is safe to show a user."""


_clients = {}


def client_for(connection):
    """One pooled client per connection, built lazily.

    pymongo's MongoClient is already a pool and is thread-safe, so building one
    per request would be both slower and a connection leak.
    """
    if connection.id not in _clients:
        _clients[connection.id] = MongoClient(
            connection.uri,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
            appname="mongo-console",
        )
    return _clients[connection.id]


def encode(value):
    """BSON -> JSON-safe structure a browser can hold and hand back unchanged."""
    return json_util.loads(json_util.dumps(value, json_options=_OPTS))


def decode(value):
    """The browser's structure -> BSON. Inverse of `encode`."""
    if value is None:
        return None
    return json_util.loads(json_util.dumps(value), json_options=_OPTS)


def _guard(fn):
    try:
        return fn()
    except PyMongoError as exc:
        raise MongoError(_redact(str(exc))) from None


def _redact(text):
    """A driver error can quote the connection URI, password and all."""
    return re.sub(r"(mongodb(\+srv)?://)[^@/\s]+@", r"\1<redacted>@", text or "")


def ping(connection):
    def go():
        client_for(connection).admin.command("ping")
        return True
    return _guard(go)


def databases(connection, include_system=False):
    def go():
        client = client_for(connection)
        out = []
        for info in client.list_databases():
            name = info["name"]
            if name in SYSTEM_DATABASES and not include_system:
                continue
            if not connection.allows_database(name):
                continue
            out.append({
                "name": name,
                "size_on_disk": info.get("sizeOnDisk"),
                "empty": info.get("empty", False),
                "system": name in SYSTEM_DATABASES,
            })
        return sorted(out, key=lambda d: d["name"])
    return _guard(go)


def collections(connection, database):
    def go():
        db = client_for(connection)[database]
        out = []
        for name in db.list_collection_names():
            # estimated_document_count uses collection metadata rather than a
            # full scan. On a large collection an exact count is a table scan
            # that can take minutes, which is not what a listing screen should do.
            try:
                count = db[name].estimated_document_count()
            except PyMongoError:
                count = None
            out.append({"name": name, "estimated_count": count})
        return sorted(out, key=lambda c: c["name"])
    return _guard(go)


def find(connection, database, collection, *, filter=None, sort=None,
         projection=None, skip=0, limit=50, max_time_ms=10_000):
    """A page of documents, plus the count the query would return.

    Returns the count separately because "showing 50 of 4" and "showing 50 of
    40 000" are different situations and the UI must be able to say which.
    """
    def go():
        col = client_for(connection)[database][collection]
        f = decode(filter) or {}
        cursor = col.find(f, decode(projection) or None,
                          max_time_ms=max_time_ms)
        if sort:
            cursor = cursor.sort(list(decode(sort).items()))
        cursor = cursor.skip(int(skip)).limit(int(limit))
        docs = [encode(d) for d in cursor]
        try:
            total = col.count_documents(f, maxTimeMS=max_time_ms)
        except PyMongoError:
            # A count that times out must not take the page down with it.
            total = None
        return {"documents": docs, "total": total, "skip": int(skip), "limit": int(limit)}
    return _guard(go)


def aggregate(connection, database, collection, pipeline, *, max_time_ms=10_000,
              limit=200):
    """Run a pipeline. Read-only stages only — see `WRITE_STAGES`."""
    stages = decode(pipeline) or []
    if not isinstance(stages, list):
        raise MongoError("a pipeline must be a list of stages")
    for stage in stages:
        if not isinstance(stage, dict) or len(stage) != 1:
            raise MongoError("each stage must be a single-key object such as {\"$match\": {...}}")
        name = next(iter(stage))
        if name in WRITE_STAGES:
            raise MongoError(
                f"{name} writes to a collection. Aggregations here are read-only; "
                "use the document editor if you mean to change data, so the change "
                "is recorded."
            )

    def go():
        col = client_for(connection)[database][collection]
        cursor = col.aggregate(stages, maxTimeMS=max_time_ms)
        docs = []
        for i, d in enumerate(cursor):
            if i >= limit:
                return {"documents": docs, "truncated": True, "limit": limit}
            docs.append(encode(d))
        return {"documents": docs, "truncated": False, "limit": limit}
    return _guard(go)


# $out and $merge write their results into a collection, which would be a
# mutation arriving through the read path with no pre-image and no audit entry.
WRITE_STAGES = {"$out", "$merge"}


def explain(connection, database, collection, *, filter=None, sort=None):
    def go():
        db = client_for(connection)[database]
        cmd = {"find": collection, "filter": decode(filter) or {}}
        if sort:
            cmd["sort"] = decode(sort)
        return encode(db.command({"explain": cmd, "verbosity": "executionStats"}))
    return _guard(go)


def indexes(connection, database, collection):
    def go():
        col = client_for(connection)[database][collection]
        return [encode(i) for i in col.list_indexes()]
    return _guard(go)


def stats(connection, database, collection=None):
    def go():
        db = client_for(connection)[database]
        if collection:
            return encode(db.command("collStats", collection))
        return encode(db.command("dbStats"))
    return _guard(go)


def one(connection, database, collection, document_id):
    """Fetch a single document by _id, for the pre-image."""
    def go():
        col = client_for(connection)[database][collection]
        return encode(col.find_one({"_id": decode(document_id)}))
    return _guard(go)
