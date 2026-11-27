// Phase 12 — mixing studio API client.
import { ApiError } from "./api";
import type { Job } from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
const enc = encodeURIComponent;

export type MixTarget = "original" | "ur" | "hi";
export type MixRole = "original_dialogue" | "urdu_dub" | "hindi_dub" | "music" | "sfx" | "ambience";

export interface TrackSettings {
  volume_db: number;
  muted: boolean;
  solo: boolean;
  pan: number;
  enabled: boolean;
  gain_db: number;
  eq: { enabled: boolean; highpass_hz: number; low_gain_db: number; mid_freq_hz: number; mid_gain_db: number; high_gain_db: number };
  noise_reduction: { enabled: boolean; amount_db: number };
  compressor: { enabled: boolean; threshold_db: number; ratio: number; attack_ms: number; release_ms: number; makeup_db: number };
  fade_in_ms: number;
  fade_out_ms: number;
}

export interface MixSettings {
  version: number;
  keep_original_dialogue_in_dub: boolean;
  ducking: { enabled: boolean; threshold_db: number; ratio: number; attack_ms: number; release_ms: number };
  master: {
    gain_db: number;
    limiter: { enabled: boolean; ceiling_db: number };
    loudness: { enabled: boolean; target_lufs: number };
    fade_in_ms: number;
    fade_out_ms: number;
  };
  tracks: Record<MixRole, TrackSettings>;
}

export interface SettingsPayload {
  project_id: string;
  settings: MixSettings;
  revision: number;
  history: { reason: string; at: string; revision: number }[];
  updated_at: string | null;
}

export interface StudioTrack {
  role: MixRole;
  label: string;
  kind: "dialogue" | "bed";
  available: boolean;
  url: string | null;
  origin: string | null;
  stale: boolean;
  reason: string | null;
  included: Record<MixTarget, boolean>;
}

export interface MixPresetItem {
  id: string;
  name: string;
  settings: MixSettings;
  updated_at: string | null;
}

export interface MixRenderItem {
  id: string;
  target: MixTarget;
  format: "wav" | "aac";
  status: string;
  tracks: { role: MixRole; label: string; origin: string | null }[];
  size_bytes: number | null;
  duration_ms: number | null;
  stats: {
    integrated_lufs?: number | null;
    true_peak_dbfs?: number | null;
    lra?: number | null;
    duration_s?: number | null;
    sample_rate?: number | null;
    channels?: string | null;
    loudness_gain_db?: number | null;
    target_lufs?: number | null;
    ok?: boolean;
    problems?: string[];
  };
  error_message: string | null;
  url: string | null;
  download_url: string | null;
  created_at: string | null;
}

export interface StudioBundle {
  video_id: string;
  project_id: string;
  duration_ms: number | null;
  settings: SettingsPayload;
  tracks: StudioTrack[];
  original_full: { available: boolean; url: string | null };
  presets: MixPresetItem[];
  renders: MixRenderItem[];
  job: Job | null;
  live_controls: string[];
  render_only_controls: string[];
  sample_rate: number;
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

export const mixStudioApi = {
  bundle: (videoId: string) => call<StudioBundle>(`/api/v1/videos/${enc(videoId)}/mix-studio`),
  save: (projectId: string, settings: MixSettings, baseRevision?: number) =>
    call<SettingsPayload>(`/api/v1/projects/${enc(projectId)}/mix-studio/settings`, {
      method: "PUT",
      body: JSON.stringify({ settings, base_revision: baseRevision }),
    }),
  reset: (projectId: string) => call<SettingsPayload>(`/api/v1/projects/${enc(projectId)}/mix-studio/settings/reset`, { method: "POST" }),
  restorePrevious: (projectId: string) =>
    call<SettingsPayload>(`/api/v1/projects/${enc(projectId)}/mix-studio/settings/restore-previous`, { method: "POST" }),
  savePreset: (projectId: string, name: string, settings: MixSettings, overwrite: boolean) =>
    call<MixPresetItem>(`/api/v1/projects/${enc(projectId)}/mix-studio/presets`, {
      method: "POST",
      body: JSON.stringify({ name, settings, overwrite }),
    }),
  applyPreset: (projectId: string, presetId: string) =>
    call<SettingsPayload>(`/api/v1/projects/${enc(projectId)}/mix-studio/presets/${enc(presetId)}/apply`, { method: "POST" }),
  deletePreset: (projectId: string, presetId: string) =>
    call<void>(`/api/v1/projects/${enc(projectId)}/mix-studio/presets/${enc(presetId)}`, { method: "DELETE" }),
  render: (videoId: string, target: MixTarget, format: "wav" | "aac") =>
    call<Job>(`/api/v1/videos/${enc(videoId)}/mix-studio/render`, { method: "POST", body: JSON.stringify({ target, format }) }),
  deleteRender: (renderId: string) => call<void>(`/api/v1/mix-renders/${enc(renderId)}`, { method: "DELETE" }),
  async exactPreview(videoId: string, target: MixTarget, startMs: number, lengthMs: number, settings: MixSettings): Promise<string> {
    const response = await fetch(`${API_BASE}/api/v1/videos/${enc(videoId)}/mix-studio/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, start_ms: Math.max(0, Math.round(startMs)), length_ms: lengthMs, settings }),
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
  url: (path: string) => `${API_BASE}${path}`,
};

/** Same inclusion rule as the backend render (target, availability, enable, mute, solo). */
export function includedRoles(settings: MixSettings, tracks: StudioTrack[], target: MixTarget): MixRole[] {
  const dubFor: Record<MixTarget, MixRole | null> = { original: null, ur: "urdu_dub", hi: "hindi_dub" };
  const candidates = tracks
    .filter((track) => track.available)
    .filter((track) => !(track.role === "urdu_dub" || track.role === "hindi_dub") || dubFor[target] === track.role)
    .filter((track) => track.role !== "original_dialogue" || target === "original" || settings.keep_original_dialogue_in_dub)
    .map((track) => track.role);
  const soloed = candidates.filter((role) => settings.tracks[role].solo && settings.tracks[role].enabled);
  const pool = soloed.length ? soloed : candidates;
  return pool.filter((role) => settings.tracks[role].enabled && !settings.tracks[role].muted);
}
