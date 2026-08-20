/* <sextant-app> — decides whether you are looking at a sign-in or the console.
 *
 * ── The trap, do not reintroduce it ─────────────────────────────────────────
 * Tina4Element calls render() INSIDE a reactive effect, so render() re-runs on
 * every signal change. Creating a signal in render() therefore builds a NEW one
 * on every pass and re-fires whatever depends on it — an effect that never
 * settles, presenting as an island that renders nothing at all rather than as
 * an error. Signals are instance state, created once in the constructor; side
 * effects belong in onMount(), which runs once.
 */
(function () {
  const { html, Tina4Element } = Tina4;
  const S = window.Sextant;

  class SextantApp extends Tina4Element {
    /* Light DOM, not shadow. Tina4Element.shadow defaults to TRUE, and a shadow
     * root is a style boundary — /css/sextant.css would not reach inside it and
     * the whole console would render unstyled while the stylesheet loaded fine. */
    static shadow = false;

    constructor() {
      super();
      this.ready = S.busy;
      this.checked = Tina4.signal(false);
    }

    async onMount() {
      await S.loadMe();
      this.checked.set(true);
    }

    render() {
      if (!this.checked()) {
        return html`<div class="empty">Loading…</div>`;
      }
      if (!S.me()) {
        return html`<sextant-signin></sextant-signin>`;
      }
      return html`
        <div class="shell">
          <sextant-rail></sextant-rail>
          <sextant-workspace></sextant-workspace>
        </div>
      `;
    }
  }

  customElements.define("sextant-app", SextantApp);
})();
