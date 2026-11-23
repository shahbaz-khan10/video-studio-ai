import type {
  Health,
  Job,
  MediaAsset,
  Project,
  ProjectCreate,
  ProjectSummary,
  ServerInfo,
  UploadAccepted,
  UploadSession,
  Video,
} from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: { message?: string; code?: string } };
    return new ApiError(
      body.error?.message ?? `Request failed (${response.status})`,
      body.error?.code ?? "HTTP_ERROR",
      response.status,
    );
  } catch {
    return new ApiError(`Request failed (${response.status})`, "HTTP_ERROR", response.status);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    throw await parseError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>("/api/v1/health"),
  info: () => request<ServerInfo>("/api/v1/info"),
  listProjects: (search?: string) =>
    request<ProjectSummary[]>(`/api/v1/projects${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  createProject: (payload: ProjectCreate) =>
    request<Project>("/api/v1/projects", { method: "POST", body: JSON.stringify(payload) }),
  getProject: (id: string) => request<Project>(`/api/v1/projects/${encodeURIComponent(id)}`),
  listProjectJobs: (id: string) =>
    request<Job[]>(`/api/v1/projects/${encodeURIComponent(id)}/jobs`),
  deleteProject: (id: string) =>
    request<void>(`/api/v1/projects/${encodeURIComponent(id)}`, { method: "DELETE" }),
  getJob: (id: string) => request<Job>(`/api/v1/jobs/${encodeURIComponent(id)}`),
  deleteVideo: (id: string) =>
    request<void>(`/api/v1/videos/${encodeURIComponent(id)}`, { method: "DELETE" }),
  listVideoAssets: (videoId: string, kind?: string) =>
    request<MediaAsset[]>(
      `/api/v1/videos/${encodeURIComponent(videoId)}/assets${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`,
    ),
  reprobeVideo: (videoId: string) =>
    request<Job>(`/api/v1/videos/${encodeURIComponent(videoId)}/reprobe`, { method: "POST" }),
  videoUrl: (video: Video) => `${API_BASE}${video.stream_url}`,
  mediaUrl: (asset: MediaAsset) => `${API_BASE}${asset.url}`,
};

export interface VideoMetadata {
  duration_ms?: number;
  width?: number;
  height?: number;
}

const MIME_BY_EXTENSION: Record<string, string> = {
  mp4: "video/mp4",
  m4v: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
  mkv: "video/x-matroska",
  avi: "video/x-msvideo",
  ogv: "video/ogg",
  ogg: "video/ogg",
};

export function resolveVideoMimeType(filename: string, declared?: string): string | null {
  if (declared && declared.startsWith("video/")) return declared;
  const extension = filename.split(".").pop()?.toLowerCase() ?? "";
  return MIME_BY_EXTENSION[extension] ?? null;
}

interface UploadSessionInit {
  filename: string;
  size_bytes: number;
  mime_type: string;
  duration_ms?: number;
  width?: number;
  height?: number;
}

function uploadChunk(
  sessionId: string,
  index: number,
  chunk: Blob,
  onProgress: (percent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `${API_BASE}/api/v1/uploads/sessions/${encodeURIComponent(sessionId)}/chunks/${index}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(100);
        resolve();
        return;
      }
      let body: { error?: { message?: string; code?: string } } | null = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      reject(new ApiError(body?.error?.message ?? `Chunk ${index} failed`, body?.error?.code ?? "CHUNK_FAILED", xhr.status));
    };
    xhr.onerror = () => reject(new ApiError("Network error during upload", "NETWORK_ERROR", 0));
    xhr.send(chunk);
  });
}

export async function uploadVideo(
  projectId: string,
  file: File,
  metadata: VideoMetadata,
  onProgress: (percent: number) => void,
): Promise<UploadAccepted> {
  const mimeType = resolveVideoMimeType(file.name, file.type);
  if (!mimeType) {
    throw new ApiError("Choose a recognised MP4, MOV, WebM, MKV, AVI or Ogg video.", "UNSUPPORTED_MEDIA_TYPE", 422);
  }
  const payload: UploadSessionInit = {
    filename: file.name,
    size_bytes: file.size,
    mime_type: mimeType,
    ...metadata,
  };
  const session = await request<UploadSession>(
    `/api/v1/uploads/projects/${encodeURIComponent(projectId)}/sessions`,
    { method: "POST", body: JSON.stringify(payload) },
  );

  try {
    for (let index = 0; index < session.total_chunks; index += 1) {
      const start = index * session.chunk_size;
      const end = Math.min(start + session.chunk_size, file.size);
      await uploadChunk(session.id, index, file.slice(start, end), (chunkPercent) => {
        const span = 98 / session.total_chunks;
        const base = (index / session.total_chunks) * 98;
        onProgress(Math.min(99, Math.round(base + (chunkPercent / 100) * span)));
      });
    }
    const result = await request<UploadAccepted>(
      `/api/v1/uploads/sessions/${encodeURIComponent(session.id)}/complete`,
      { method: "POST" },
    );
    onProgress(100);
    return result;
  } catch (error) {
    try {
      await request<void>(`/api/v1/uploads/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
    } catch {}
    throw error;
  }
}
