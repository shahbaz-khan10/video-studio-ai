import { useRef, useState, type DragEvent } from "react";
import { api, resolveVideoMimeType, uploadVideo, type VideoMetadata } from "../lib/api";
import type { UploadAccepted } from "../types";

interface UploadPanelProps {
  projectId: string;
  maxUploadMb: number;
  onUploaded: (result: UploadAccepted) => void;
}

function readMetadata(file: File): Promise<VideoMetadata> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const element = document.createElement("video");
    element.preload = "metadata";
    const finish = (metadata: VideoMetadata) => {
      URL.revokeObjectURL(url);
      resolve(metadata);
    };
    element.onloadedmetadata = () => {
      finish({
        duration_ms: Number.isFinite(element.duration) ? Math.round(element.duration * 1000) : undefined,
        width: element.videoWidth || undefined,
        height: element.videoHeight || undefined,
      });
    };
    element.onerror = () => finish({});
    element.src = url;
  });
}

export function UploadPanel({ projectId, maxUploadMb, onUploaded }: UploadPanelProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sessionRef = useRef<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const selectFile = (candidate: File | undefined) => {
    if (!candidate || uploading) return;
    setError(null);
    setProgress(0);
    if (!resolveVideoMimeType(candidate.name, candidate.type)) {
      setError("Choose a video file (MP4, MOV, WebM, MKV, AVI or Ogg).");
      return;
    }
    if (candidate.size > maxUploadMb * 1024 * 1024) {
      setError(`File is larger than the ${maxUploadMb} MB upload limit.`);
      return;
    }
    setFile(candidate);
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    selectFile(event.dataTransfer.files[0]);
  };

  const submit = async () => {
    if (!file || uploading) return;
    const controller = new AbortController();
    abortRef.current = controller;
    sessionRef.current = null;
    setUploading(true);
    setProgress(0);
    setError(null);
    try {
      const metadata = await readMetadata(file);
      const result = await uploadVideo(projectId, file, metadata, setProgress, {
        signal: controller.signal,
        onSession: (sessionId) => {
          sessionRef.current = sessionId;
        },
      });
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded(result);
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(reason instanceof Error ? reason.message : "Upload failed.");
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        sessionRef.current = null;
        setUploading(false);
      }
    }
  };

  const cancel = async () => {
    const sessionId = sessionRef.current;
    abortRef.current?.abort();
    if (sessionId) {
      try {
        await api.cancelUploadSession(sessionId);
      } catch {}
    }
    sessionRef.current = null;
    setUploading(false);
    setProgress(0);
    setError(null);
  };

  return (
    <section className="panel upload-panel" id="upload-panel">
      <div className="panel-heading">
        <div>
          <h2>Add source video</h2>
          <p>Chunked and resumable. The original file is stored unchanged with no re-encoding.</p>
        </div>
        <span className="chip">Phase 3 engine</span>
      </div>
      <div
        className={`dropzone${dragging ? " dropzone-active" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <div className="dropzone-icon" aria-hidden="true">
          ↑
        </div>
        <p>Drag a video here, or choose a file</p>
        <input
          ref={inputRef}
          type="file"
          accept="video/*"
          onChange={(event) => selectFile(event.target.files?.[0])}
          aria-label="Select video file"
        />
        <small>Maximum {maxUploadMb} MB · audio and video stay together in the original file</small>
      </div>
      {file && (
        <div className="selected-file">
          <div>
            <strong>{file.name}</strong>
            <span>{(file.size / 1024 / 1024).toFixed(1)} MB</span>
          </div>
          <div className="selected-file-actions">
            <button className="button button-primary" type="button" onClick={submit} disabled={uploading}>
              {uploading ? `Uploading ${progress}%` : error ? "Retry upload" : "Upload video"}
            </button>
            {uploading && (
              <button className="button button-quiet" type="button" onClick={cancel}>
                Cancel upload
              </button>
            )}
          </div>
        </div>
      )}
      {uploading && (
        <div className="progress-track" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}>
          <div className="progress-fill" style={{ width: `${progress}%` }} />
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
    </section>
  );
}
