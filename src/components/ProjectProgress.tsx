import { StatusBadge } from "./States";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";

interface ProjectProgressProps {
  projectId: string;
  fallbackStatus?: string | null;
}

function progressLabel(status: string, progress: number, hasJob: boolean): string {
  if (!hasJob) return "No import job";
  if (status === "completed") return "Import complete";
  if (status === "queued") return "Import queued";
  if (status === "running") return `Import ${progress}%`;
  if (status === "failed") return "Import failed";
  return status.replace(/^./, (character) => character.toUpperCase());
}

export function ProjectProgress({ projectId, fallbackStatus }: ProjectProgressProps) {
  const jobs = useAsync(() => api.listProjectJobs(projectId), [projectId]);
  const job = jobs.data?.[0] ?? null;
  const status = job?.status ?? fallbackStatus ?? "none";
  const progress = job?.status === "completed" ? 100 : Math.max(0, Math.min(100, job?.progress ?? 0));
  const label = jobs.loading ? "Loading status…" : progressLabel(status, progress, Boolean(job));

  return (
    <div className="project-progress" aria-label={`${status}: ${label}`}>
      <StatusBadge value={status} />
      <div className="mini-progress" role="presentation">
        <span style={{ width: `${progress}%` }} />
      </div>
      <small>{label}</small>
    </div>
  );
}
