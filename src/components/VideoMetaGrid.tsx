import { formatBytes, formatDuration } from "../lib/format";
import type { MediaAsset, Video } from "../types";

interface VideoMetaGridProps {
  video: Video;
  assets?: MediaAsset[];
}

export function VideoMetaGrid({ video, assets = [] }: VideoMetaGridProps) {
  const frameIndex = assets.find((asset) => asset.kind === "frame_index");
  const frameSamples = (frameIndex?.metadata_json?.frame_count ?? video.frame_count) as number | null;
  const subtitleCount = video.subtitle_streams?.length ?? 0;
  const validation = video.stream_validation ?? {};
  const streamCheck = validation.video
    ? `Video ${validation.video} · audio ${validation.audio ?? "unknown"}`
    : "Pending";
  const probeLabel =
    video.probe_source === "ffprobe"
      ? "ffprobe"
      : video.probe_source === "ffmpeg"
        ? "ffmpeg"
        : video.probe_status === "client_metadata"
          ? "Browser"
          : "Pending";

  return (
    <div className="metadata-grid">
      <div>
        <span>Duration</span>
        <strong>{formatDuration(video.duration_ms)}</strong>
      </div>
      <div>
        <span>Resolution</span>
        <strong>{video.width && video.height ? `${video.width}×${video.height}` : "Unknown"}</strong>
      </div>
      <div>
        <span>Frame rate</span>
        <strong>{video.fps ? `${Number(video.fps.toFixed(3))} fps` : "Unknown"}</strong>
      </div>
      <div>
        <span>Frames</span>
        <strong>{frameSamples?.toLocaleString() ?? "Unknown"}</strong>
      </div>
      <div>
        <span>Size</span>
        <strong>{formatBytes(video.size_bytes)}</strong>
      </div>
      <div>
        <span>Video codec</span>
        <strong>{video.video_codec ?? "Unknown"}</strong>
      </div>
      <div>
        <span>Audio codec</span>
        <strong>{video.audio_codec ?? "None"}</strong>
      </div>
      <div>
        <span>Audio streams</span>
        <strong>{video.audio_stream_count ?? 0}</strong>
      </div>
      <div>
        <span>Aspect ratio</span>
        <strong>{video.aspect_ratio ?? "Unknown"}</strong>
      </div>
      <div>
        <span>Subtitle streams</span>
        <strong>{subtitleCount}</strong>
      </div>
      <div>
        <span>Stream check</span>
        <strong>{streamCheck}</strong>
      </div>
      <div>
        <span>Pixel format</span>
        <strong>{video.pixel_format ?? "Unknown"}</strong>
      </div>
      <div>
        <span>Color</span>
        <strong>{[video.color_space, video.color_range].filter(Boolean).join(" / ") || "Unknown"}</strong>
      </div>
      <div>
        <span>Bitrate</span>
        <strong>{video.bit_rate ? `${Math.round(video.bit_rate / 1000)} kb/s` : "Unknown"}</strong>
      </div>
      <div>
        <span>Probe source</span>
        <strong>{probeLabel}</strong>
      </div>
      <div>
        <span>Artifacts</span>
        <strong>{assets.length}</strong>
      </div>
    </div>
  );
}
