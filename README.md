# Sextant

A MongoDB console that runs in a browser, with an audit trail and an undo.

MongoDB Compass is a desktop application. That is fine until the database you
need to look at is inside a Kubernetes cluster, or you are not at your own
machine, or you want more than one person to be able to see what someone else
changed. This is the same job, served over HTTP.

**Why "Sextant".** A compass tells you which way you are pointing. A sextant
fixes where you actually are, and it is what you reach for when it matters. It is
also an instrument of record -- you take a sighting and you write it down. That is
this tool: every change is measured against what was there before, and written
down.

**Nothing about any particular estate is compiled in.** Point it at a MongoDB and
it shows the databases and collections that the credential you gave it can see.

## What it does

- Browse databases, collections and documents
- Query with a real filter, projection, sort, skip and limit
- Aggregation pipelines, read-only stages
- Explain plans and index listings
- Edit, insert and delete documents — **if** the connection is configured to allow it
- **Every change is recorded with the document as it was**, and can be put back

## What it deliberately does not do

- **Aggregations cannot write.** `$out` and `$merge` are refused: they are a
  mutation arriving through the read path, with no pre-image and no audit entry.
- **No unbounded bulk delete.** A filter matching more than the configured cap is
  refused, because this tool records every document it removes so the change can
  be undone, and that is not practical at scale. Use a migration.
- **No schema-inference charts.** Compass spends a lot of its code there. If you
  want it, open an issue — it was left out on purpose rather than half-built.

## Safety, since it can write to production

Four things, in order:

1. **Connections are read-only unless the config says otherwise.** Somebody will
   point this at production before they finish reading this file.
2. **Writable connections require an explicit confirmation** per change by
   default, which the UI cannot send by accident.
3. **The document is read and recorded before it is changed.** "Deleted a
   document" is not a record anyone can act on at 2am; the document is.
4. **If the audit record cannot be written, the caller is told the change
   failed.** A change that survives in the database but not in the log is the one
   case this exists to prevent.

The audit log is append-only JSON Lines on disk, **not** in the MongoDB being
audited — an audit trail stored inside the system it audits disappears at exactly
the moment it matters.

## Sign-in

Two doors, on purpose:

- **OIDC** against a provider you already run, for everyday use.
- **A local break-glass credential**, for when that provider is itself the
  outage. A database console you cannot open during an identity incident is one
  you cannot use on the day you need it.

Starting with neither configured is refused rather than quietly serving an open
console.

## Configuration

One YAML file. `${VAR}` is substituted from the environment, so a connection
string with a password comes from a secret rather than from the file:

```yaml
connections:
  - id: staging
    name: Staging
    uri: mongodb://console:${STAGING_PASSWORD}@mongo-staging:27017/?authSource=admin
    writable: true
    confirm_writes: false          # staging is where you try things

  - id: production
    name: Production
    uri: mongodb://console:${PROD_PASSWORD}@mongo:27017/?authSource=admin
    writable: true
    confirm_writes: true           # every change needs confirming
    allowed_groups: [db-admins]    # optional: OIDC groups that may use this
    databases: [orders, customers] # optional: restrict; empty means all

auth:
  oidc:
    issuer: https://auth.example.com/realms/internal
    client_id: sextant
    groups_claim: groups
    required_groups: [db-users]
  local:
    username: operator
    password_env: SEXTANT_PASSWORD

audit:
  path: /data/audit.log

limits:
  max_documents: 200
  max_time_ms: 10000
```

A missing `${VAR}` fails at boot, not at connect time — otherwise a half-built
connection string surfaces as an authentication failure hours later against the
wrong database.

## Running it

```bash
docker compose up --build
```

Or directly:

```bash
cd backend && pip install -r requirements.txt
cd ../frontend && npm install && npm run build
SEXTANT_CONFIG=./examples/sextant.yml python backend/app.py
```

## Built with

| | |
|---|---|
| **tina4-python 3.13** | routing, templating, sessions |
| **Tina4JS** | the frontend: signals and web components, no build-time framework |
| **pymongo** | with `bson.json_util` in canonical mode |

### Why canonical Extended JSON matters

A browser speaks JSON, which has no ObjectId, no Decimal128, no BSON date and no
64-bit integer that survives `JSON.parse`. Round-tripping documents through plain
JSON corrupts all of them: an `_id` becomes a string, an update filter built from
it matches nothing, and **the edit appears to succeed while changing no
document**. Canonical Extended JSON (`{"$oid": "..."}`) survives the trip; relaxed
mode does not.

## Tests

```bash
cd backend && python -m pytest tests/ -q
```

The tests cover refusals and the audit contract rather than MongoDB itself — a
test that needs a live database is a test that does not get run.

## Licence

Apache 2.0 -- see [LICENSE](LICENSE).

Not affiliated with or endorsed by MongoDB, Inc. "Compass" and "MongoDB" are
their trademarks; this is an independent tool that does a similar job.
