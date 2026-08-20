"""Every change, with what it replaced.

A browser-based console that can edit production needs to answer three questions
after the fact: who changed it, what did it look like before, and can it be put
back. Compass answers none of these — it is a desktop app talking straight to the
database, and the only record is whatever the server happens to log.

**The pre-image is the point.** An audit line saying "Wimpie updated a document"
is nearly useless at 2am. An audit line carrying the document as it was is an
undo.

Written as append-only JSON Lines rather than into MongoDB itself, deliberately:
an audit trail stored in the system being audited is worth very little, and it
disappears at exactly the moment it matters — when someone drops the wrong
collection.
"""
import datetime
import json
import os
import threading

_lock = threading.Lock()

# Refuse to record a pre-image larger than this rather than silently truncating
# it, because a truncated pre-image is an undo that does not work.
MAX_PREIMAGE_BYTES = 1_000_000


class AuditError(Exception):
    """The change was NOT applied because it could not be recorded."""


def _default(o):
    """BSON types that json does not know. ObjectId and datetime are the common
    pair; anything else is stringified rather than dropped."""
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)


class Audit:
    def __init__(self, path):
        self.path = path

    def _write(self, entry):
        line = json.dumps(entry, default=_default, ensure_ascii=False)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with _lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                # fsync so a pod killed mid-write still has the record. A write
                # that survives in the database but not in the audit log is the
                # one case this file exists to prevent.
                os.fsync(fh.fileno())

    def record(self, *, who, action, connection, database, collection,
               query=None, pre_image=None, post_image=None, affected=None,
               request_id=None):
        """Append one entry. Raises rather than returning False.

        The caller MUST treat a raise as "do not proceed" — see `guard()`. A
        change applied without a record is precisely the thing this prevents.
        """
        if pre_image is not None:
            size = len(json.dumps(pre_image, default=_default))
            if size > MAX_PREIMAGE_BYTES:
                raise AuditError(
                    f"the document is {size} bytes and the pre-image limit is "
                    f"{MAX_PREIMAGE_BYTES}. Refusing the change rather than "
                    "recording an incomplete copy that cannot be used to undo it."
                )
        entry = {
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "who": who,
            "action": action,
            "connection": connection,
            "database": database,
            "collection": collection,
            "query": query,
            "affected": affected,
            "pre_image": pre_image,
            "post_image": post_image,
            "request_id": request_id,
        }
        try:
            self._write(entry)
        except OSError as exc:
            raise AuditError(f"could not write the audit record: {exc}") from None

    def tail(self, limit=200, connection=None):
        """Most recent entries, newest first. For the Activity screen."""
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if connection and entry.get("connection") != connection:
                    continue
                out.append(entry)
        return list(reversed(out[-limit:]))


class guard:
    """Context manager: record first, then apply. Never the other way round.

        with audit.guard(...) as g:
            g.pre_image = collection.find_one(query)
            result = collection.update_one(query, update)
            g.affected = result.modified_count

    The entry is written on a clean exit. If the body raises, a FAILED entry is
    written instead, so an attempted-but-broken change is still visible — a
    silent failure and a silent success look identical afterwards otherwise.
    """

    def __init__(self, audit, **fields):
        self.audit = audit
        self.fields = fields
        self.pre_image = None
        self.post_image = None
        self.affected = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        action = self.fields.get("action", "?")
        if exc_type is not None:
            self.fields["action"] = f"{action}:FAILED"
        try:
            self.audit.record(
                pre_image=self.pre_image,
                post_image=self.post_image,
                affected=self.affected,
                **self.fields,
            )
        except AuditError:
            # If the change already happened we cannot un-happen it, but we can
            # refuse to stay quiet. Re-raise so the caller returns an error and
            # the operator knows the record is missing.
            raise
        return False
