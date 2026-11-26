import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import type { MediaAsset, Video } from "../types";

interface ProjectThumbProps {
  projectId: string;
  compact?: boolean;
}

interface ThumbData {
  video: Video | null;
  asset: MediaAsset | null;
}

export function ProjectThumb({ projectId, compact = false }: ProjectThumbProps) {
  const media = useAsync<ThumbData>(async () => {
    const project = await api.getProject(projectId);
    const video = project.videos[0] ?? null;
    if (!video) return { video: null, asset: null };
    const assets = await api.listVideoAssets(video.id, "thumbnail");
    return { video, asset: assets[0] ?? null };
  }, [projectId]);

  const asset = media.data?.asset ?? null;
  const title = media.data?.video?.original_filename ?? "No source video";

  return (
    <div className={`project-thumb${compact ? " project-thumb-compact" : ""}`} title={title}>
      {asset ? (
        <img src={api.mediaUrl(asset)} alt={`Preview frame from ${title}`} loading="lazy" />
      ) : (
        <span aria-hidden="true">▶</span>
      )}
    </div>
  );
}
