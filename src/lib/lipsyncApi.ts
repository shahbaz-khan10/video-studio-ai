// Phase 13 — lip sync API client.
import { ApiError } from "./api";
import type { Job } from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
const enc = encodeURIComponent;

export type LipLanguage = "ur" | "hi";

export interface LipFlag {
  code: string;
  severity: "error" | "warning" | "info";
  message: string;
}

export interface FaceSummary {
  track_id: number;
  frames: number;
  coverage: number;
  box: [number, number, number, number];
  speaking_evidence: number;
  mouth_activity: number;
  face_size_px: number;
}

export interface SyncScore {
  confidence: number;
  best_offset_frames: number;
  similarity_at_0: number;
  best_similarity: number;
  windows: number;
}

export interface LipResult {
  stale: boolean;
  stale_reason: string | null;
  id: string;
  language: LipLanguage;
  attempt: number;
  is_current: boolean;
  status: "generated" | "failed" | "needs_face" | "no_face" | "no_audio";
  review: "pending" | "accepted" | "rejected";
  review_note: string | null;
  engine: string;
  model: string | null;
  target_source: string | null;
  audio_source: string | null;
  window_start_ms: number | null;
  window_end_ms: number | null;
  frame_count: number | null;
  fps: number | null;
  width: number | null;
  height: number | null;
  faces: FaceSummary[];
  qc: {
    face_coverage?: number;
    face_height_px?: number;
    other_faces?: number;
    upper_face_psnr_db?: number | null;
    outside_face_psnr_db?: number | null;
    sync_original?: SyncScore | null;
    sync_before?: SyncScore | null;
    sync_after?: SyncScore | null;
    output?: { frames?: number; black_frames?: number; duration_s?: number };
  };
  flags: LipFlag[];
  processing_s: number | null;
  error_message: string | null;
  after_url: string | null;
  before_url: string | null;
}

export interface LipLine {
  segment_id: string;
  position: number;
  start_ms: number;
  end_ms: number;
  text: string;
  character_id: string | null;
  character_name: string | null;
  dub: { source: string; start_ms: number; end_ms: number; url: string } | null;
  selection: { time_ms: number; box: number[]; off_screen: boolean; character_id: string | null } | null;
  current: LipResult | null;
  latest: LipResult | null;
  attempts: number;
}

export interface LipEngine {
  backend: string;
  device: string;
  wav2lip_available: boolean;
  syncnet_available: boolean;
  remote_configured: boolean;
  remote_model: string | null;
  gpu_detected: string | null;
  models_evaluated: { name: string; license: string; vram: string; status: string; resolution: string }[];
  disclosure: string;
}

export interface LipBundle {
  video_id: string;
  language: LipLanguage;
  fps: number | null;
  width: number | null;
  height: number | null;
  engine: LipEngine;
  lines: LipLine[];
  job: Job | null;
}

export interface FaceAnalysis {
  segment_id: string;
  window_start_ms: number;
  window_end_ms: number;
  frames: number;
  faces: FaceSummary[];
  chosen_track: number | null;
  source: string;
  flags: LipFlag[];
  frame_time_ms: number;
  frame_url: string;
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
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const lipsyncApi = {
  bundle: (videoId: string, language: LipLanguage) => call<LipBundle>(`/api/v1/videos/${enc(videoId)}/lipsync?language=${language}`),
  analyze: (videoId: string, segmentId: string, language: LipLanguage) =>
    call<FaceAnalysis>(`/api/v1/videos/${enc(videoId)}/lipsync/segments/${enc(segmentId)}/analyze?language=${language}`, { method: "POST" }),
  chooseFace: (videoId: string, segmentId: string, body: { time_ms: number; box?: number[] | null; off_screen?: boolean; character_id?: string | null }) =>
    call<unknown>(`/api/v1/videos/${enc(videoId)}/lipsync/segments/${enc(segmentId)}/face`, { method: "PUT", body: JSON.stringify(body) }),
  clearFace: (videoId: string, segmentId: string) =>
    call<void>(`/api/v1/videos/${enc(videoId)}/lipsync/segments/${enc(segmentId)}/face`, { method: "DELETE" }),
  generate: (videoId: string, language: LipLanguage, segmentIds: string[]) =>
    call<Job>(`/api/v1/videos/${enc(videoId)}/lipsync/generate`, { method: "POST", body: JSON.stringify({ language, segment_ids: segmentIds }) }),
  review: (resultId: string, decision: "accepted" | "rejected", note?: string) =>
    call<LipResult>(`/api/v1/lipsync/results/${enc(resultId)}/review`, { method: "POST", body: JSON.stringify({ decision, note }) }),
  url: (path: string) => `${API_BASE}${path}`,
};
