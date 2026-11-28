// Phase 16 — sign-in support for every API client.
//
// One fetch wrapper (installed before the app renders) adds credentials for a
// cross-origin API base and announces any 401 from the API, so the AuthGate can
// show the sign-in screen no matter which panel's request found the session expired.
const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
export const UNAUTHENTICATED_EVENT = "cinema:unauthenticated";

export interface AuthUser {
  id: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
}

export interface AuthStatus {
  mode: "off" | "password";
  user: AuthUser | null;
  can_register: boolean;
  first_user?: boolean;
  bootstrap_token_required?: boolean;
}

let installed = false;

export function installAuthFetch(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const isApi = url.includes("/api/v1/");
    const options: RequestInit = { ...init };
    if (isApi && API_BASE && !options.credentials) options.credentials = "include";
    const response = await original(input, options);
    if (isApi && response.status === 401 && !url.includes("/api/v1/auth/")) {
      window.dispatchEvent(new CustomEvent(UNAUTHENTICATED_EVENT));
    }
    return response;
  };
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}/api/v1/auth/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: API_BASE ? "include" : "same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = (data as { error?: { message?: string } }).error?.message ?? `Request failed (${response.status})`;
    throw new Error(message);
  }
  return data as T;
}

export const authApi = {
  status: async (): Promise<AuthStatus> => {
    const response = await fetch(`${API_BASE}/api/v1/auth/status`, { credentials: API_BASE ? "include" : "same-origin" });
    if (!response.ok) throw new Error(`Could not reach the server (${response.status})`);
    return (await response.json()) as AuthStatus;
  },
  login: (email: string, password: string) => post<AuthUser>("login", { email, password }),
  register: (email: string, password: string, displayName: string, bootstrapToken?: string) =>
    post<AuthUser>("register", { email, password, display_name: displayName || null, bootstrap_token: bootstrapToken || null }),
  logout: () => post<{ ok: boolean }>("logout", {}),
};
