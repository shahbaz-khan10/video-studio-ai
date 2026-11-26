import type {
  AudioBundle,
  AudioMixSettings,
  Character,
  CharacterBundle,
  CharacterRelationshipItem,
  GlossaryTerm,
  TargetLanguage,
  TranslateAllRequest,
  TranslationBundle,
  TranslationLine,
  TranslationProfile,
  CharacterDetectionRequest,
  CharacterMergeRequest,
  CharacterSplitRequest,
  CharacterUpdate,
  Health,
  Job,
  MediaAsset,
  Project,
  ProjectCreate,
  ProjectSummary,
  SegmentAssignmentUpdate,
  ServerInfo,
  Transcript,
  TranscriptUpdate,
  TranscriptionRequest,
  UploadAccepted,
  UploadSession,
  Video,
  VisualCandidateUpdate,
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
  cancelJob: (id: string) =>
    request<Job>(`/api/v1/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  retryJob: (id: string) =>
    request<Job>(`/api/v1/jobs/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  cancelUploadSession: (id: string) =>
    request<void>(`/api/v1/uploads/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deleteVideo: (id: string) =>
    request<void>(`/api/v1/videos/${encodeURIComponent(id)}`, { method: "DELETE" }),
  listVideoAssets: (videoId: string, kind?: string) =>
    request<MediaAsset[]>(
      `/api/v1/videos/${encodeURIComponent(videoId)}/assets${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`,
    ),
  reprobeVideo: (videoId: string) =>
    request<Job>(`/api/v1/videos/${encodeURIComponent(videoId)}/reprobe`, { method: "POST" }),
  getAudioBundle: (videoId: string) =>
    request<AudioBundle>(`/api/v1/videos/${encodeURIComponent(videoId)}/audio`),
  separateAudio: (videoId: string) =>
    request<Job>(`/api/v1/videos/${encodeURIComponent(videoId)}/audio/separate`, { method: "POST" }),
  getAudioMixSettings: (projectId: string) =>
    request<AudioMixSettings>(`/api/v1/projects/${encodeURIComponent(projectId)}/audio-mix`),
  saveAudioMixSettings: (projectId: string, payload: AudioMixSettings) =>
    request<AudioMixSettings>(`/api/v1/projects/${encodeURIComponent(projectId)}/audio-mix`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  resetAudioMixSettings: (projectId: string) =>
    request<AudioMixSettings>(`/api/v1/projects/${encodeURIComponent(projectId)}/audio-mix/reset`, {
      method: "POST",
    }),
  getTranscript: (videoId: string) =>
    request<Transcript>(`/api/v1/videos/${encodeURIComponent(videoId)}/transcript`),
  startTranscription: (videoId: string, payload: TranscriptionRequest) =>
    request<Job>(`/api/v1/videos/${encodeURIComponent(videoId)}/transcript/transcribe`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  saveTranscript: (videoId: string, payload: TranscriptUpdate) =>
    request<Transcript>(`/api/v1/videos/${encodeURIComponent(videoId)}/transcript`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  getTranslations: (videoId: string) =>
    request<TranslationBundle>(`/api/v1/videos/${encodeURIComponent(videoId)}/translations`),
  translateAll: (videoId: string, payload: TranslateAllRequest) =>
    request<Job>(`/api/v1/videos/${encodeURIComponent(videoId)}/translations/translate`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  translateSegment: (videoId: string, segmentId: string, languages: TargetLanguage[], force = false) =>
    request<TranslationLine>(
      `/api/v1/videos/${encodeURIComponent(videoId)}/translations/segments/${encodeURIComponent(segmentId)}/translate`,
      { method: "POST", body: JSON.stringify({ languages, force }) },
    ),
  saveTranslation: (
    segmentId: string,
    language: TargetLanguage,
    payload: { text?: string; roman_text?: string; base_revision?: number },
  ) =>
    request<TranslationLine>(`/api/v1/translations/${encodeURIComponent(segmentId)}/${language}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  restoreTranslation: (segmentId: string, language: TargetLanguage, target: "ai" | "history", index?: number) =>
    request<TranslationLine>(`/api/v1/translations/${encodeURIComponent(segmentId)}/${language}/restore`, {
      method: "POST",
      body: JSON.stringify({ target, index }),
    }),
  romanizeTranslation: (segmentId: string) =>
    request<TranslationLine>(`/api/v1/translations/${encodeURIComponent(segmentId)}/ur/romanize`, { method: "POST" }),
  saveTranslationProfile: (characterId: string, payload: Omit<TranslationProfile, "character_id" | "name" | "status" | "names_auto">) =>
    request<TranslationProfile>(`/api/v1/characters/${encodeURIComponent(characterId)}/translation-profile`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  saveRelationships: (projectId: string, relationships: CharacterRelationshipItem[]) =>
    request<CharacterRelationshipItem[]>(`/api/v1/projects/${encodeURIComponent(projectId)}/character-relationships`, {
      method: "PUT",
      body: JSON.stringify({ relationships }),
    }),
  saveGlossary: (projectId: string, terms: GlossaryTerm[]) =>
    request<GlossaryTerm[]>(`/api/v1/projects/${encodeURIComponent(projectId)}/translation-glossary`, {
      method: "PUT",
      body: JSON.stringify({ terms }),
    }),
  saveSceneNote: (videoId: string, segmentId: string, note: string) =>
    request<{ segment_id: string; note: string }>(
      `/api/v1/videos/${encodeURIComponent(videoId)}/translation-scenes/${encodeURIComponent(segmentId)}`,
      { method: "PUT", body: JSON.stringify({ note }) },
    ),
  getCharacterBundle: (videoId: string) =>
    request<CharacterBundle>(`/api/v1/videos/${encodeURIComponent(videoId)}/characters`),
  startCharacterDetection: (videoId: string, payload: CharacterDetectionRequest) =>
    request<Job>(`/api/v1/videos/${encodeURIComponent(videoId)}/characters/detect`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateCharacter: (characterId: string, payload: CharacterUpdate) =>
    request<Character>(`/api/v1/characters/${encodeURIComponent(characterId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  mergeCharacters: (characterId: string, payload: CharacterMergeRequest) =>
    request<Character>(`/api/v1/characters/${encodeURIComponent(characterId)}/merge`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  splitCharacter: (characterId: string, payload: CharacterSplitRequest) =>
    request<Character>(`/api/v1/characters/${encodeURIComponent(characterId)}/split`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  assignSegmentCharacter: (videoId: string, segmentId: string, payload: SegmentAssignmentUpdate) =>
    request<CharacterBundle>(
      `/api/v1/videos/${encodeURIComponent(videoId)}/segments/${encodeURIComponent(segmentId)}/character`,
      { method: "PUT", body: JSON.stringify(payload) },
    ),
  updateVisualCandidate: (candidateId: string, payload: VisualCandidateUpdate) =>
    request<CharacterBundle>(`/api/v1/visual-candidates/${encodeURIComponent(candidateId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  listProjectCharacters: (projectId: string) =>
    request<Character[]>(`/api/v1/projects/${encodeURIComponent(projectId)}/characters`),
  videoUrl: (video: Video) => `${API_BASE}${video.stream_url}`,
  assetUrl: (url: string) => `${API_BASE}${url}`,
  mediaUrl: (asset: MediaAsset) => `${API_BASE}${asset.url}`,
  mediaDownloadUrl: (asset: MediaAsset) => `${API_BASE}${asset.url}?download=1`,
  audioDownloadUrl: (videoId: string, role: string) =>
    `${API_BASE}/api/v1/videos/${encodeURIComponent(videoId)}/audio/stems/${encodeURIComponent(role)}/download`,
  sourceAudioDownloadUrl: (videoId: string) =>
    `${API_BASE}/api/v1/videos/${encodeURIComponent(videoId)}/audio/source/download`,
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
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const cleanup = () => signal?.removeEventListener("abort", abort);
    const abort = () => xhr.abort();
    if (signal) {
      if (signal.aborted) {
        reject(new DOMException("Upload cancelled", "AbortError"));
        return;
      }
      signal.addEventListener("abort", abort, { once: true });
    }
    xhr.open("PUT", `${API_BASE}/api/v1/uploads/sessions/${encodeURIComponent(sessionId)}/chunks/${index}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Upload cancelled", "AbortError"));
    };
    xhr.onload = () => {
      cleanup();
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
    xhr.onerror = () => {
      cleanup();
      reject(new ApiError("Network error during upload", "NETWORK_ERROR", 0));
    };
    xhr.send(chunk);
  });
}

export interface UploadOptions {
  signal?: AbortSignal;
  onSession?: (sessionId: string) => void;
}

export async function uploadVideo(
  projectId: string,
  file: File,
  metadata: VideoMetadata,
  onProgress: (percent: number) => void,
  options: UploadOptions = {},
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
  options.onSession?.(session.id);

  try {
    for (let index = 0; index < session.total_chunks; index += 1) {
      if (options.signal?.aborted) throw new DOMException("Upload cancelled", "AbortError");
      const start = index * session.chunk_size;
      const end = Math.min(start + session.chunk_size, file.size);
      await uploadChunk(
        session.id,
        index,
        file.slice(start, end),
        (chunkPercent) => {
          const span = 98 / session.total_chunks;
          const base = (index / session.total_chunks) * 98;
          onProgress(Math.min(99, Math.round(base + (chunkPercent / 100) * span)));
        },
        options.signal,
      );
    }
    const result = await request<UploadAccepted>(
      `/api/v1/uploads/sessions/${encodeURIComponent(session.id)}/complete`,
      { method: "POST" },
    );
    onProgress(100);
    return result;
  } catch (error) {
    if (!options.signal?.aborted) {
      try {
        await request<void>(`/api/v1/uploads/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
      } catch {}
    }
    throw error;
  }
}
