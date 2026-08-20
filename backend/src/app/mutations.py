"""Changing documents, safely enough to point at production.

Every mutation goes through here, and every one of them:

  1. is refused unless the connection is marked `writable` in config;
  2. is refused on a confirm-required connection without an explicit `confirm`;
  3. reads the document FIRST and records it as the pre-image;
  4. is written to the audit log before the caller is told it succeeded.

Step 3 is what makes step 4 worth anything. "Deleted a document" is not a record
you can act on; the document is.
"""
from . import audit as audit_mod
from . import mongo


class Refused(Exception):
    """A deliberate refusal, safe to show. Not a failure."""


def _check(connection, confirm):
    if not connection.writable:
        raise Refused(
            f"'{connection.name}' is configured read-only. "
            "Set `writable: true` on the connection to allow changes."
        )
    if connection.confirm_writes and not confirm:
        raise Refused(
            f"'{connection.name}' requires confirmation for changes. "
            "Re-send with confirm set once you have checked the filter."
        )


def _collection(connection, database, collection):
    return mongo.client_for(connection)[database][collection]


def insert(audit, connection, database, collection, document, *, who,
           confirm=False, request_id=None):
    _check(connection, confirm)
    doc = mongo.decode(document)
    if not isinstance(doc, dict):
        raise Refused("a document must be an object")

    with audit_mod.guard(
        audit, who=who, action="insert", connection=connection.id,
        database=database, collection=collection, request_id=request_id,
    ) as g:
        result = _collection(connection, database, collection).insert_one(doc)
        g.post_image = mongo.encode(doc)
        g.affected = 1
        return {"inserted_id": mongo.encode(result.inserted_id), "affected": 1}


def replace(audit, connection, database, collection, document_id, document, *,
            who, confirm=False, request_id=None):
    """Replace one document, identified by _id.

    Replace rather than update-by-filter on purpose: the editor shows one
    document and saves that document. A filter-based update in the same screen
    would let a mistyped filter rewrite the collection, and the pre-image would
    then be a single document standing in for many.
    """
    _check(connection, confirm)
    col = _collection(connection, database, collection)
    _id = mongo.decode(document_id)

    with audit_mod.guard(
        audit, who=who, action="replace", connection=connection.id,
        database=database, collection=collection,
        query={"_id": mongo.encode(_id)}, request_id=request_id,
    ) as g:
        before = col.find_one({"_id": _id})
        if before is None:
            raise Refused("that document no longer exists — it may have been changed by someone else")
        g.pre_image = mongo.encode(before)

        doc = mongo.decode(document)
        if not isinstance(doc, dict):
            raise Refused("a document must be an object")
        # Keep the _id immutable. Mongo rejects a changed _id anyway, but it
        # rejects it with a driver error rather than something a person can read.
        doc.pop("_id", None)

        result = col.replace_one({"_id": _id}, doc)
        g.post_image = mongo.encode({**doc, "_id": _id})
        g.affected = result.modified_count
        return {"affected": result.modified_count, "matched": result.matched_count}


def delete(audit, connection, database, collection, document_id, *, who,
           confirm=False, request_id=None):
    _check(connection, confirm)
    col = _collection(connection, database, collection)
    _id = mongo.decode(document_id)

    with audit_mod.guard(
        audit, who=who, action="delete", connection=connection.id,
        database=database, collection=collection,
        query={"_id": mongo.encode(_id)}, request_id=request_id,
    ) as g:
        before = col.find_one({"_id": _id})
        if before is None:
            raise Refused("that document no longer exists")
        # Recorded BEFORE the delete. Recording it afterwards would mean a crash
        # between the two leaves a deleted document and no copy of it.
        g.pre_image = mongo.encode(before)
        result = col.delete_one({"_id": _id})
        g.affected = result.deleted_count
        return {"affected": result.deleted_count}


def delete_many(audit, connection, database, collection, filter, *, who,
                confirm=False, request_id=None, max_documents=100):
    """Bulk delete by filter, capped and fully pre-imaged.

    The cap is not arbitrary: this records every document it removes, and a
    delete of ten thousand documents would produce an audit entry nobody can use
    and a memory spike while building it. Above the cap the honest answer is that
    this is not the tool — use a migration.
    """
    _check(connection, confirm)
    col = _collection(connection, database, collection)
    f = mongo.decode(filter)
    if not isinstance(f, dict) or not f:
        raise Refused(
            "a bulk delete needs a filter. An empty filter would remove every "
            "document in the collection."
        )

    with audit_mod.guard(
        audit, who=who, action="delete_many", connection=connection.id,
        database=database, collection=collection,
        query=mongo.encode(f), request_id=request_id,
    ) as g:
        doomed = list(col.find(f).limit(max_documents + 1))
        if len(doomed) > max_documents:
            raise Refused(
                f"that filter matches more than {max_documents} documents. "
                "This tool records every document it deletes so the change can be "
                "undone, which is not practical at that size — use a migration."
            )
        if not doomed:
            raise Refused("that filter matches no documents")
        g.pre_image = [mongo.encode(d) for d in doomed]
        result = col.delete_many({"_id": {"$in": [d["_id"] for d in doomed]}})
        g.affected = result.deleted_count
        return {"affected": result.deleted_count}


def undo(audit, connection, database, collection, entry, *, who, request_id=None):
    """Put back what an audit entry recorded.

    Deliberately narrow: it restores documents from a pre-image and nothing else.
    It is not a general time machine, and it will happily fail if the document
    has changed again since — which is the correct behaviour, because silently
    overwriting someone else's later edit is a worse outcome than refusing.
    """
    _check(connection, confirm=True)
    pre = entry.get("pre_image")
    if pre is None:
        raise Refused("that entry has no pre-image, so there is nothing to put back")
    docs = pre if isinstance(pre, list) else [pre]
    col = _collection(connection, database, collection)

    with audit_mod.guard(
        audit, who=who, action="undo", connection=connection.id,
        database=database, collection=collection,
        query={"undoes": entry.get("at")}, request_id=request_id,
    ) as g:
        restored = 0
        for d in docs:
            doc = mongo.decode(d)
            col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            restored += 1
        g.post_image = docs
        g.affected = restored
        return {"affected": restored}
