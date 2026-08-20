"""The HTTP surface.

Every route resolves the caller first and the connection second, and **never
trusts a connection id from the request without checking it is one the caller may
see**. That check is `identity.connection_for`, and it is the only thing between
someone who can sign in and every database in the config.

Tina4 note, and it is not obvious: `auth_required` defaults to
`method not in ("GET", "HEAD", "OPTIONS", "ANY")`, so every POST and DELETE is
guarded by Tina4's own bearer scheme keyed on TINA4_SECRET — which has nothing to
do with our OIDC token and which our token can never satisfy. The request never
reaches the handler and the caller gets a bare "Unauthorized". `auth_required=False`
opts out of THAT scheme, not out of authentication: `_caller` below is the real
check and runs on every route. This cost an afternoon on another project.
"""
from tina4_python.core.router import delete, get, post

from src.app import config as config_mod
from src.app import identity, mongo, mutations
from src.app.audit import Audit, AuditError


def _cfg():
    return config_mod.load()


def _audit():
    return Audit(_cfg().audit_path)


def _caller(request, response):
    """(who, config, None) or (None, None, error_response)."""
    cfg = _cfg()
    try:
        who = identity.identify(cfg, request)
    except identity.AuthError as exc:
        return None, None, response({"error": str(exc)}, 401)
    return who, cfg, None


def _target(request, response, *, write=False):
    """Resolve caller + connection + database + collection from the request."""
    who, cfg, error = _caller(request, response)
    if error:
        return None, None, error

    params = getattr(request, "params", None) or {}
    try:
        query = dict(request.query or {})
    except (AttributeError, TypeError):
        query = {}

    connection_id = params.get("connection") or query.get("connection", "")
    try:
        conn = identity.connection_for(cfg, who, connection_id)
    except identity.AuthError as exc:
        # 403 rather than 404: the caller is authenticated and simply not
        # entitled. Hiding it would make a permission problem look like a bug.
        return None, None, response({"error": str(exc)}, 403)

    database = params.get("database") or query.get("database", "")
    if database and not conn.allows_database(database):
        return None, None, response(
            {"error": f"'{database}' is not one of the databases this connection exposes"}, 403)

    return who, {"conn": conn, "database": database,
                 "collection": params.get("collection") or query.get("collection", ""),
                 "cfg": cfg}, None


def _body(request):
    body = getattr(request, "body", None) or {}
    return body if isinstance(body, dict) else {}


# ── who am I, what can I reach ─────────────────────────────────────────────

@get("/api/me")
async def me(request, response):
    who, cfg, error = _caller(request, response)
    if error:
        return error
    return response({
        "username": who["username"],
        "groups": who["groups"],
        "via": who["via"],
        "connections": identity.visible_connections(cfg, who),
    }, 200)


@post("/api/sign-in", auth_required=False)
async def sign_in(request, response):
    """Local break-glass sign-in. OIDC callers send a bearer token instead."""
    cfg = _cfg()
    body = _body(request)
    try:
        who = identity.sign_in_local(cfg, body.get("username"), body.get("password"))
        token = identity.issue_local_token(cfg.auth, who)
    except identity.AuthError as exc:
        return response({"error": str(exc)}, 401)
    # The token goes back to the FRONTEND, which stores it in a HttpOnly session
    # cookie and sends it as a bearer. It is never handed to page JavaScript --
    # anything that can read it can act as this user for eight hours.
    return response({"username": who["username"], "via": who["via"], "token": token}, 200)


# ── structure ──────────────────────────────────────────────────────────────

@get("/api/{connection}/databases")
async def databases(request, response):
    who, t, error = _target(request, response)
    if error:
        return error
    try:
        return response({"databases": mongo.databases(t["conn"])}, 200)
    except mongo.MongoError as exc:
        return response({"error": str(exc)}, 502)


@get("/api/{connection}/{database}/collections")
async def collections(request, response):
    who, t, error = _target(request, response)
    if error:
        return error
    try:
        return response({"collections": mongo.collections(t["conn"], t["database"])}, 200)
    except mongo.MongoError as exc:
        return response({"error": str(exc)}, 502)


@get("/api/{connection}/{database}/{collection}/indexes")
async def indexes(request, response):
    who, t, error = _target(request, response)
    if error:
        return error
    try:
        return response({"indexes": mongo.indexes(t["conn"], t["database"], t["collection"])}, 200)
    except mongo.MongoError as exc:
        return response({"error": str(exc)}, 502)


@get("/api/{connection}/{database}/{collection}/stats")
async def collection_stats(request, response):
    who, t, error = _target(request, response)
    if error:
        return error
    try:
        return response(mongo.stats(t["conn"], t["database"], t["collection"]), 200)
    except mongo.MongoError as exc:
        return response({"error": str(exc)}, 502)


# ── reading ────────────────────────────────────────────────────────────────
#
# find is a POST despite being a read: a Mongo filter is a JSON document, and
# putting one in a query string means URL-encoding nested BSON and losing it to
# length limits on a big $in. It also keeps filters out of access logs.

@post("/api/{connection}/{database}/{collection}/find", auth_required=False)
async def find(request, response):
    who, t, error = _target(request, response)
    if error:
        return error
    body = _body(request)
    cfg = t["cfg"]
    try:
        return response(mongo.find(
            t["conn"], t["database"], t["collection"],
            filter=body.get("filter"), sort=body.get("sort"),
            projection=body.get("projection"),
            skip=body.get("skip", 0),
            limit=min(int(body.get("limit", 50)), cfg.max_documents),
            max_time_ms=cfg.max_time_ms,
        ), 200)
    except mongo.MongoError as exc:
        return response({"error": str(exc)}, 400)


@post("/api/{connection}/{database}/{collection}/aggregate", auth_required=False)
async def aggregate(request, response):
    who, t, error = _target(request, response)
    if error:
        return error
    cfg = t["cfg"]
    try:
        return response(mongo.aggregate(
            t["conn"], t["database"], t["collection"],
            _body(request).get("pipeline"),
            max_time_ms=cfg.max_time_ms, limit=cfg.max_documents,
        ), 200)
    except mongo.MongoError as exc:
        return response({"error": str(exc)}, 400)


@post("/api/{connection}/{database}/{collection}/explain", auth_required=False)
async def explain(request, response):
    who, t, error = _target(request, response)
    if error:
        return error
    body = _body(request)
    try:
        return response(mongo.explain(t["conn"], t["database"], t["collection"],
                                      filter=body.get("filter"), sort=body.get("sort")), 200)
    except mongo.MongoError as exc:
        return response({"error": str(exc)}, 400)


# ── writing ────────────────────────────────────────────────────────────────
#
# Each of these goes through `mutations`, which refuses a read-only connection,
# refuses an unconfirmed change where the connection demands one, and records the
# pre-image BEFORE the change. None of that logic lives here, so a new route
# cannot accidentally skip it.

def _write(fn, request, response):
    who, t, error = _target(request, response, write=True)
    if error:
        return error
    body = _body(request)
    try:
        result = fn(who, t, body)
    except mutations.Refused as exc:
        return response({"error": str(exc)}, 403)
    except AuditError as exc:
        # The change may or may not have landed, but it was not recorded. Say so
        # rather than reporting a clean success.
        return response({"error": f"not recorded, so treated as failed: {exc}"}, 500)
    except mongo.MongoError as exc:
        return response({"error": str(exc)}, 400)
    return response(result, 200)


@post("/api/{connection}/{database}/{collection}/documents", auth_required=False)
async def insert_document(request, response):
    return _write(lambda who, t, body: mutations.insert(
        _audit(), t["conn"], t["database"], t["collection"], body.get("document"),
        who=who["username"], confirm=bool(body.get("confirm")),
    ), request, response)


@post("/api/{connection}/{database}/{collection}/documents/replace", auth_required=False)
async def replace_document(request, response):
    return _write(lambda who, t, body: mutations.replace(
        _audit(), t["conn"], t["database"], t["collection"],
        body.get("id"), body.get("document"),
        who=who["username"], confirm=bool(body.get("confirm")),
    ), request, response)


@post("/api/{connection}/{database}/{collection}/documents/delete", auth_required=False)
async def delete_document(request, response):
    return _write(lambda who, t, body: mutations.delete(
        _audit(), t["conn"], t["database"], t["collection"], body.get("id"),
        who=who["username"], confirm=bool(body.get("confirm")),
    ), request, response)


@post("/api/{connection}/{database}/{collection}/documents/delete-many", auth_required=False)
async def delete_many_documents(request, response):
    return _write(lambda who, t, body: mutations.delete_many(
        _audit(), t["conn"], t["database"], t["collection"], body.get("filter"),
        who=who["username"], confirm=bool(body.get("confirm")),
    ), request, response)


# ── the record ─────────────────────────────────────────────────────────────

@get("/api/{connection}/activity")
async def activity(request, response):
    """What has been changed through this console, newest first."""
    who, t, error = _target(request, response)
    if error:
        return error
    entries = _audit().tail(limit=200, connection=t["conn"].id)
    return response({
        "entries": entries,
        # Said plainly rather than left to be inferred from an empty table.
        "empty_means": None if entries else
            "Nothing has been changed through this console yet.",
    }, 200)


@post("/api/{connection}/{database}/{collection}/undo", auth_required=False)
async def undo(request, response):
    """Put back what an audit entry recorded. Needs the entry itself, so the
    caller has to have looked at what they are restoring."""
    return _write(lambda who, t, body: mutations.undo(
        _audit(), t["conn"], t["database"], t["collection"], body.get("entry") or {},
        who=who["username"],
    ), request, response)
