import { useState } from "react";
import { api } from "../lib/api";
import { formatDate } from "../lib/format";
import { useAsync } from "../hooks/useAsync";
import { NewProjectDialog } from "../components/NewProjectDialog";
import { ProjectProgress } from "../components/ProjectProgress";
import { ProjectThumb } from "../components/ProjectThumb";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import type { Project } from "../types";

export function DashboardPage() {
  const [dialogOpen, setDialogOpen] = useState(false);
  const projects = useAsync(() => api.listProjects(), []);
  const info = useAsync(() => api.info(), []);
  const health = useAsync(() => api.health(), []);

  const list = projects.data ?? [];
  const totalVideos = list.reduce((sum, item) => sum + item.video_count, 0);
  const processing = list.filter((item) => item.latest_job_status === "running" || item.latest_job_status === "queued").length;

  return (
    <div className="page">
      <section className="hero">
        <div>
          <span className="eyebrow">Project foundation</span>
          <h2>Your dubbing workspace is ready.</h2>
          <p>
            Create projects, upload source video, and inspect real metadata and processing status.
            Heavy AI stages stay on the central server and are added in later phases.
          </p>
          <div className="hero-actions">
            <button className="button button-primary" type="button" onClick={() => setDialogOpen(true)}>
              Create new project
            </button>
            <a className="button button-quiet" href="#/projects">
              Open projects
            </a>
          </div>
        </div>
        <div className="hero-card">
          <span>Studio status</span>
          <strong>{health.data?.status === "ok" ? "Operational" : "Checking…"}</strong>
          <div className="hero-card-row">
            <span>Database</span>
            <span>{health.data?.database ?? "…"}</span>
          </div>
          <div className="hero-card-row">
            <span>Max upload</span>
            <span>{info.data ? `${info.data.max_upload_mb} MB` : "…"}</span>
          </div>
          <div className="hero-card-row">
            <span>AI stages</span>
            <span>{info.data?.ai_stages ?? "…"}</span>
          </div>
        </div>
      </section>

      <section className="stats-grid" aria-label="Workspace statistics">
        <div className="stat-card">
          <span>Projects</span>
          <strong>{projects.loading ? "—" : list.length}</strong>
        </div>
        <div className="stat-card">
          <span>Source videos</span>
          <strong>{projects.loading ? "—" : totalVideos}</strong>
        </div>
        <div className="stat-card">
          <span>In progress</span>
          <strong>{projects.loading ? "—" : processing}</strong>
        </div>
        <div className="stat-card">
          <span>API</span>
          <strong>{health.data?.status ?? "…"}</strong>
        </div>
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Recent projects</h2>
            <p>The newest projects appear first.</p>
          </div>
          <a className="button button-quiet" href="#/projects">
            View all
          </a>
        </div>
        {projects.loading && <LoadingState label="Loading projects…" />}
        {projects.error && <ErrorState message={projects.error} onRetry={projects.reload} />}
        {!projects.loading && !projects.error && list.length === 0 && (
          <EmptyState title="No projects yet" message="Create your first dubbing project to add a source video." />
        )}
        {!projects.loading && !projects.error && list.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Preview</th>
                  <th>Name</th>
                  <th>Languages</th>
                  <th>Videos</th>
                  <th>Status / progress</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {list.slice(0, 6).map((project) => (
                  <tr key={project.id}>
                    <td>
                      <ProjectThumb projectId={project.id} compact />
                    </td>
                    <td>
                      <a href={`#/projects/${project.id}`}>{project.name}</a>
                      <small className="row-sublink">
                        <a href={`#/projects/${project.id}/studio`}>Studio</a>
                      </small>
                    </td>
                    <td>{project.target_languages.join(", ") || "—"}</td>
                    <td>{project.video_count}</td>
                    <td>
                      <ProjectProgress projectId={project.id} fallbackStatus={project.latest_job_status ?? project.status} />
                    </td>
                    <td>{formatDate(project.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel roadmap-panel">
        <div className="panel-heading">
          <div>
            <h2>Planned AI stages</h2>
            <p>These are part of the 16-phase roadmap and are not enabled in this foundation build.</p>
          </div>
        </div>
        <div className="chip-grid">
          {["Dialogue separation", "Speech recognition", "Character detection", "Translation", "Character voices", "Lip sync", "Timeline editor", "Language exports"].map(
            (item) => (
              <span className="chip chip-planned" key={item}>
                {item} · planned
              </span>
            ),
          )}
        </div>
      </section>
      {dialogOpen && (
        <NewProjectDialog
          onClose={() => setDialogOpen(false)}
          onCreated={(project: Project) => {
            setDialogOpen(false);
            projects.reload();
            window.location.hash = `/projects/${project.id}`;
          }}
        />
      )}
    </div>
  );
}
