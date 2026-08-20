/* <sextant-schema> — the Schema tab.
 *
 * The thing people open Compass for. A document store has no declared schema, so
 * the only way to know what is in a collection is to sample it and count.
 *
 * The visual grammar is a stacked bar per field showing the proportion of each
 * BSON type. That is not decoration: a field that is 98% string and 2% int is
 * almost always a bug someone has been living with, and a single bar makes it
 * obvious at a glance where a table of numbers would not.
 */
(function () {
  const { signal, html, Tina4Element } = Tina4;
  const S = window.Sextant;

  /* Stable colours per BSON type. Stable matters: the same type must be the
   * same colour on every field, or the bars cannot be compared down the page. */
  const TYPE_COLOUR = {
    string: "#2c7a5b", int: "#8a5a1a", double: "#a3711f", decimal: "#7a5a12",
    boolean: "#7a3ba8", date: "#1f6f8b", objectId: "#a97b28", array: "#4a6fa5",
    object: "#5a5f6a", null: "#b03a2e", binData: "#6d4c7d", regex: "#8a3b6b",
  };

  function colour(type) { return TYPE_COLOUR[type] || "#8a999f"; }

  class SextantSchema extends Tina4Element {
    static shadow = false;

    constructor() {
      super();
      // Created once. Tina4 calls render() inside a reactive effect, so signals
      // built in render() are rebuilt every pass and never settle.
      this.report = signal(null);
      this.loading = signal(false);
      this.failed = signal(null);
      this.sample = signal(1000);
      this.expanded = signal({});
      this._lastKey = null;
    }

    onMount() { this.load(); }

    key() {
      return [S.connectionId(), S.database(), S.collection()].join("|");
    }

    async load(force) {
      const k = this.key();
      if (!force && k === this._lastKey) return;
      if (!S.connectionId() || !S.database() || !S.collection()) return;
      this._lastKey = k;

      this.loading.set(true);
      this.failed.set(null);
      const r = await S.api(
        `/api/${encodeURIComponent(S.connectionId())}` +
        `/${encodeURIComponent(S.database())}` +
        `/${encodeURIComponent(S.collection())}/schema`,
        { method: "POST", body: { sample: this.sample() } });
      this.loading.set(false);
      if (!r.ok) { this.failed.set(r.error); this.report.set(null); return; }
      this.report.set(r.data);
    }

    toggle(path) {
      const open = Object.assign({}, this.expanded());
      open[path] = !open[path];
      this.expanded.set(open);
    }

    renderBar(field) {
      return html`
        <div class="typebar" title=${field.types.map((t) => t.type + " " + t.percent + "%").join(", ")}>
          ${field.types.map((t) => html`
            <span class="seg" style=${`width:${t.percent}%; background:${colour(t.type)}`}></span>
          `)}
        </div>
      `;
    }

    renderField(field) {
      const open = this.expanded()[field.path];
      return html`
        <div class=${"field" + (field.mixed_types ? " mixed" : "")}>
          <div class="field-head" onclick=${() => this.toggle(field.path)}>
            <span class="path">${field.path}</span>
            <span class="types">
              ${field.types.map((t) => html`
                <span class="chip" style=${`border-color:${colour(t.type)}; color:${colour(t.type)}`}>
                  ${t.type} ${t.percent}%
                </span>
              `)}
            </span>
            <span class="presence">
              ${field.presence_percent}% present
              ${field.missing > 0 ? html`<span class="missing">· ${field.missing} missing</span>` : ""}
            </span>
          </div>
          ${this.renderBar(field)}
          ${open ? html`
            <div class="field-detail">
              ${field.min !== undefined
                ? html`<div class="range">range <code>${String(field.min)}</code> → <code>${String(field.max)}</code></div>`
                : ""}
              ${field.too_many_distinct
                ? html`<div class="note">Too many distinct values to list — this looks like an identifier.</div>`
                : field.distinct
                  ? html`
                      <div class="note">${field.distinct_total} distinct value${field.distinct_total === 1 ? "" : "s"}</div>
                      <table class="dist">
                        ${field.distinct.map((d) => html`
                          <tr>
                            <td class="v"><code>${d.value}</code></td>
                            <td class="n">${d.count}</td>
                            <td class="p">
                              <span class="minibar" style=${`width:${Math.max(2, d.percent)}%`}></span>
                              ${d.percent}%
                            </td>
                          </tr>
                        `)}
                      </table>
                    `
                  : ""}
            </div>
          ` : ""}
        </div>
      `;
    }

    render() {
      if (!S.collection()) {
        return html`<div class="empty">Pick a collection on the left.</div>`;
      }
      if (this.loading()) {
        return html`<div class="empty">Sampling…</div>`;
      }
      if (this.failed()) {
        return html`<div class="notice bad">${this.failed()}</div>`;
      }
      const report = this.report();
      if (!report) {
        return html`<div class="empty">
          <button class="btn primary" onclick=${() => this.load(true)}>Analyse schema</button>
        </div>`;
      }

      const mixed = report.fields.filter((f) => f.mixed_types);

      return html`
        <div>
          <div class="querybar">
            <div class="row" style="align-items:center">
              <div class="narrow">
                <label>Sample size</label>
                <input type="number" data-q="sample" value=${String(this.sample())}
                       min="1" max="10000"
                       onchange=${(e) => this.sample.set(Number(e.target.value) || 1000)} />
              </div>
              <button class="btn primary" style="margin-top:16px"
                      onclick=${() => this.load(true)}>Re-sample</button>
              <span style="margin-left:auto; font-size:12px; color:var(--ink-faint)">
                ${report.sampled} document${report.sampled === 1 ? "" : "s"} sampled
                ${report.collection_total !== null
                  ? html` of ${report.collection_total}` : ""}
              </span>
            </div>
          </div>

          ${report.is_estimate ? html`
            <div class="notice warn">
              These percentages are measured on ${report.sampled} sampled documents,
              not on all ${report.collection_total}. Treat them as an estimate.
            </div>` : ""}

          ${mixed.length ? html`
            <div class="notice warn">
              <strong>${mixed.length} field${mixed.length === 1 ? " holds" : "s hold"} more than one type.</strong>
              That is usually worth looking at: ${mixed.slice(0, 4).map((f) => f.path).join(", ")}${mixed.length > 4 ? "…" : ""}
            </div>` : ""}

          <div class="schema">
            ${report.fields.map((f) => this.renderField(f))}
          </div>
        </div>
      `;
    }
  }

  customElements.define("sextant-schema", SextantSchema);
})();
