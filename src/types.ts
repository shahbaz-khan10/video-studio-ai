export interface MediaAsset {
  id: string;
  video_id: string;
  kind: string;
  version: number;
  mime_type: string;
  size_bytes: number;
  checksum_sha256: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
  url: string;
}

export interface Video {
  id: string;
  project_id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  checksum_sha256: string | null;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  frame_count: number | null;
  pixel_format: string | null;
  color_space: string | null;
  color_range: string | null;
  bit_rate: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  stream_layout: Record<string, unknown>[];
  probe_status: string;
  probe_source: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
  stream_url: string;
}

export interface JobStep {
  id: string;
  name: string;
  status: string;
  detail: string | null;
  position: number;
}

export interface Job {
  id: string;
  project_id: string;
  video_id: string;
  type: string;
  status: string;
  progress: number;
  current_step: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  steps: JobStep[];
}

export interface ProjectSummary {
  id: string;
  name: string;
  description: string | null;
  source_language: string;
  target_languages: string[];
  status: string;
  created_at: string;
  updated_at: string;
  video_count: number;
  latest_job_status: string | null;
}

export interface Project extends Omit<ProjectSummary, "video_count" | "latest_job_status"> {
  videos: Video[];
}

export interface ProjectCreate {
  name: string;
  description?: string | null;
  source_language?: string;
  target_languages?: string[];
}

export interface UploadAccepted {
  video: Video;
  job: Job;
}

export interface UploadSession {
  id: string;
  project_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  chunk_size: number;
  total_chunks: number;
  received_bytes: number;
  uploaded_chunks: number[];
  status: string;
  expires_at: string;
}

export interface Health {
  status: string;
  app: string;
  environment: string;
  database: string;
}

export interface ServerInfo {
  app: string;
  version: string;
  max_upload_mb: number;
  upload_chunk_mb: number;
  thumbnail_count: number;
  allowed_video_types: string[];
  media_probe_available: boolean;
  ai_stages: string;
  capabilities: Record<string, boolean>;
}
