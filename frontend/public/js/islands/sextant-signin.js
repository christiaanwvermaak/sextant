/* <sextant-signin> — the local break-glass door.
 *
 * OIDC callers never see this: they arrive with a bearer token and /api/me
 * answers straight away. This is the way in when the identity provider is
 * itself the outage, which is the day a database console matters most.
 */
(function () {
  const { signal, html, Tina4Element } = Tina4;
  const S = window.Sextant;

  class SextantSignin extends Tina4Element {
    static shadow = false;

    constructor() {
      super();
      // Created once. See the note in sextant-app.js.
      this.failed = signal(null);
      this.working = signal(false);
    }

    async submit(event) {
      event.preventDefault();
      const form = event.target.closest("form") || event.target;
      // Read from the DOM rather than binding value= on the inputs. A controlled
      // input inside a reactive render loses the caret position on every
      // keystroke, which makes a password field unusable.
      const username = form.querySelector('[name="username"]').value;
      const password = form.querySelector('[name="password"]').value;

      this.working.set(true);
      this.failed.set(null);
      const r = await S.api("/sign-in", { method: "POST", body: { username, password } });
      this.working.set(false);

      if (!r.ok) {
        this.failed.set(r.error);
        return;
      }
      await S.loadMe();
    }

    render() {
      const failed = this.failed();
      const working = this.working();
      // Read off <sextant-app>, which the server rendered. An inline <script>
      // carrying this would be blocked by `default-src 'self'` and the button
      // would simply never appear.
      const root = document.querySelector("sextant-app");
      const oidc = root && root.getAttribute("oidc") === "true";
      const provider = (root && root.getAttribute("provider")) || "single sign-on";

      return html`
        <div class="signin">
          <div class="card">
            <h1>Sextant</h1>
            <p>Sign in to reach the databases this console is configured for.</p>

            ${failed ? html`<div class="notice bad">${failed}</div>` : ""}

            ${oidc ? html`
              <a class="btn primary sso" href="/login">Sign in with ${provider}</a>
              <p class="or">or use the break-glass credential</p>
            ` : ""}

            <form onsubmit=${(e) => this.submit(e)}>
              <label>
                Username
                <input type="text" name="username" autocomplete="username" autofocus />
              </label>
              <label>
                Password
                <input type="password" name="password" autocomplete="current-password" />
              </label>
              <button class="btn primary" type="submit" disabled=${working}>
                ${working ? "Signing in…" : "Sign in"}
              </button>
            </form>
          </div>
        </div>
      `;
    }
  }

  customElements.define("sextant-signin", SextantSignin);
})();
