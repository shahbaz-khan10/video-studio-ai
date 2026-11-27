// Phase 11 — dialogue timing API client.
import { ApiError } from "./api";
import type { Job } from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
const enc = encodeURIComponent;

export type TimingLanguage = "ur" | "hi";
export type TimingStatus = "no_audio" | "pending" | "stale" | "fitted" | "fitted_fast" | "needs_attention" | "failed";

export interface TimingFlag {
  code: string;
  severity: "error" | "warning" | "info";
  message: string;
}

export interface TimingSuggestion {
  action: "shorten_translation" | "regenerate_faster" | "manual_timing" | "allow_overlap";
  message: string;
  speed_factor?: number;
  cut_percent?: number;
}

export interface Placement {
  id: string;
  status: TimingStatus;
  strategy: string | null;
  manual: boolean;
  approved: boolean;
  manual_start_ms: number | null;
  manual_end_ms: number | null;
  allow_overlap_ms: number;
  window_start_ms: number | null;
  window_end_ms: number | null;
  placed_start_ms: number | null;
  placed_end_ms: number | null;
  take_id: string | null;
  take_duration_ms: number | null;
  speech_duration_ms: number | null;
  fitted_duration_ms: number | null;
  tempo: number;
  pause_saved_ms: number;
  overflow_ms: number;
  details: {
    steps?: string[];
    lead_room_ms?: number;
    tail_room_ms?: number;
    voiced_ratio?: number;
    original_duration_ms?: number;
    difference_ms?: number;
    text?: string;
  };
  flags: TimingFlag[];
  suggestions: TimingSuggestion[];
  error_message: string | null;
  audio_url: string | null;
  updated_at: string | null;
}

export interface TimingLine {
  segment_id: string;
  position: number;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  source_text: string;
  text: string;
  character_id: string | null;
  character_name: string | null;
  take: { id: string; take_number: number; duration_ms: number | null; approved: boolean; audio_url: string } | null;
  difference_ms: number | null;
  overlaps: { segment_id: string; ms: number; same_speaker: boolean }[];
  status: TimingStatus;
  placement: Placement | null;
}

export interface TimingBundle {
  video_id: string;
  language: TimingLanguage;
  duration_ms: number | null;
  original_audio: string | null;
  limits: { natural_tempo: number; max_tempo: number; min_pause_ms: number; max_tail_ms: number; max_lead_ms: number };
  lines: TimingLine[];
  summary: Partial<Record<TimingStatus | "total", number>>;
  track: { exists: boolean; stale: boolean; url: string; clips: number; duration_ms: number; rendered_at: string } | null;
  job: Job | null;
}

export interface Waveform {
  source: string;
  kind: string;
  start_ms: number;
  end_ms: number;
  peaks: [number, number][];
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    let code = "HTTP_ERROR";
    try {
      const body = (await response.json()) as { error?: { message?: string; code?: string } };
      message = body.error?.message ?? message;
      code = body.error?.code ?? code;
    } catch {
      /* non-JSON */
    }
    throw new ApiError(message, code, response.status);
  }
  return (await response.json()) as T;
}

export const dubTimingApi = {
  bundle: (videoId: string, language: TimingLanguage) =>
    call<TimingBundle>(`/api/v1/videos/${enc(videoId)}/dub-timing?language=${language}`),
  fitBatch: (videoId: string, body: { language: TimingLanguage; mode: "pending" | "failed" | "all"; overwrite_manual: boolean }) =>
    call<Job>(`/api/v1/videos/${enc(videoId)}/dub-timing/fit`, { method: "POST", body: JSON.stringify(body) }),
  fitLine: (videoId: string, segmentId: string, language: TimingLanguage) =>
    call<TimingLine>(`/api/v1/videos/${enc(videoId)}/dub-timing/segments/${enc(segmentId)}/fit`, {
      method: "POST",
      body: JSON.stringify({ language }),
    }),
  update: (
    videoId: string,
    segmentId: string,
    body: { language: TimingLanguage; start_ms?: number; end_ms?: number; allow_overlap_ms?: number; approved?: boolean; reset?: boolean },
  ) => call<TimingLine>(`/api/v1/videos/${enc(videoId)}/dub-timing/segments/${enc(segmentId)}`, { method: "PATCH", body: JSON.stringify(body) }),
  regenerateFaster: (videoId: string, segmentId: string, language: TimingLanguage, speedFactor: number) =>
    call<TimingLine>(`/api/v1/videos/${enc(videoId)}/dub-timing/segments/${enc(segmentId)}/regenerate-faster`, {
      method: "POST",
      body: JSON.stringify({ language, speed_factor: speedFactor }),
    }),
  renderTrack: (videoId: string, language: TimingLanguage) =>
    call<Record<string, unknown>>(`/api/v1/videos/${enc(videoId)}/dub-timing/track/render?language=${language}`, { method: "POST" }),
  waveform: (videoId: string, source: "original" | TimingLanguage, startMs: number, endMs: number, buckets: number) =>
    call<Waveform>(
      `/api/v1/videos/${enc(videoId)}/dub-timing/waveform?source=${source}&start_ms=${Math.max(0, Math.round(startMs))}&end_ms=${Math.round(endMs)}&buckets=${buckets}`,
    ),
  url: (path: string) => `${API_BASE}${path}`,
};
