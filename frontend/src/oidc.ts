/**
 * OIDC authorization-code login against Keycloak, with PKCE.
 *
 * The portal is a confidential client and still uses PKCE. The client secret
 * alone would be enough for Keycloak, but PKCE additionally binds the callback
 * to the browser that started the login, which is what stops an intercepted
 * authorization code being redeemed by anyone else.
 *
 * Tokens live in the server-side session only. Nothing is written to a cookie
 * the browser can read, so an XSS in a template cannot lift a token that grants
 * cluster access.
 */
import { createHash, randomBytes, timingSafeEqual } from "crypto";

export const ISSUER =
  process.env.OIDC_ISSUER ?? "https://auth.c8eapps.co.za/realms/c8e-internal";
export const CLIENT_ID = process.env.OIDC_CLIENT_ID ?? "sextant";

/**
 * Hostnames the portal will build a redirect URI for.
 *
 * The redirect URI is derived from the host the browser actually used, NOT
 * fixed. With two hostnames a fixed value sends someone who signed in on one
 * host back to the other, where their session cookie does not exist — so the
 * callback finds no stored state and rejects a perfectly good login. Both names
 * are registered on the Keycloak client, so either works.
 *
 * Allow-listed rather than trusting the Host header: an attacker-controlled
 * Host would otherwise make us hand Keycloak a redirect URI pointing at them.
 * Keycloak would reject it, but the check belongs here too.
 */
export const ALLOWED_HOSTS = (
  process.env.OIDC_ALLOWED_HOSTS ?? "mongo.c8eapps.co.za"
).split(",").map((h) => h.trim()).filter(Boolean);

export const CANONICAL_HOST = ALLOWED_HOSTS[0];

export function redirectUriFor(host: string | undefined): string {
  const clean = (host ?? "").split(":")[0].toLowerCase();
  const chosen = ALLOWED_HOSTS.includes(clean) ? clean : CANONICAL_HOST;
  return `https://${chosen}/callback`;
}

const CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET ?? "";

export interface Tokens {
  id_token: string;
  access_token: string;
  refresh_token?: string;
  expires_at: number;
}

export interface Claims {
  sub: string;
  preferred_username?: string;
  name?: string;
  email?: string;
  groups?: string[];
  exp: number;
}

function base64url(buffer: Buffer): string {
  return buffer.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A fresh PKCE verifier plus the challenge to send with the redirect. */
export function pkce(): { verifier: string; challenge: string } {
  const verifier = base64url(randomBytes(48));
  const challenge = base64url(createHash("sha256").update(verifier).digest());
  return { verifier, challenge };
}

/** Opaque value tying the callback back to the request that started it. */
export function newState(): string {
  return base64url(randomBytes(24));
}

/**
 * Compare the returned state to the stored one without leaking timing.
 * Length is checked first because timingSafeEqual throws on a mismatch.
 */
export function stateMatches(expected: string, actual: string): boolean {
  if (!expected || !actual || expected.length !== actual.length) return false;
  return timingSafeEqual(Buffer.from(expected), Buffer.from(actual));
}

export function authorizeUrl(state: string, challenge: string, redirectUri: string): string {
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    redirect_uri: redirectUri,
    response_type: "code",
    scope: "openid profile email",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  return `${ISSUER}/protocol/openid-connect/auth?${params}`;
}

export async function exchangeCode(
  code: string,
  verifier: string,
  redirectUri: string,
): Promise<Tokens> {
  // redirect_uri must be byte-identical to the one sent to /auth, which is why
  // the callback reads it back out of the session rather than recomputing it.
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: CLIENT_ID,
    client_secret: CLIENT_SECRET,
    code,
    redirect_uri: redirectUri,
    code_verifier: verifier,
  });

  const res = await fetch(`${ISSUER}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });

  if (!res.ok) {
    // Keycloak's error body can echo request detail. Log the status only.
    console.error(`[oidc] token exchange failed: ${res.status}`);
    throw new Error("Could not complete sign in");
  }

  const json = (await res.json()) as any;
  return {
    id_token: json.id_token,
    access_token: json.access_token,
    refresh_token: json.refresh_token,
    // 30s of slack so a request that starts just under the wire does not land
    // on the far side of expiry at the backend.
    expires_at: Date.now() + (json.expires_in - 30) * 1000,
  };
}

export async function refresh(refreshToken: string): Promise<Tokens> {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: CLIENT_ID,
    client_secret: CLIENT_SECRET,
    refresh_token: refreshToken,
  });
  const res = await fetch(`${ISSUER}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) throw new Error("session expired");
  const json = (await res.json()) as any;
  return {
    id_token: json.id_token,
    access_token: json.access_token,
    refresh_token: json.refresh_token ?? refreshToken,
    expires_at: Date.now() + (json.expires_in - 30) * 1000,
  };
}

/**
 * Read the claims out of an ID token WITHOUT verifying it.
 *
 * Safe only because this token came straight from Keycloak's token endpoint
 * over TLS in exchange for our own client secret, and is used purely to draw
 * the page. Every decision that grants anything is made by the backend, which
 * verifies the signature, or by the cluster. Do not reuse this on a token that
 * arrived from a browser.
 */
export function claimsOf(idToken: string): Claims {
  const part = idToken.split(".")[1];
  const json = Buffer.from(part.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8");
  return JSON.parse(json) as Claims;
}

export function logoutUrl(idToken: string, returnTo: string): string {
  const params = new URLSearchParams({
    id_token_hint: idToken,
    post_logout_redirect_uri: returnTo,
  });
  return `${ISSUER}/protocol/openid-connect/logout?${params}`;
}

export function configured(): string[] {
  const missing: string[] = [];
  if (!CLIENT_SECRET) missing.push("OIDC_CLIENT_SECRET");
  if (!ALLOWED_HOSTS.length) missing.push("OIDC_ALLOWED_HOSTS is empty");
  return missing;
}
