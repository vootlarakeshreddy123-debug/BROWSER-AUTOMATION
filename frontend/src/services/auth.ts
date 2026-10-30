/** Supabase Auth (GoTrue REST) for the browser. Uses ONLY the publishable key; a secret key never belongs here.
 *  The session lives in sessionStorage (per tab, cleared when the tab closes). Supabase Auth only says WHO the user is:
 *  it never plans actions or decides whether a task succeeded. */
export interface Session { access_token: string; refresh_token: string; expires_at: number; email: string | null }

const URL_ = (import.meta.env.VITE_SUPABASE_URL || "").replace(/\/+$/, "");
const KEY = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY || "";
const STORE = "nexa.session";

export const authEnabled = (): boolean => Boolean(URL_ && KEY);

let session: Session | null = null;
const listeners = new Set<(s: Session | null) => void>();

function persist(s: Session | null) {
  session = s;
  try { s ? sessionStorage.setItem(STORE, JSON.stringify(s)) : sessionStorage.removeItem(STORE); } catch { /* storage unavailable: memory only */ }
  listeners.forEach((fn) => fn(s));
}

export function loadSession(): Session | null {
  if (session) return session;
  try {
    const raw = sessionStorage.getItem(STORE);
    if (raw) session = JSON.parse(raw) as Session;
  } catch { session = null; }
  return session;
}

export function onSession(fn: (s: Session | null) => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

async function gotrue(path: string, body: unknown, bearer?: string): Promise<Record<string, any>> {
  if (!authEnabled()) throw new Error("Sign-in is not configured (VITE_SUPABASE_URL / VITE_SUPABASE_PUBLISHABLE_KEY).");
  const res = await fetch(`${URL_}/auth/v1/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: KEY, ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}) },
    body: JSON.stringify(body ?? {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error_description || data.msg || data.message || `Sign-in failed (${res.status})`);
  return data;
}

function fromToken(d: Record<string, any>): Session {
  return {
    access_token: d.access_token, refresh_token: d.refresh_token,
    expires_at: d.expires_at ?? Math.floor(Date.now() / 1000) + Number(d.expires_in ?? 3600),
    email: d.user?.email ?? null,
  };
}

export async function signIn(email: string, password: string): Promise<Session> {
  const s = fromToken(await gotrue("token?grant_type=password", { email, password }));
  persist(s);
  return s;
}

export async function signOut(): Promise<void> {
  const s = loadSession();
  persist(null);                                    // local sign-out always succeeds
  if (s) await gotrue("logout", {}, s.access_token).catch(() => undefined);
}

/** A valid access token, refreshing it when it is about to expire. null = signed out. */
export async function getAccessToken(): Promise<string | null> {
  const s = loadSession();
  if (!s) return null;
  if (s.expires_at - 30 > Date.now() / 1000) return s.access_token;
  try {
    const next = fromToken(await gotrue("token?grant_type=refresh_token", { refresh_token: s.refresh_token }));
    next.email = next.email ?? s.email;
    persist(next);
    return next.access_token;
  } catch {
    persist(null);
    return null;
  }
}

export async function authHeaders(): Promise<Record<string, string>> {
  const t = await getAccessToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}
