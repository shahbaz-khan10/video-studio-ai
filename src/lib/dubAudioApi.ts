// Phase 10 — voice generation API client (kept separate from api.ts so
// Phase 9 work in shared files is not disturbed).
import { ApiError } from "./api";
import type { Job } from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";

export type DubLanguage = "ur" | "hi";
export type Delivery =
  | "neutral"
  | "happy"
  | "sad"
  | "angry"
  | "fearful"
  | "excited"
  | "calm"
  | "whispering"
  | "shouting"
  | "serious";
export type LineStatus = "no_text" | "missing" | "ready" | "approved" | "stale_text" | "stale_voice" | "failed";

export interface DubQcFlag {
  code: string;
  severity: "error" | "warning" | "info";
  message: string;
}

export interface DubVoice {
  synth: "builtin" | "phase9";
  language: DubLanguage;
  engine: string;
  voice_id: string | null;
  model_id: string | null;
  params: Record<string, unknown>;
  characteristics: Record<string, unknown>;
  native: string[];
  speed: number;
  pitch_semitones: number;
  tone: string;
  emotion: Delivery;
  seed: number;
  pause_ms: number | null;
  reference_id: string | null;
  source: "default" | "phase9_profile" | "phase9_profile_fallback";
  profile_id: string | null;
  gender: string;
  applied: string[];
  shaped: string[];
  notes: string[];
  fingerprint: string;
  engine_label: string;
  license: string | null;
  emotions: Delivery[];
}

export interface DubTake {
  id: string;
  segment_id: string;
  language: DubLanguage;
  take_number: number;
  status: "ready" | "failed";
  is_current: boolean;
  approved: boolean;
  text: string;
  emotion: Delivery;
  engine: string;
  model: string | null;
  voice_source: string;
  voice: DubVoice;
  sample_rate: number | null;
  duration_ms: number | null;
  size_bytes: number | null;
  stats: Record<string, number | boolean>;
  qc_flags: DubQcFlag[];
  error_message: string | null;
  audio_url: string | null;
  download_url: string | null;
  synthetic: true;
  created_at: string | null;
}

export interface DubLine {
  segment_id: string;
  position: number;
  start_ms: number;
  end_ms: number;
  source_text: string;
  speaker_label: string | null;
  character_id: string | null;
  character_name: string | null;
  text: string;
  translation_origin: string | null;
  status: LineStatus;
  current_take: DubTake | null;
  takes: DubTake[];
  voice_fingerprint: string;
}

export interface DubCharacter {
  key: string;
  character_id: string | null;
  name: string;
  lines: number;
  voice: DubVoice;
}

export interface DubEngineInfo {
  sample_rate: number;
  builtin: {
    label: string;
    license: string;
    commercial_use: boolean;
    available: boolean;
    cached: Record<DubLanguage, boolean>;
    models: Record<DubLanguage, string>;
    ram_gb: number;
    download_gb: number;
    notes: string;
  };
  phase9_available: boolean;
  phase9_engines: Record<string, { label: string; license: string; emotion: boolean }>;
  all_emotions: Delivery[];
  synthetic_disclosure: string;
}

export interface DubBundle {
  video_id: string;
  project_id: string;
  language: DubLanguage;
  engine: DubEngineInfo;
  characters: DubCharacter[];
  lines: DubLine[];
  summary: Partial<Record<LineStatus | "total", number>>;
  job: Job | null;
}

export interface GenerateLineOptions {
  language: DubLanguage;
  emotion?: Delivery;
  speed_factor?: number | null;
  pitch_offset?: number | null;
  pause_ms?: number | null;
  new_variation?: boolean;
}

export interface GenerateBatchOptions {
  language: DubLanguage;
  mode: "pending" | "missing" | "failed" | "all";
  include_approved?: boolean;
  character_id?: string | null;
  segment_ids?: string[] | null;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    let code = "HTTP_ERROR";
    try {
      const body = (await response.json()) as { error?: { message?: string; code?: string } };
      message = body.error?.message ?? message;
      code = body.error?.code ?? code;
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(message, code, response.status);
  }
  return (await response.json()) as T;
}

const enc = encodeURIComponent;

export const dubAudioApi = {
  bundle: (videoId: string, language: DubLanguage) =>
    call<DubBundle>(`/api/v1/videos/${enc(videoId)}/dub-audio?language=${language}`),
  generateLine: (videoId: string, segmentId: string, options: GenerateLineOptions) =>
    call<DubLine>(`/api/v1/videos/${enc(videoId)}/dub-audio/segments/${enc(segmentId)}/generate`, {
      method: "POST",
      body: JSON.stringify(options),
    }),
  generateBatch: (videoId: string, options: GenerateBatchOptions) =>
    call<Job>(`/api/v1/videos/${enc(videoId)}/dub-audio/generate`, { method: "POST", body: JSON.stringify(options) }),
  approve: (takeId: string, approved: boolean) =>
    call<DubLine>(`/api/v1/dub-audio/takes/${enc(takeId)}/approve`, { method: "POST", body: JSON.stringify({ approved }) }),
  select: (takeId: string) => call<DubLine>(`/api/v1/dub-audio/takes/${enc(takeId)}/select`, { method: "POST" }),
  audioUrl: (path: string, cacheKey?: string) => `${API_BASE}${path}${cacheKey ? `${path.includes("?") ? "&" : "?"}v=${cacheKey}` : ""}`,
  async preview(videoId: string, voiceKey: string, language: DubLanguage, text?: string): Promise<string> {
    const response = await fetch(`${API_BASE}/api/v1/videos/${enc(videoId)}/dub-audio/voices/${enc(voiceKey)}/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language, text: text || null }),
    });
    if (!response.ok) {
      let message = `Preview failed (${response.status})`;
      try {
        message = ((await response.json()) as { error?: { message?: string } }).error?.message ?? message;
      } catch {
        /* ignore */
      }
      throw new ApiError(message, "PREVIEW_FAILED", response.status);
    }
    return URL.createObjectURL(await response.blob());
  },
};
