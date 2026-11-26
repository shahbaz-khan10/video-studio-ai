import { StatusBadge } from "./States";
import { VideoMetaGrid } from "./VideoMetaGrid";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import type { Job, Project, Video } from "../types";

interface VideoWorkspaceProps {
  project: Project;
  videos: Video[];
  video: Video | null;
  job: Job | null;
  onSelectVideo: (videoId: string) => void;
}

export function VideoWorkspace({ project, videos, video, job, onSelectVideo }: VideoWorkspaceProps) {
  const assets = useAsync(
    () => (video ? api.listVideoAssets(video.id) : Promise.resolve([])),
    [video?.id],
  );

  const focusUpload = () => {
    document.getElementById("upload-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <section className="panel workspace-panel" aria-label="Video workspace">
      <div className="panel-heading">
        <div>
          <h2>Video workspace</h2>
          <p>Preview the real source file and inspect its import status.</p>
        </div>
        <a className="button button-primary" href={`#/projects/${project.id}/studio`}>
          Open dubbing studio
        </a>
      </div>

      {!video ? (
        <div className="workspace-empty">
          <div>
            <h3>No source video selected</h3>
            <p>Upload a video to enable preview, metadata inspection and the dubbing studio.</p>
          </div>
          <button className="button button-quiet" type="button" onClick={focusUpload}>
            Go to upload
          </button>
        </div>
      ) : (
        <div className="workspace-grid">
          <div className="workspace-preview">
            <video controls preload="metadata" src={api.videoUrl(video)} aria-label={`Preview of ${video.original_filename}`} />
            {videos.length > 1 && (
              <label className="workspace-select">
                <span>Active source</span>
                <select value={video.id} onChange={(event) => onSelectVideo(event.target.value)}>
                  {videos.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.original_filename}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <div className="workspace-details">
            <div className="workspace-file">
              <span>Original file</span>
              <strong title={video.original_filename}>{video.original_filename}</strong>
            </div>
            <VideoMetaGrid video={video} assets={assets.data ?? []} />
            <div className="workspace-status">
              <StatusBadge value={job?.status ?? "idle"} />
              <div>
                <strong>{job?.current_step ?? "Import inspection"}</strong>
                <small>
                  {job
                    ? `${job.progress}% · ${job.status}`
                    : "No active import job for this source"}
                </small>
              </div>
            </div>
            <div className="workspace-actions">
              <button className="button button-quiet" type="button" onClick={focusUpload}>
                Upload source
              </button>
              <button className="button button-quiet" type="button" disabled title="Replacing a source video is planned for a later phase.">
                Replace source · planned
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
