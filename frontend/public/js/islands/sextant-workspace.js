/* <sextant-workspace> — the query bar, the documents, and everything that
 * changes them.
 *
 * The query and editor textareas are deliberately UNCONTROLLED: they are read
 * from the DOM when a button is pressed rather than bound to a signal. Binding
 * `value=` inside a reactive render rewrites the field on every keystroke and
 * throws the caret to the end, which makes editing a JSON document impossible.
 */
(function () {
  const { signal, html, Tina4Element } = Tina4;
  const S = window.Sextant;

  /* Compass's tab order, deliberately. Someone who uses Compass daily should
   * not have to hunt for Schema because we sorted them differently. */
  const TABS = [
    ["documents", "Documents"],
    ["aggregate", "Aggregations"],
    ["schema", "Schema"],
    ["explain", "Explain"],
    ["indexes", "Indexes"],
    ["activity", "Activity"],
  ];

  /* Compass shows documents three ways and remembers which you chose. The list
   * view is the default there and here. */
  const VIEWS = [["list", "List"], ["json", "JSON"], ["table", "Table"]];

  class SextantWorkspace extends Tina4Element {
    static shadow = false;

    constructor() {
      super();
      this.documents = signal([]);
      this.total = signal(null);
      this.skip = signal(0);
      this.limit = signal(20);
      this.failed = signal(null);
      this.note = signal(null);
      this.loading = signal(false);
      this.view = signal("list");      // list | json | table, as Compass
      this.editing = signal(null);     // {doc, json}
      this.confirming = signal(null);  // {title, body, danger, run}
      this.indexes = signal([]);
      this.explainOut = signal(null);
      this.activity = signal([]);
      this._lastKey = null;
    }

    onMount() { this.maybeLoad(); }

    key() {
      return [S.connectionId(), S.database(), S.collection(), S.tab()].join("|");
    }

    /* Called from handlers, never from render(). render() re-runs on every
     * signal change, so a fetch there would loop. */
    async maybeLoad(force) {
      const k = this.key();
      if (!force && k === this._lastKey) return;
      this._lastKey = k;
      const tab = S.tab();
      if (!S.connectionId()) return;
      if (tab === "activity") return this.loadActivity();
      if (!S.database() || !S.collection()) return;
      if (tab === "schema") return;   // the island loads itself
      if (tab === "documents") return this.runFind();
      if (tab === "indexes") return this.loadIndexes();
    }

    base() {
      return `/api/${encodeURIComponent(S.connectionId())}` +
             `/${encodeURIComponent(S.database())}` +
             `/${encodeURIComponent(S.collection())}`;
    }

    readQuery() {
      const q = (name) => {
        const el = this.querySelector(`[data-q="${name}"]`);
        return el ? el.value : "";
      };
      const filter = S.parseJson(q("filter"), "the filter");
      const sort = S.parseJson(q("sort"), "the sort");
      const projection = S.parseJson(q("projection"), "the projection");
      for (const p of [filter, sort, projection]) {
        if (!p.ok) return { ok: false, error: p.error };
      }
      return { ok: true, filter: filter.value, sort: sort.value, projection: projection.value };
    }

    async runFind(skip) {
      const parsed = this.readQuery();
      if (!parsed.ok) { this.failed.set(parsed.error); return; }
      if (typeof skip === "number") this.skip.set(skip);

      this.loading.set(true);
      const r = await S.api(this.base() + "/find", {
        method: "POST",
        body: {
          filter: parsed.filter, sort: parsed.sort, projection: parsed.projection,
          skip: this.skip(), limit: this.limit(),
        },
      });
      this.loading.set(false);
      if (!r.ok) { this.failed.set(r.error); this.documents.set([]); return; }
      this.failed.set(null);
      this.documents.set(r.data.documents || []);
      this.total.set(r.data.total);
    }

    async runAggregate() {
      const el = this.querySelector('[data-q="pipeline"]');
      const parsed = S.parseJson(el ? el.value : "", "the pipeline");
      if (!parsed.ok) { this.failed.set(parsed.error); return; }
      this.loading.set(true);
      const r = await S.api(this.base() + "/aggregate", {
        method: "POST", body: { pipeline: parsed.value || [] },
      });
      this.loading.set(false);
      if (!r.ok) { this.failed.set(r.error); this.documents.set([]); return; }
      this.failed.set(null);
      this.documents.set(r.data.documents || []);
      this.total.set(null);
      this.note.set(r.data.truncated ? `Showing the first ${r.data.limit} results.` : null);
    }

    async runExplain() {
      const parsed = this.readQuery();
      if (!parsed.ok) { this.failed.set(parsed.error); return; }
      this.loading.set(true);
      const r = await S.api(this.base() + "/explain", {
        method: "POST", body: { filter: parsed.filter, sort: parsed.sort },
      });
      this.loading.set(false);
      if (!r.ok) { this.failed.set(r.error); return; }
      this.failed.set(null);
      this.explainOut.set(r.data);
    }

    async loadIndexes() {
      const r = await S.api(this.base() + "/indexes");
      if (r.ok) { this.indexes.set(r.data.indexes || []); this.failed.set(null); }
      else this.failed.set(r.error);
    }

    async loadActivity() {
      const r = await S.api(`/api/${encodeURIComponent(S.connectionId())}/activity`);
      if (r.ok) { this.activity.set(r.data.entries || []); this.failed.set(null); }
      else this.failed.set(r.error);
    }

    // ── writing ────────────────────────────────────────────────────────────

    /* Every write goes through here so that the confirmation, the error
     * handling and the reload cannot be forgotten by a new call site. */
    async write(path, body, describe) {
      const conn = S.connection();
      const run = async () => {
        this.confirming.set(null);
        const r = await S.api(path, { method: "POST", body: Object.assign({}, body, { confirm: true }) });
        if (!r.ok) { this.failed.set(r.error); return; }
        this.failed.set(null);
        this.note.set(describe.done(r.data));
        this.editing.set(null);
        await this.runFind();
      };

      if (conn && conn.confirm_writes) {
        this.confirming.set({ title: describe.title, body: describe.body, danger: describe.danger, run });
      } else {
        await run();
      }
    }

    edit(doc) {
      this.editing.set({ doc: doc, json: JSON.stringify(doc, null, 2) });
    }

    saveEdit() {
      const el = this.querySelector('[data-q="editor"]');
      const parsed = S.parseJson(el ? el.value : "", "the document");
      if (!parsed.ok) { this.failed.set(parsed.error); return; }
      const original = this.editing().doc;
      this.write(this.base() + "/documents/replace",
        { id: original._id, document: parsed.value },
        {
          title: "Replace this document?",
          body: `_id ${S.idLabel(original)} in ${S.database()}.${S.collection()}`,
          danger: false,
          done: (d) => `Replaced ${d.affected} document.`,
        });
    }

    removeDoc(doc) {
      this.write(this.base() + "/documents/delete",
        { id: doc._id },
        {
          title: "Delete this document?",
          body: `_id ${S.idLabel(doc)} in ${S.database()}.${S.collection()}. ` +
                `A copy is recorded first, so it can be put back from Activity.`,
          danger: true,
          done: (d) => `Deleted ${d.affected} document. It can be restored from Activity.`,
        });
    }

    insertDoc() {
      this.editing.set({ doc: null, json: "{\n  \n}" });
    }

    saveInsert() {
      const el = this.querySelector('[data-q="editor"]');
      const parsed = S.parseJson(el ? el.value : "", "the document");
      if (!parsed.ok) { this.failed.set(parsed.error); return; }
      this.write(this.base() + "/documents",
        { document: parsed.value },
        {
          title: "Insert this document?",
          body: `Into ${S.database()}.${S.collection()}`,
          danger: false,
          done: () => "Inserted.",
        });
    }

    undo(entry) {
      this.write(
        `/api/${encodeURIComponent(entry.connection)}/${encodeURIComponent(entry.database)}` +
        `/${encodeURIComponent(entry.collection)}/undo`,
        { entry: entry },
        {
          title: "Put this back?",
          body: `Restores what ${entry.who} ${entry.action}d at ${entry.at}. ` +
                `If the document has changed since, this overwrites that change.`,
          danger: true,
          done: (d) => `Restored ${d.affected}.`,
        });
    }

    // ── rendering ──────────────────────────────────────────────────────────

    renderQueryBar() {
      return html`
        <div class="querybar">
          <div class="row">
            <div class="field">
              <label>Filter</label>
              <textarea data-q="filter" rows="2" spellcheck="false"
                        placeholder='{ "status": "active" }'></textarea>
            </div>
            <div class="field">
              <label>Sort</label>
              <textarea data-q="sort" rows="2" spellcheck="false"
                        placeholder='{ "createdAt": -1 }'></textarea>
            </div>
            <div class="field">
              <label>Projection</label>
              <textarea data-q="projection" rows="2" spellcheck="false"
                        placeholder='{ "name": 1 }'></textarea>
            </div>
          </div>
          <div class="row" style="margin-top:8px; align-items:center">
            <button class="btn primary" onclick=${() => this.runFind(0)}>Run</button>
            ${S.connection() && S.connection().writable
              ? html`<button class="btn" onclick=${() => this.insertDoc()}>Insert document</button>`
              : ""}
            <span class="spacer" style="margin-left:auto"></span>
            <span class="viewtoggle">
              ${VIEWS.map(([id, label]) => html`
                <button class="vbtn" aria-selected=${String(this.view() === id)}
                        onclick=${() => this.view.set(id)}>${label}</button>
              `)}
            </span>
            <button class="btn" disabled=${this.skip() === 0}
                    onclick=${() => this.runFind(Math.max(0, this.skip() - this.limit()))}>Previous</button>
            <button class="btn" onclick=${() => this.runFind(this.skip() + this.limit())}>Next</button>
          </div>
        </div>
      `;
    }

    /* Compass's three views. The list view is per-document cards; JSON is one
     * continuous array you can copy; the table view flattens documents onto
     * shared columns, which is the only way to compare a page of them at a
     * glance and is why Compass has it. */
    renderDocuments() {
      const docs = this.documents();
      const writable = S.connection() && S.connection().writable;
      if (this.loading()) return html`<div class="empty">Running…</div>`;
      if (!docs.length) return html`<div class="empty">No documents matched.</div>`;

      const view = this.view();
      if (view === "json") return this.renderJsonView(docs);
      if (view === "table") return this.renderTableView(docs, writable);
      return this.renderListView(docs, writable);
    }

    renderListView(docs, writable) {
      return html`
        <div>
          ${docs.map((doc) => html`
            <div class="doc">
              <div class="doc-head">
                <span class="id">${S.idLabel(doc)}</span>
                <span class="actions">
                  ${writable ? html`
                    <button class="btn" onclick=${() => this.edit(doc)}>Edit</button>
                    <button class="btn danger" onclick=${() => this.removeDoc(doc)}>Delete</button>
                  ` : ""}
                </span>
              </div>
              <pre innerHTML=${S.highlight(doc)}></pre>
            </div>
          `)}
        </div>
      `;
    }

    renderJsonView(docs) {
      /* One array, exactly as a shell would print it — so it can be copied
       * straight into a script. Type wrappers are kept: `{"$oid": "..."}` is
       * what makes it paste back correctly. */
      return html`
        <div class="doc">
          <div class="doc-head">
            <span>${docs.length} document${docs.length === 1 ? "" : "s"}</span>
            <span class="actions">
              <button class="btn" onclick=${(e) => this.copyJson(e, docs)}>Copy</button>
            </span>
          </div>
          <pre innerHTML=${S.highlight(docs)}></pre>
        </div>
      `;
    }

    async copyJson(event, docs) {
      const button = event.target;
      try {
        await navigator.clipboard.writeText(JSON.stringify(docs, null, 2));
        button.textContent = "Copied";
      } catch (e) {
        // Clipboard access is denied in plenty of contexts. Say so rather than
        // leaving a button that silently does nothing.
        button.textContent = "Blocked";
      }
      setTimeout(() => { button.textContent = "Copy"; }, 1500);
    }

    renderTableView(docs, writable) {
      /* Columns are the union of every top-level key across the page, in
       * first-seen order. Union rather than the first document's keys: in a
       * document store the second document routinely has a field the first does
       * not, and showing only the first document's shape hides exactly that. */
      const columns = [];
      for (const doc of docs) {
        for (const key of Object.keys(doc)) {
          if (columns.indexOf(key) === -1) columns.push(key);
        }
      }

      return html`
        <div class="scroll-x">
          <table class="grid">
            <thead>
              <tr>
                ${columns.map((c) => html`<th>${c}</th>`)}
                ${writable ? html`<th></th>` : ""}
              </tr>
            </thead>
            <tbody>
              ${docs.map((doc) => html`
                <tr>
                  ${columns.map((c) => html`
                    <td class=${doc[c] === undefined ? "absent" : ""}>
                      ${doc[c] === undefined
                        ? html`<span class="absent-mark">—</span>`
                        : html`<code innerHTML=${S.highlight(doc[c])}></code>`}
                    </td>
                  `)}
                  ${writable ? html`
                    <td class="row-actions">
                      <button class="btn" onclick=${() => this.edit(doc)}>Edit</button>
                      <button class="btn danger" onclick=${() => this.removeDoc(doc)}>Delete</button>
                    </td>
                  ` : ""}
                </tr>
              `)}
            </tbody>
          </table>
        </div>
      `;
    }

    renderActivity() {
      const rows = this.activity();
      if (!rows.length) {
        return html`<div class="empty">Nothing has been changed through this console yet.</div>`;
      }
      return html`
        <div class="scroll-x">
          <table class="log">
            <thead>
              <tr><th>When</th><th>Who</th><th>Action</th><th>Where</th>
                  <th>Affected</th><th></th></tr>
            </thead>
            <tbody>
              ${rows.map((e) => html`
                <tr>
                  <td>${e.at}</td>
                  <td>${e.who}</td>
                  <td class=${"act " + String(e.action).replace(":FAILED", "")}>
                    ${String(e.action).indexOf("FAILED") >= 0
                      ? html`<span class="failed">${e.action}</span>` : e.action}
                  </td>
                  <td>${e.database}.${e.collection}</td>
                  <td>${e.affected === null ? "" : e.affected}</td>
                  <td>
                    ${e.pre_image
                      ? html`<button class="btn" onclick=${() => this.undo(e)}>Put back</button>`
                      : ""}
                  </td>
                </tr>
              `)}
            </tbody>
          </table>
        </div>
      `;
    }

    renderPane() {
      const tab = S.tab();
      if (tab === "documents") {
        return html`${this.renderQueryBar()}${this.renderDocuments()}`;
      }
      if (tab === "aggregate") {
        return html`
          <div class="querybar">
            <label>Pipeline</label>
            <textarea data-q="pipeline" rows="8" spellcheck="false"
                      placeholder='[ { "$match": { } }, { "$limit": 20 } ]'></textarea>
            <div class="row" style="margin-top:8px">
              <button class="btn primary" onclick=${() => this.runAggregate()}>Run</button>
              <span style="margin-left:10px; color:var(--ink-faint); font-size:12px">
                $out and $merge are refused — they write, and a write must go through
                the editor so it is recorded.
              </span>
            </div>
          </div>
          ${this.renderDocuments()}
        `;
      }
      if (tab === "schema") {
        return html`<sextant-schema></sextant-schema>`;
      }
      if (tab === "indexes") {
        const ix = this.indexes();
        if (!ix.length) return html`<div class="empty">No indexes.</div>`;
        return html`<div>${ix.map((i) => html`
          <div class="doc"><pre innerHTML=${S.highlight(i)}></pre></div>`)}</div>`;
      }
      if (tab === "explain") {
        return html`
          ${this.renderQueryBar()}
          <div class="row" style="margin-bottom:10px">
            <button class="btn primary" onclick=${() => this.runExplain()}>Explain</button>
          </div>
          ${this.explainOut()
            ? html`<div class="doc"><pre innerHTML=${S.highlight(this.explainOut())}></pre></div>`
            : html`<div class="empty">Run a query to see its plan.</div>`}
        `;
      }
      return this.renderActivity();
    }

    render() {
      const conn = S.connection();
      const db = S.database();
      const col = S.collection();
      const editing = this.editing();
      const confirming = this.confirming();
      const tab = S.tab();

      const needsCollection = tab !== "activity";

      return html`
        <section class="main">
          <div class="crumb">
            <strong>${conn ? conn.name : "No connection"}</strong>
            ${db ? html`<span>/</span><strong>${db}</strong>` : ""}
            ${col ? html`<span>/</span><strong>${col}</strong>` : ""}
            ${conn && !conn.writable
              ? html`<span class="badge ro" style="margin-left:8px">read only</span>` : ""}
            <span class="spacer"></span>
            <span style="font-size:12px; color:var(--ink-faint)">
              ${this.total() === null ? "" : this.total() + " matching"}
            </span>
            <a class="btn" href="/sign-out" style="text-decoration:none">Sign out</a>
          </div>

          <div class="tabs">
            ${TABS.map(([id, label]) => html`
              <button class="tab" aria-selected=${String(tab === id)}
                      onclick=${() => { S.tab.set(id); this.maybeLoad(true); }}>${label}</button>
            `)}
          </div>

          <div class="pane">
            ${this.failed() ? html`<div class="notice bad">${this.failed()}</div>` : ""}
            ${this.note() ? html`<div class="notice">${this.note()}</div>` : ""}

            ${needsCollection && !col
              ? html`<div class="empty">Pick a collection on the left.</div>`
              : this.renderPane()}
          </div>
        </section>

        ${editing ? html`
          <div class="veil">
            <div class="dialog">
              <h3>${editing.doc ? "Edit document" : "Insert document"}</h3>
              <div class="body">
                <textarea data-q="editor" rows="16" spellcheck="false">${editing.json}</textarea>
              </div>
              <div class="foot">
                <button class="btn" onclick=${() => this.editing.set(null)}>Cancel</button>
                <button class="btn primary"
                        onclick=${() => (editing.doc ? this.saveEdit() : this.saveInsert())}>
                  ${editing.doc ? "Save" : "Insert"}
                </button>
              </div>
            </div>
          </div>
        ` : ""}

        ${confirming ? html`
          <div class="veil">
            <div class="dialog">
              <h3>${confirming.title}</h3>
              <div class="body">
                <p>${confirming.body}</p>
              </div>
              <div class="foot">
                <button class="btn" onclick=${() => this.confirming.set(null)}>Cancel</button>
                <button class=${"btn " + (confirming.danger ? "danger" : "primary")}
                        onclick=${() => confirming.run()}>Confirm</button>
              </div>
            </div>
          </div>
        ` : ""}
      `;
    }
  }

  customElements.define("sextant-workspace", SextantWorkspace);
})();
