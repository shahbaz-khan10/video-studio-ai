import type { ReactNode } from "react";
import { useRoute } from "../lib/router";
import { useAuth } from "./AuthGate";
import type { ServerInfo } from "../types";

interface LayoutProps {
  children: ReactNode;
  info: ServerInfo | null;
  databaseStatus: string | null;
}

export function Layout({ children, info, databaseStatus }: LayoutProps) {
  const route = useRoute();
  const { user, mode, logout } = useAuth();
  const activeNav = route.name === "dashboard" ? "dashboard" : "projects";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            CA
          </div>
          <div>
            <strong>CINEMA AI DUB</strong>
            <span>Dubbing studio</span>
          </div>
        </div>
        <nav className="nav" aria-label="Main navigation">
          <a href="#/dashboard" aria-current={activeNav === "dashboard" ? "page" : undefined}>Overview</a>
          <a href="#/projects" aria-current={activeNav === "projects" ? "page" : undefined}>Projects</a>
        </nav>
        <div className="sidebar-note">
          <span className="dot" />
          <span>{databaseStatus === "ok" ? "Backend connected" : "Backend check pending"}</span>
        </div>
        {info && (
          <div className="sidebar-meta">
            <span>v{info.version}</span>
            <span>{info.media_probe_available ? "Media probe ready" : "Browser metadata mode"}</span>
          </div>
        )}
      </aside>
      <div className="main-column">
        <header className="topbar">
          <div>
            <h1>CINEMA AI DUB</h1>
            <p>Upload, organise and inspect dubbing projects in one place.</p>
          </div>
          {mode === "password" && user ? (
            <div className="topbar-user">
              <span title={user.email}>
                {user.display_name || user.email}
                {user.is_admin ? " · admin" : ""}
              </span>
              <button type="button" className="button button-quiet" onClick={() => void logout()}>
                Sign out
              </button>
            </div>
          ) : (
            <div className="topbar-badge">Local mode (sign-in off)</div>
          )}
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}
