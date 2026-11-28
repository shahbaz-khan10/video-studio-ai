import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Job } from "../types";
import { LoadingState } from "./States";

interface JobProgressProps {
  jobId: string;
  onSettled: () => void;
  onJobChange?: (jobId: string) => void;
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function JobProgress({ jobId, onSettled, onJobChange }: JobProgressProps) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
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

  const cancel = async () => {
    if (acting) return;
    setActing(true);
    try {
      const result = await api.cancelJob(jobId);
      setJob(result);
      setActionError(null);
      onSettled();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not cancel the job.");
    } finally {
      setActing(false);
    }
  };

  const retry = async () => {
    if (acting) return;
    setActing(true);
    try {
      const result = await api.retryJob(jobId);
      setJob(result);
      setActionError(null);
      onJobChange?.(result.id);
      onSettled();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not retry the job.");
    } finally {
      setActing(false);
    }
  };

  if (!job && !error) return <LoadingState label="Reading processing status…" />;
  if (error) return <p className="form-error" role="alert">{error}</p>;
  if (!job) return null;

  return (
    <div className={`job-card job-${job.status}`}>
      <div className="job-head">
        <div>
          <strong>
            {job.type === "audio_separation"
              ? "Audio separation"
              : job.type === "transcription"
                ? "Speech recognition"
                : job.type === "character_detection"
                  ? "Character detection"
                  : job.type === "translation"
                    ? "Translation"
                    : job.type === "dub_audio_generation"
                      ? "Voice generation"
                      : job.type === "dub_timing"
                        ? "Dialogue timing"
                        : job.type === "mix_render"
                          ? "Mix rendering"
                          : job.type === "lipsync"
                            ? "Lip sync"
                            : "Import & inspect"}
          </strong>
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
      {(job.status === "queued" || job.status === "running") && (
        <div className="job-actions">
          <button className="button button-quiet" type="button" onClick={cancel} disabled={acting}>
            {acting ? "Cancelling…" : "Cancel job"}
          </button>
        </div>
      )}
      {(job.status === "failed" || job.status === "cancelled") && (
        <div className="job-actions">
          <button className="button button-primary" type="button" onClick={retry} disabled={acting}>
            {acting ? "Retrying…" : "Retry job"}
          </button>
        </div>
      )}
      {job.error_message && <p className="form-error" role="alert">{job.error_message}</p>}
      {actionError && <p className="form-error" role="alert">{actionError}</p>}
    </div>
  );
}
