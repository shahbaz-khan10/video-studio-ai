import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { authApi, UNAUTHENTICATED_EVENT, type AuthStatus, type AuthUser } from "../lib/auth";
import { ErrorState, LoadingState } from "./States";

interface AuthContextValue {
  user: AuthUser | null;
  mode: "off" | "password";
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({ user: null, mode: "off", logout: async () => undefined });

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      setStatus(await authApi.status());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not reach the server.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const onExpired = () => {
      setExpired(true);
      setStatus((current) => (current && current.mode === "password" ? { ...current, user: null } : current));
    };
    window.addEventListener(UNAUTHENTICATED_EVENT, onExpired);
    return () => window.removeEventListener(UNAUTHENTICATED_EVENT, onExpired);
  }, []);

  const logout = useCallback(async () => {
    await authApi.logout().catch(() => undefined);
    setExpired(false);
    await refresh();
  }, [refresh]);

  if (error) return <ErrorState message={error} onRetry={() => void refresh()} />;
  if (!status) return <LoadingState label="Connecting…" />;
  if (status.mode === "password" && !status.user) {
    return (
      <SignIn
        status={status}
        expired={expired}
        onSignedIn={async () => {
          setExpired(false);
          await refresh();
        }}
      />
    );
  }
  return <AuthContext.Provider value={{ user: status.user, mode: status.mode, logout }}>{children}</AuthContext.Provider>;
}

function SignIn({ status, expired, onSignedIn }: { status: AuthStatus; expired: boolean; onSignedIn: () => Promise<void> }) {
  const firstUser = Boolean(status.first_user);
  const [mode, setMode] = useState<"login" | "register">(firstUser ? "register" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") await authApi.login(email, password);
      else await authApi.register(email, password, displayName, token);
      await onSignedIn();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-screen">
      <form className="panel auth-card" onSubmit={(event) => void submit(event)}>
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            CA
          </div>
          <div>
            <strong>CINEMA AI DUB</strong>
            <span>{mode === "login" ? "Sign in to continue" : firstUser ? "Create the administrator account" : "Create an account"}</span>
          </div>
        </div>
        {expired && mode === "login" && <p className="translation-notice">Your session ended. Please sign in again.</p>}
        <label>
          Email
          <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {mode === "register" && (
          <label>
            Display name (optional)
            <input value={displayName} maxLength={120} onChange={(e) => setDisplayName(e.target.value)} />
          </label>
        )}
        {mode === "register" && status.bootstrap_token_required && (
          <label>
            Setup token
            <input value={token} required autoComplete="off" onChange={(e) => setToken(e.target.value)} />
            <small className="muted">The AUTH_BOOTSTRAP_TOKEN value from the server configuration.</small>
          </label>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="button button-primary" disabled={busy}>
          {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
        </button>
        {status.can_register && !firstUser && (
          <button type="button" className="link-button" onClick={() => setMode(mode === "login" ? "register" : "login")}>
            {mode === "login" ? "Create an account" : "I already have an account"}
          </button>
        )}
      </form>
    </div>
  );
}
