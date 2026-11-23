import { useCallback, useState } from "react";
import { JobProgress } from "../components/JobProgress";
import { UploadPanel } from "../components/UploadPanel";
import { VideoCard } from "../components/VideoCard";
import { EmptyState, ErrorState, LoadingState, StatusBadge } from "../components/States";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import type { UploadAccepted } from "../types";

interface ProjectDetailPageProps {
  projectId: string;
  maxUploadMb: number;
}

export function ProjectDetailPage({ projectId, maxUploadMb }: ProjectDetailPageProps) {
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const project = useAsync(() => api.getProject(projectId), [projectId]);
  const jobs = useAsync(() => api.listProjectJobs(projectId), [projectId]);

  const refreshAll = useCallback(() => {
    project.reload();
    jobs.reload();
  }, [project.reload, jobs.reload]);

  const onUploaded = useCallback(
    (result: UploadAccepted) => {
      setActiveJobId(result.job.id);
      refreshAll();
    },
    [refreshAll],
  );

  const onJobSettled = useCallback(() => {
    refreshAll();
  }, [refreshAll]);

  const onVideoDeleted = useCallback(() => {
    refreshAll();
  }, [refreshAll]);

  const onJobStarted = useCallback(
    (jobId: string) => {
      setActiveJobId(jobId);
      jobs.reload();
    },
    [jobs.reload],
  );

  if (project.loading) return <LoadingState label="Loading project…" />;
  if (project.error) return <ErrorState message={project.error} onRetry={project.reload} />;
  if (!project.data) return <EmptyState title="Project not found" message="Select another project from the list." />;

  const current = project.data;
  const jobId = activeJobId ?? jobs.data?.[0]?.id ?? null;

  return (
    <div className="page">
      <div className="breadcrumb">
        <a href="#/projects">Projects</a>
        <span>/</span>
        <span>{current.name}</span>
      </div>
      <section className="panel project-header">
        <div>
          <span className="eyebrow">Project</span>
          <h2>{current.name}</h2>
          <p>{current.description || "No description added."}</p>
        </div>
        <div className="project-header-meta">
          <StatusBadge value={current.status} />
          <span>Source: {current.source_language.toUpperCase()}</span>
          <span>Targets: {current.target_languages.join(", ").toUpperCase()}</span>
        </div>
      </section>

      <div className="detail-grid">
        <div className="detail-main">
          <UploadPanel projectId={projectId} maxUploadMb={maxUploadMb} onUploaded={onUploaded} />
          {actionError && <p className="form-error" role="alert">{actionError}</p>}
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>Source videos</h2>
                <p>Original quality is preserved; files are never re-encoded here.</p>
              </div>
              <button className="button button-quiet" type="button" onClick={project.reload}>
                Refresh
              </button>
            </div>
            {current.videos.length === 0 ? (
              <EmptyState title="No videos yet" message="Upload a video to see metadata and a preview here." />
            ) : (
              <div className="video-list">
                {current.videos.map((video) => (
                  <VideoCard
                    key={video.id}
                    video={video}
                    onDeleted={() => {
                      setActionError(null);
                      onVideoDeleted();
                    }}
                    onError={setActionError}
                    onJobStarted={onJobStarted}
                  />
                ))}
              </div>
            )}
          </section>
        </div>
        <aside className="detail-side">
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>Processing status</h2>
                <p>Real stages from the server import job.</p>
              </div>
            </div>
            {jobId ? (
              <JobProgress jobId={jobId} onSettled={onJobSettled} />
            ) : (
              <EmptyState title="No jobs yet" message="Upload a video to start an import and inspection job." />
            )}
          </section>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>Dubbing pipeline</h2>
                <p>Planned capabilities, shown as roadmap items — not active buttons.</p>
              </div>
            </div>
            <ol className="phase-list">
              {[
                "3 Video Processing Engine",
                "4 Audio Extraction & Separation",
                "5 Original Dialogue Control",
                "6 Speech Recognition",
                "7 Character Detection",
                "8 Translation Engine",
                "9 Character Voice Profiles",
                "10 Natural Voice Generation",
                "11 Dialogue Timing",
                "12 Audio Mixing Studio",
                "13 AI Lip Sync & Facial Synchronization",
                "14 Professional Timeline Editor",
                "16 Final Testing & Production",
              ].map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ol>
          </section>
        </aside>
      </div>
    </div>
  );
}
