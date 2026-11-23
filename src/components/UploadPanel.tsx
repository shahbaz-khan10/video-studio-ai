import { useRef, useState, type DragEvent } from "react";
import { resolveVideoMimeType, uploadVideo, type VideoMetadata } from "../lib/api";
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
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const selectFile = (candidate: File | undefined) => {
    if (!candidate) return;
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
    setUploading(true);
    setProgress(0);
    setError(null);
    try {
      const metadata = await readMetadata(file);
      const result = await uploadVideo(projectId, file, metadata, setProgress);
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  };

  return (
    <section className="panel upload-panel">
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
          <button className="button button-primary" type="button" onClick={submit} disabled={uploading}>
            {uploading ? `Uploading ${progress}%` : "Upload video"}
          </button>
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
