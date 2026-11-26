import { useEffect, useState } from "react";
import { VideoMetaGrid } from "./VideoMetaGrid";
import { api } from "../lib/api";
import { formatDate, formatDuration } from "../lib/format";
import type { MediaAsset, Video } from "../types";

interface VideoCardProps {
  video: Video;
  onDeleted: (videoId: string) => void;
  onError: (message: string) => void;
  onJobStarted: (jobId: string) => void;
}

export function VideoCard({ video, onDeleted, onError, onJobStarted }: VideoCardProps) {
  const [assets, setAssets] = useState<MediaAsset[]>([]);
  const [reprobing, setReprobing] = useState(false);

  useEffect(() => {
    let active = true;
    api
      .listVideoAssets(video.id)
      .then((result) => {
        if (active) setAssets(result);
      })
      .catch((reason) => {
        if (active) onError(reason instanceof Error ? reason.message : "Could not load derived assets.");
      });
    return () => {
      active = false;
    };
  }, [video.id, video.probe_status, onError]);

  const remove = async () => {
    if (!window.confirm(`Remove "${video.original_filename}" from this project?`)) return;
    try {
      await api.deleteVideo(video.id);
      onDeleted(video.id);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Could not remove video.");
    }
  };

  const reprobe = async () => {
    if (reprobing) return;
    setReprobing(true);
    try {
      const job = await api.reprobeVideo(video.id);
      onJobStarted(job.id);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Could not start re-inspection.");
    } finally {
      setReprobing(false);
    }
  };

  const thumbnails = assets.filter((asset) => asset.kind === "thumbnail");

  return (
    <article className="video-card">
      <div className="video-preview">
        <video controls preload="metadata" src={api.videoUrl(video)} aria-label={`Preview of ${video.original_filename}`} />
        {thumbnails.length > 0 && (
          <div className="thumbnail-strip" aria-label="Extracted preview frames">
            {thumbnails.map((asset) => {
              const timestamp = Number(asset.metadata_json.timestamp_ms ?? 0);
              return (
                <figure key={asset.id}>
                  <img src={api.mediaUrl(asset)} alt={`Frame at ${formatDuration(timestamp)}`} loading="lazy" />
                  <figcaption>{formatDuration(timestamp)}</figcaption>
                </figure>
              );
            })}
          </div>
        )}
      </div>
      <div className="video-body">
        <div className="video-title-row">
          <h3 title={video.original_filename}>{video.original_filename}</h3>
          <div className="video-actions">
            <button className="button button-quiet" type="button" onClick={reprobe} disabled={reprobing}>
              {reprobing ? "Starting…" : "Re-inspect"}
            </button>
            <button className="button button-danger" type="button" onClick={remove}>
              Remove
            </button>
          </div>
        </div>
        <VideoMetaGrid video={video} assets={assets} />
        <p className="video-footnote">
          Added {formatDate(video.created_at)} · original file unchanged · export policy: stream-copy until a visual edit
        </p>
      </div>
    </article>
  );
}
