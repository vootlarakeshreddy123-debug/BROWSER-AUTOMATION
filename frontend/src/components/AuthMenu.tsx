import { useEffect, useState } from "react";
import { authEnabled, loadSession, onSession, signIn, signOut, type Session } from "../services/auth";
import { getHealth } from "../services/api";

/**
 * Supabase sign-in controls.
 * If the frontend is not configured for Supabase, show a helpful diagnostic
 * instead of silently hiding the only way to obtain a token.
 */
export default function AuthMenu() {
  const [session, setSession] = useState<Session | null>(loadSession());
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [backendAuthEnabled, setBackendAuthEnabled] = useState<boolean | null>(null);
  const configured = authEnabled();

  useEffect(() => {
    const unsubscribe = onSession(setSession);
    getHealth()
      .then((health) => setBackendAuthEnabled(Boolean(health.auth_enabled)))
      .catch(() => setBackendAuthEnabled(null));
    return unsubscribe;
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email.trim(), password);
      setPassword("");
      setOpen(false);
    } catch (err) {
      setError((err as Error).message || "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  };

  if (!configured) {
    const label = backendAuthEnabled === false
      ? "Local mode · sign-in off"
      : backendAuthEnabled === true
        ? "Auth mismatch · configure frontend Supabase"
        : "Checking authentication setup…";
    const detail = backendAuthEnabled === false
      ? "The backend is configured for local single-user mode. Tasks do not require a sign-in."
      : backendAuthEnabled === true
        ? "The backend requires Supabase tokens, but the frontend has no VITE_SUPABASE_URL / VITE_SUPABASE_PUBLISHABLE_KEY. Configure both sides or switch the backend to local mode."
        : "Checking the backend authentication mode.";
    return (
      <div
        className={`max-w-xs text-right text-xs ${backendAuthEnabled === true ? "text-amber-300" : "text-muted"}`}
        title={detail}
        role="status"
      >
        {label}
      </div>
    );
  }

  if (session) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted">
        <span title="Signed in" className="max-w-[10rem] truncate">
          {session.email ?? "Signed in"}
        </span>
        <button
          type="button"
          className="nav-button"
          onClick={() => void signOut()}
        >
          Sign out
        </button>
      </div>
    );
  }

  return (
    <div className="relative">
      <button
        type="button"
        className="nav-button"
        onClick={() => {
          setError(null);
          setOpen((v) => !v);
        }}
      >
        Sign in
      </button>
      {open && (
        <form
          onSubmit={submit}
          className="panel absolute right-0 z-20 mt-2 w-72 space-y-2 p-3"
        >
          <p className="text-xs text-muted">
            Sign in with your Supabase account to run tasks when backend
            authentication is enabled.
          </p>
          <input
            type="email"
            required
            autoComplete="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded bg-base px-2 py-1 text-sm"
          />
          <input
            type="password"
            required
            autoComplete="current-password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded bg-base px-2 py-1 text-sm"
          />
          {error && <p role="alert" className="submit-error">{error}</p>}
          <button
            type="submit"
            disabled={busy}
            className="nav-button nav-active w-full"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      )}
    </div>
  );
}
