/**
 * Sextant frontend.
 *
 * Renders the shell and proxies the API. It makes **no access decisions of its
 * own** — every /api call is forwarded to the backend, which resolves the caller
 * and the connection independently. A frontend that decided what you may see
 * would be a second place to get that wrong, and the two would drift.
 *
 * The proxy exists so the browser holds a session cookie rather than a bearer
 * token. A token in browser storage is readable by any script that gets onto the
 * page; a HttpOnly cookie is not.
 */
import { startServer, get, post, type Tina4Request, type Tina4Response } from "tina4-nodejs";
import {
  authorizeUrl, configured, exchangeCode, logoutUrl, newState, pkce,
  redirectUriFor, stateMatches, type Tokens,
} from "./src/oidc.js";

const BACKEND = process.env.BACKEND_URL ?? "http://sextant-backend:7145";
const PORT = Number(process.env.PORT ?? 7148);

// OIDC is optional: a local-only deployment (docker compose, a laptop) has no
// identity provider and signs in with the break-glass credential instead. If it
// IS configured, it must be configured completely — a half-set issuer produces a
// sign-in that fails at Keycloak, and the app would otherwise look healthy.
const OIDC_ENABLED = !!process.env.OIDC_ISSUER;
if (OIDC_ENABLED) {
  const missing = configured();
  if (missing.length) {
    for (const m of missing) console.error(`FATAL: ${m}`);
    process.exit(1);
  }
}

/**
 * `request.session` is a Tina4Session with get/set/delete/save — NOT a plain
 * object. Assigning to it replaces it and nothing is ever persisted, which
 * surfaces much later as "you are not signed in" on every request.
 *
 * It is also nullable: the framework degrades rather than 500-ing when the
 * session backend is unusable, so a request really can arrive without one.
 */
type Store = {
  get<T>(key: string): T | undefined;
  set(key: string, value: unknown): void;
  drop(key: string): void;
  ok: boolean;
};

function session(request: Tina4Request): Store {
  const s = (request as any).session as {
    get(k: string, d?: unknown): unknown;
    set(k: string, v: unknown): void;
    delete(k: string): void;
    save(): void;
  } | null;

  if (!s) {
    console.error("[session] request arrived with no session backend");
    return { get: () => undefined, set: () => {}, drop: () => {}, ok: false };
  }
  return {
    get: <T,>(k: string) => s.get(k) as T | undefined,
    set: (k, v) => { s.set(k, v); s.save(); },
    drop: (k) => { s.delete(k); s.save(); },
    ok: true,
  };
}

/** Forward to the backend, carrying whatever credential the session holds. */
async function forward(request: Tina4Request, path: string, init: RequestInit = {}) {
  const store = session(request);
  // Either an OIDC access token or a local break-glass token. The backend
  // decides which by shape and verifies accordingly; this only carries it.
  const token = store.get<Tokens>("tokens")?.access_token
              ?? store.get<string>("access_token");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> ?? {}),
  };
  // ONE credential path. An earlier draft forwarded the signed-in username in a
  // header for the local break-glass case and had the backend believe it. That
  // holds only while nothing else can reach the backend — another pod, a
  // port-forward, a misconfigured Service — and "not published" is a deployment
  // detail, not authentication. The backend now signs a token at sign-in and
  // verifies its own signature; this proxy only carries it.
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch(`${BACKEND}${path}`, { ...init, headers });
  const text = await response.text();
  return { status: response.status, text };
}

// ── API proxy ──────────────────────────────────────────────────────────────
//
// One handler for every /api path rather than mirroring each route. Mirroring
// them means a new backend route silently 404s here until someone remembers,
// and the frontend has no business knowing the route list.

async function proxy(request: Tina4Request, response: Tina4Response, method: string) {
  const url = (request as any).url ?? (request as any).path ?? "";
  const path = String(url).split("?")[0];
  const query = String(url).includes("?") ? "?" + String(url).split("?").slice(1).join("?") : "";

  const init: RequestInit = { method };
  if (method !== "GET") {
    init.body = JSON.stringify((request as any).body ?? {});
  }
  const result = await forward(request, path + query, init);
  return response(result.text, result.status, "application/json");
}

get("/api/*", async (request: Tina4Request, response: Tina4Response) =>
  proxy(request, response, "GET"));

post("/api/*", async (request: Tina4Request, response: Tina4Response) =>
  proxy(request, response, "POST"));

// ── sign-in ────────────────────────────────────────────────────────────────

post("/sign-in", async (request: Tina4Request, response: Tina4Response) => {
  const body = (request as any).body ?? {};
  const result = await forward(request, "/api/sign-in", {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (result.status !== 200) {
    return response(result.text, result.status, "application/json");
  }

  let who: { username?: string; via?: string; token?: string };
  try {
    who = JSON.parse(result.text);
  } catch {
    // A 200 that is not JSON means something other than the backend answered.
    return response(JSON.stringify({ error: "unexpected response from the backend" }),
      502, "application/json");
  }

  // Into the HttpOnly session cookie, never into page JavaScript. Anything that
  // can read this token can act as the user for its lifetime.
  session(request).set("access_token", who.token);
  session(request).set("username", who.username);

  // Rebuild the reply rather than forwarding it. The backend's response CONTAINS
  // the token, and passing it through would hand it straight to page scripts —
  // which is exactly what storing it in a HttpOnly cookie exists to prevent.
  // Forwarding the upstream body verbatim is the easy version of this mistake.
  return response(
    JSON.stringify({ username: who.username, via: who.via }),
    200, "application/json");
});

get("/sign-out", async (request: Tina4Request, response: Tina4Response) => {
  const store = session(request);
  const tokens = store.get<Tokens>("tokens");
  store.drop("access_token");
  store.drop("username");
  store.drop("tokens");
  // End the session at the provider too. Dropping only our cookie leaves them
  // signed in at Keycloak, so "sign out" followed by "sign in" is silently a
  // no-op and the next person at that machine is still them.
  if (OIDC_ENABLED && tokens?.id_token) {
    const host = (request as any).headers?.host;
    return response.redirect(logoutUrl(tokens.id_token, `https://${host}/`));
  }
  return response.redirect("/");
});

// ── single sign-on ─────────────────────────────────────────────────────────

get("/login", async (request: Tina4Request, response: Tina4Response) => {
  if (!OIDC_ENABLED) return response.redirect("/");
  const s = session(request);
  if (!s.ok) {
    return response(JSON.stringify({ error: "could not start a session" }),
      500, "application/json");
  }
  const { verifier, challenge } = pkce();
  const state = newState();
  const redirectUri = redirectUriFor((request as any).headers?.host);
  s.set("state", state);
  s.set("verifier", verifier);
  // Stored so the token exchange sends a BYTE-IDENTICAL redirect_uri. OAuth
  // requires the two to match exactly, and recomputing it in the callback breaks
  // the moment the two requests disagree about the host.
  s.set("redirect_uri", redirectUri);
  return response.redirect(authorizeUrl(state, challenge, redirectUri));
});

get("/callback", async (request: Tina4Request, response: Tina4Response) => {
  const s = session(request);
  const query = ((request as any).query ?? {}) as Record<string, string>;

  if (query.error) {
    console.error(`[auth] the identity provider returned ${query.error}`);
    return response.redirect("/?error=declined");
  }

  // Without this a third party could hand someone a crafted callback URL and
  // sign them into an account that is not theirs.
  const expected = s.get<string>("state");
  if (!expected || !stateMatches(expected, query.state ?? "")) {
    console.error(`[auth] state mismatch (stored=${expected ? "yes" : "no"}, session=${s.ok ? "ok" : "unavailable"})`);
    return response.redirect("/?error=state");
  }

  const verifier = s.get<string>("verifier") ?? "";
  const redirectUri = s.get<string>("redirect_uri")
                   ?? redirectUriFor((request as any).headers?.host);
  s.drop("state");
  s.drop("verifier");
  s.drop("redirect_uri");

  try {
    s.set("tokens", await exchangeCode(query.code ?? "", verifier, redirectUri));
  } catch (e) {
    console.error(`[auth] token exchange failed: ${(e as Error).message}`);
    return response.redirect("/?error=exchange");
  }
  return response.redirect("/");
});

// ── pages ──────────────────────────────────────────────────────────────────
//
// One page. Everything else is an island talking to the API, which is what makes
// the tabbed layout possible without a round trip per tab.

get("/", async (_request: Tina4Request, response: Tina4Response) =>
  // `oidc` decides whether the sign-in screen offers a single sign-on button or
  // only the local form. Rendered as an ATTRIBUTE rather than an inline script,
  // because `default-src 'self'` blocks inline <script> silently.
  response.render("index.html", {
    version: process.env.SEXTANT_VERSION ?? "dev",
    oidc: OIDC_ENABLED ? "true" : "false",
    provider: process.env.OIDC_PROVIDER_NAME ?? "single sign-on",
  }));

get("/healthz", async (_request: Tina4Request, response: Tina4Response) =>
  response({ ok: true }, 200));

startServer("", PORT, () => {
  console.log(`sextant frontend on ${PORT}, backend ${BACKEND}`);
});
