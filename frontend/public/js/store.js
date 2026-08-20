/* Shared state and the API client for Sextant.
 *
 * A plain script exposing `window.Sextant`, NOT an ES module. The islands use
 * the `Tina4` global from /js/tina4js.min.js, which tina4-nodejs serves itself —
 * there is no bundler here and no import map, so `import { signal } from ...`
 * would fail at load and the islands would simply never register. That presents
 * as a blank page rather than as an error, which is why it is worth stating.
 *
 * Signals live HERE rather than inside a component because the rail and the
 * workspace must see the same selection. They are created once, at load. Tina4
 * calls render() inside a reactive effect, so a signal created inside render()
 * is rebuilt on every pass — state that appears to reset whenever anything
 * changes, and an effect that never settles.
 */
(function () {
  const { signal } = Tina4;

  const me = signal(null);            // {username, groups, via, connections}
  const connectionId = signal(null);
  const database = signal(null);
  const collection = signal(null);
  const tab = signal("documents");
  const error = signal(null);
  const busy = signal(false);

  function connection() {
    const id = connectionId();
    return (me()?.connections || []).find((c) => c.id === id) || null;
  }

  /* Fetch JSON and always return the same shape. A rejected promise nobody
   * catches becomes an island that renders nothing, with the reason only in the
   * console. */
  async function api(path, options) {
    options = options || {};
    busy.set(true);
    error.set(null);
    try {
      const response = await fetch(path, {
        method: options.method || "GET",
        headers: { "Content-Type": "application/json" },
        body: options.body ? JSON.stringify(options.body) : undefined,
        credentials: "same-origin",
      });

      const text = await response.text();
      let data;
      try {
        data = text ? JSON.parse(text) : {};
      } catch (e) {
        // A non-JSON body from a JSON endpoint means something upstream is
        // answering instead of the backend — a proxy error page, usually.
        // "Unexpected end of JSON input" would send the reader somewhere useless.
        return { ok: false, status: response.status,
                 error: "the server returned something that is not JSON (HTTP " + response.status + ")" };
      }
      if (!response.ok) {
        return { ok: false, status: response.status,
                 error: data.error || ("HTTP " + response.status) };
      }
      return { ok: true, status: response.status, data: data };
    } catch (e) {
      return { ok: false, status: 0, error: "could not reach the server: " + e.message };
    } finally {
      busy.set(false);
    }
  }

  async function loadMe() {
    const r = await api("/api/me");
    if (r.ok) {
      me.set(r.data);
      if (!connectionId() && r.data.connections && r.data.connections.length) {
        connectionId.set(r.data.connections[0].id);
      }
    } else if (r.status !== 401) {
      error.set(r.error);
    }
    return r;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* Render a value as Extended JSON with type hints.
   *
   * The type wrapper is the point, not decoration. `{"$oid": "..."}` shown as a
   * bare string is exactly the confusion that makes an edit match no document:
   * the filter built from it is a string where the database holds an ObjectId,
   * the update reports success, and nothing changes. Seeing `$oid(...)` is how
   * someone knows which they are looking at. */
  function highlight(value, indent) {
    indent = indent || 0;
    const pad = "  ".repeat(indent);
    const padIn = "  ".repeat(indent + 1);

    if (value === null) return '<span class="b">null</span>';
    if (typeof value === "boolean") return '<span class="b">' + value + "</span>";
    if (typeof value === "number") return '<span class="n">' + value + "</span>";
    if (typeof value === "string")
      return '<span class="s">' + escapeHtml(JSON.stringify(value)) + "</span>";

    if (Array.isArray(value)) {
      if (!value.length) return "[]";
      return "[\n" + value.map(function (v) {
        return padIn + highlight(v, indent + 1);
      }).join(",\n") + "\n" + pad + "]";
    }

    if (typeof value === "object") {
      const keys = Object.keys(value);
      if (keys.length === 1 && keys[0].charAt(0) === "$") {
        return '<span class="t">' + escapeHtml(keys[0]) + "(</span>" +
               '<span class="s">' + escapeHtml(String(value[keys[0]])) + "</span>" +
               '<span class="t">)</span>';
      }
      if (!keys.length) return "{}";
      return "{\n" + keys.map(function (k) {
        return padIn + '<span class="k">' + escapeHtml(JSON.stringify(k)) + "</span>: " +
               highlight(value[k], indent + 1);
      }).join(",\n") + "\n" + pad + "}";
    }
    return escapeHtml(String(value));
  }

  function idLabel(doc) {
    const id = doc && doc._id;
    if (id && typeof id === "object" && id.$oid) return id.$oid;
    if (id === undefined) return "(no _id)";
    return typeof id === "object" ? JSON.stringify(id) : String(id);
  }

  /* Parse what the user typed into the filter box.
   *
   * Deliberately strict: an unparseable filter is reported rather than silently
   * treated as {}. Running an empty filter when someone meant to narrow is how a
   * bulk delete hits a whole collection. */
  function parseJson(text, whatItIs) {
    const trimmed = (text || "").trim();
    if (!trimmed) return { ok: true, value: undefined };
    try {
      return { ok: true, value: JSON.parse(trimmed) };
    } catch (e) {
      return { ok: false, error: whatItIs + " is not valid JSON: " + e.message };
    }
  }

  window.Sextant = {
    me: me, connectionId: connectionId, database: database,
    collection: collection, tab: tab, error: error, busy: busy,
    connection: connection, api: api, loadMe: loadMe,
    highlight: highlight, escapeHtml: escapeHtml, idLabel: idLabel,
    parseJson: parseJson,
  };
})();
