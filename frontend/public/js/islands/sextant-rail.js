/* <sextant-rail> — connections, databases and collections down the left.
 *
 * Loads lazily: databases when a connection is picked, collections when a
 * database is. Listing every collection on every database up front means a
 * `listCollections` per database on a server that might hold hundreds, and the
 * console would sit blank while it finished.
 */
(function () {
  const { signal, html, Tina4Element } = Tina4;
  const S = window.Sextant;

  class SextantRail extends Tina4Element {
    static shadow = false;

    constructor() {
      super();
      this.databases = signal([]);
      this.collections = signal([]);
      this.loadingDbs = signal(false);
      this.loadingCols = signal(false);
      this.failed = signal(null);
      this._lastConnection = null;
      this._lastDatabase = null;
    }

    onMount() {
      this.refresh();
    }

    /* render() re-runs on every signal change, so the fetch cannot live there.
     * This is called from onMount and from the click handlers instead, and it
     * guards against refetching what it already has. */
    async refresh() {
      const conn = S.connectionId();
      if (conn && conn !== this._lastConnection) {
        this._lastConnection = conn;
        this._lastDatabase = null;
        this.collections.set([]);
        this.loadingDbs.set(true);
        const r = await S.api(`/api/${encodeURIComponent(conn)}/databases`);
        this.loadingDbs.set(false);
        if (r.ok) { this.databases.set(r.data.databases || []); this.failed.set(null); }
        else { this.databases.set([]); this.failed.set(r.error); }
      }

      const db = S.database();
      if (conn && db && db !== this._lastDatabase) {
        this._lastDatabase = db;
        this.loadingCols.set(true);
        const r = await S.api(
          `/api/${encodeURIComponent(conn)}/${encodeURIComponent(db)}/collections`);
        this.loadingCols.set(false);
        if (r.ok) { this.collections.set(r.data.collections || []); this.failed.set(null); }
        else { this.collections.set([]); this.failed.set(r.error); }
      }
    }

    pickConnection(id) {
      if (S.connectionId() === id) return;
      S.connectionId.set(id);
      S.database.set(null);
      S.collection.set(null);
      this.refresh();
    }

    pickDatabase(name) {
      if (S.database() === name) return;
      S.database.set(name);
      S.collection.set(null);
      this.refresh();
    }

    pickCollection(name) {
      S.collection.set(name);
      S.tab.set("documents");
    }

    render() {
      const me = S.me();
      const conns = (me && me.connections) || [];
      const activeConn = S.connectionId();
      const activeDb = S.database();
      const activeCol = S.collection();

      return html`
        <aside class="rail">
          <div class="rail-head">
            <span class="mark"></span>
            <h1>Sextant</h1>
          </div>

          <div class="rail-body">
            ${this.failed() ? html`<div class="notice bad" style="margin:10px">${this.failed()}</div>` : ""}

            <h2>Connections</h2>
            ${conns.length === 0
              ? html`<div class="empty">No connections are available to you.</div>`
              : conns.map((c) => html`
                  <div class="tree-item" aria-current=${String(c.id === activeConn)}
                       onclick=${() => this.pickConnection(c.id)}>
                    <span>${c.name}</span>
                    <span class=${"badge " + (c.writable ? "rw" : "ro")}>
                      ${c.writable ? "rw" : "ro"}
                    </span>
                  </div>
                `)}

            ${activeConn ? html`
              <h2>Databases</h2>
              ${this.loadingDbs()
                ? html`<div class="empty">Loading…</div>`
                : this.databases().length === 0
                  ? html`<div class="empty">Nothing this credential can see.</div>`
                  : this.databases().map((d) => html`
                      <div class="tree-item db" aria-current=${String(d.name === activeDb)}
                           onclick=${() => this.pickDatabase(d.name)}>
                        <span>${d.name}</span>
                      </div>
                      ${d.name === activeDb ? html`
                        ${this.loadingCols()
                          ? html`<div class="empty">Loading…</div>`
                          : this.collections().map((c) => html`
                              <div class="tree-item col" aria-current=${String(c.name === activeCol)}
                                   onclick=${() => this.pickCollection(c.name)}>
                                <span>${c.name}</span>
                                <span class="count">
                                  ${c.estimated_count === null ? "?" : c.estimated_count}
                                </span>
                              </div>
                            `)}
                      ` : ""}
                    `)}
            ` : ""}
          </div>
        </aside>
      `;
    }
  }

  customElements.define("sextant-rail", SextantRail);
})();
