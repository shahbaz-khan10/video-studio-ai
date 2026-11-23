import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Job } from "../types";
import { LoadingState } from "./States";

interface JobProgressProps {
  jobId: string;
  onSettled: () => void;
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function JobProgress({ jobId, onSettled }: JobProgressProps) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const settled = useRef(false);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    settled.current = false;

    const load = async () => {
      try {
        const result = await api.getJob(jobId);
        if (!active) return true;
        setJob(result);
        setError(null);
        if (TERMINAL.has(result.status)) {
          if (!settled.current) {
            settled.current = true;
            onSettled();
          }
          return true;
        }
        return false;
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "Could not read job status.");
        return true;
      }
    };

    void load();
    timer = window.setInterval(async () => {
      const done = await load();
      if (done && timer) window.clearInterval(timer);
    }, 900);

    return () => {
      active = false;
      if (timer) window.clearInterval(timer);
    };
  }, [jobId, onSettled]);

  if (!job && !error) return <LoadingState label="Reading processing status…" />;
  if (error) return <p className="form-error" role="alert">{error}</p>;
  if (!job) return null;

  return (
    <div className={`job-card job-${job.status}`}>
      <div className="job-head">
        <div>
          <strong>Import &amp; inspect</strong>
          <span className="job-type">{job.type}</span>
        </div>
        <span className={`badge badge-${job.status}`}>{job.status}</span>
      </div>
      <div className="progress-track" role="progressbar" aria-valuenow={job.progress} aria-valuemin={0} aria-valuemax={100}>
        <div className="progress-fill" style={{ width: `${job.progress}%` }} />
      </div>
      <div className="job-meta">
        <span>{job.progress}%</span>
        <span>{job.current_step ? job.current_step.replace(/_/g, " ") : "queued"}</span>
      </div>
      <ol className="step-list">
        {job.steps.map((step) => (
          <li key={step.id} className={`step-${step.status}`}>
            <span className="step-dot" aria-hidden="true" />
            <span className="step-name">{step.name.replace(/_/g, " ")}</span>
            <span className="step-detail">{step.detail ?? step.status}</span>
          </li>
        ))}
      </ol>
      {job.error_message && <p className="form-error" role="alert">{job.error_message}</p>}
    </div>
  );
}
