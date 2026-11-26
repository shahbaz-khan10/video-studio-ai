import { useState } from "react";
import { NewProjectDialog } from "../components/NewProjectDialog";
import { ProjectProgress } from "../components/ProjectProgress";
import { ProjectThumb } from "../components/ProjectThumb";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import { formatDate } from "../lib/format";
import type { Project } from "../types";

export function ProjectsPage() {
  const [search, setSearch] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const projects = useAsync(() => api.listProjects(search || undefined), [search]);

  const remove = async (id: string, name: string) => {
    if (!window.confirm(`Delete project "${name}" and its uploaded files?`)) return;
    setActionError(null);
    try {
      await api.deleteProject(id);
      projects.reload();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not delete project.");
    }
  };

  return (
    <div className="page">
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Projects</h2>
            <p>Each project holds one or more source videos and their dubbing work.</p>
          </div>
          <button className="button button-primary" type="button" onClick={() => setDialogOpen(true)}>
            New project
          </button>
        </div>
        <div className="toolbar">
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search projects"
            aria-label="Search projects"
          />
          <button className="button button-quiet" type="button" onClick={projects.reload}>
            Refresh
          </button>
        </div>
        {actionError && <p className="form-error" role="alert">{actionError}</p>}
        {projects.loading && <LoadingState label="Loading projects…" />}
        {projects.error && <ErrorState message={projects.error} onRetry={projects.reload} />}
        {!projects.loading && !projects.error && projects.data?.length === 0 && (
          <EmptyState
            title={search ? "No matching projects" : "No projects yet"}
            message={search ? "Try a different search term." : "Create a project to start uploading source videos."}
          />
        )}
        <div className="project-grid">
          {projects.data?.map((project) => (
            <article className="project-card" key={project.id}>
              <div className="project-card-top">
                <ProjectThumb projectId={project.id} compact />
                <span className="project-card-label">Source preview</span>
              </div>
              <h3>
                <a href={`#/projects/${project.id}`}>{project.name}</a>
              </h3>
              <p>{project.description || "No description added."}</p>
              <div className="project-languages">
                {project.target_languages.map((language) => (
                  <span key={language} className="chip">
                    {language.toUpperCase()}
                  </span>
                ))}
              </div>
              <ProjectProgress projectId={project.id} fallbackStatus={project.latest_job_status ?? project.status} />
              <div className="project-card-foot">
                <span>{project.video_count} video{project.video_count === 1 ? "" : "s"}</span>
                <span>{formatDate(project.updated_at)}</span>
              </div>
              <div className="project-card-actions">
                <a className="button button-quiet" href={`#/projects/${project.id}`}>Open</a>
                <a className="button button-quiet" href={`#/projects/${project.id}/studio`}>Studio</a>
                <button className="button button-danger" type="button" onClick={() => remove(project.id, project.name)}>
                  Delete
                </button>
              </div>
            </article>
          ))}
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
