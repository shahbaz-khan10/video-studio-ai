// Phase 14 - professional timeline editor + external audio import API client.
import { ApiError } from "./api";
import type { Job } from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
const enc = encodeURIComponent;

export type TrackKind =
  | "video"
  | "original_english"
  | "urdu_dub"
  | "hindi_dub"
  | "background_music"
  | "sound_effects"
  | "ambience"
  | "external_audio"
  | "lip_sync";

export type ClipSourceType =
  | "video"
  | "audio_stem"
  | "dub_take"
  | "dub_track"
  | "external_audio"
  | "original_audio"
  | "lip_sync_video";

export interface TimelineClip {
  id: string;
  timeline_id: string;
  track_id: string;
  source_type: ClipSourceType;
  source_id: string | null;
  external_audio_id: string | null;
  storage_key: string | null;
  name: string;
  character_id: string | null;
  character_name: string | null;
  segment_id: string | null;
  language: string | null;
  start_ms: number;
  duration_ms: number;
  end_ms: number;
  source_in_ms: number;
  source_out_ms: number | null;
  source_duration_ms: number | null;
  volume_db: number;
  fade_in_ms: number;
  fade_out_ms: number;
  muted: boolean;
  locked: boolean;
  replaces_clip_id: string | null;
  metadata: Record<string, unknown>;
  voice_profile: unknown;
  replaced_by_clip_id: string | null;
  audio_url: string | null;
  waveform_url: string;
  fingerprint: string;
  updated_at: string | null;
}

export interface TimelineTrack {
  id: string;
  timeline_id: string;
  kind: TrackKind;
  name: string;
  language: string | null;
  color: string;
  position: number;
  enabled: boolean;
  muted: boolean;
  solo: boolean;
  locked: boolean;
  volume_db: number;
  pan: number;
  clips: TimelineClip[];
}

export interface Timeline {
  id: string;
  project_id: string;
  video_id: string;
  name: string;
  duration_ms: number;
  sample_rate: number;
  revision: number;
  autosave: boolean;
  tracks: TimelineTrack[];
  clip_count: number;
  can_undo: boolean;
  can_redo: boolean;
  history_cursor: number;
  history_length: number;
  saved_at: string | null;
}

export interface Waveform {
  peaks: [number, number][];
  available?: boolean;
  start_ms?: number;
  end_ms?: number;
}

export interface ExternalAudio {
  id: string;
  project_id: string;
  video_id: string | null;
  original_filename: string;
  format: string;
  content_type: string;
  size_bytes: number;
  checksum_sha256: string | null;
  duration_ms: number | null;
  sample_rate: number | null;
  channels: number | null;
  stored_unchanged: boolean;
  file_url: string;
  waveform_url: string;
  preview_url: string;
  has_waveform_overview: boolean;
  notes: string | null;
  created_at: string;
}

export interface TimelineCharacter {
  id: string;
  name: string;
  source_label: string | null;
  status: string;
  voice_profiles: { profile_id: string; language: string; version: number; engine: string; voice_id: string | null; model_id: string | null; locked: boolean; consent_status: string }[];
}

export interface DialogueSegment {
  id: string;
  position: number;
  start_ms: number;
  end_ms: number;
  text: string;
  speaker: string | null;
  language: string | null;
}

export interface LipSyncSegment {
  id: string;
  result_id: string;
  video_id: string;
  segment_id: string | null;
  language: string | null;
  character_id: string | null;
  window_start_ms: number | null;
  window_end_ms: number | null;
  status: string;
  review: string;
  after_url: string | null;
  output_url: string | null;
  synced: boolean;
  needs_regeneration: boolean;
  regeneration_reason: string | null;
  engine: string | null;
}

export interface ExportPlan {
  timeline_id: string;
  mode: string;
  container: string;
  duration_ms: number;
  languages_selected: string[];
  never_combines_languages: boolean;
  original_video_preserved: boolean;
  outputs: unknown[];
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

export const timelineApi = {
  url: (path: string) => `${API_BASE}${path}`,
  create: (videoId: string, name?: string) => call<Timeline>(`/api/v1/videos/${enc(videoId)}/timeline`, { method: "POST", body: JSON.stringify({ name: name ?? "Main timeline" }) }),
  getOrCreate: (videoId: string) => call<Timeline>(`/api/v1/videos/${enc(videoId)}/timeline`),
  get: (timelineId: string) => call<Timeline>(`/api/v1/timeline/${enc(timelineId)}`),
  save: (timelineId: string) => call<Timeline>(`/api/v1/timeline/${enc(timelineId)}/save`, { method: "POST" }),
  undo: (timelineId: string) => call<Timeline>(`/api/v1/timeline/${enc(timelineId)}/undo`, { method: "POST" }),
  redo: (timelineId: string) => call<Timeline>(`/api/v1/timeline/${enc(timelineId)}/redo`, { method: "POST" }),
  segments: (videoId: string) => call<DialogueSegment[]>(`/api/v1/videos/${enc(videoId)}/timeline/segments`),
  characters: (projectId: string) => call<TimelineCharacter[]>(`/api/v1/projects/${enc(projectId)}/timeline-characters`),
  // clips
  moveClip: (clipId: string, startMs: number, trackId?: string | null, snapMs = 0) =>
    call<TimelineClip>(`/api/v1/timeline/clips/${enc(clipId)}/move`, { method: "PATCH", body: JSON.stringify({ start_ms: Math.round(startMs), track_id: trackId ?? null, snap_ms: snapMs }) }),
  trimClip: (clipId: string, body: { source_in_ms?: number; source_out_ms?: number; start_ms?: number; duration_ms?: number; snap_ms?: number }) =>
    call<TimelineClip>(`/api/v1/timeline/clips/${enc(clipId)}/trim`, { method: "PATCH", body: JSON.stringify(body) }),
  trimStart: (clipId: string, deltaMs: number, snapMs = 0) => call<TimelineClip>(`/api/v1/timeline/clips/${enc(clipId)}/trim-start`, { method: "PATCH", body: JSON.stringify({ delta_ms: Math.round(deltaMs), snap_ms: snapMs }) }),
  trimEnd: (clipId: string, deltaMs: number, snapMs = 0) => call<TimelineClip>(`/api/v1/timeline/clips/${enc(clipId)}/trim-end`, { method: "PATCH", body: JSON.stringify({ delta_ms: Math.round(deltaMs), snap_ms: snapMs }) }),
  splitClip: (clipId: string, atMs: number, snapMs = 0) => call<{ left: TimelineClip; right: TimelineClip }>(`/api/v1/timeline/clips/${enc(clipId)}/split`, { method: "POST", body: JSON.stringify({ at_ms: Math.round(atMs), snap_ms: snapMs }) }),
  joinClips: (clipIds: string[]) => call<TimelineClip>(`/api/v1/timeline/clips/join`, { method: "POST", body: JSON.stringify({ clip_ids: clipIds }) }),
  deleteClip: (clipId: string) => call<void>(`/api/v1/timeline/clips/${enc(clipId)}`, { method: "DELETE" }),
  duplicateClip: (clipId: string, startMs?: number) => call<TimelineClip>(`/api/v1/timeline/clips/${enc(clipId)}/duplicate`, { method: "POST", body: JSON.stringify(startMs == null ? {} : { start_ms: Math.round(startMs) }) }),
  updateClip: (clipId: string, body: { name?: string; volume_db?: number; fade_in_ms?: number; fade_out_ms?: number; muted?: boolean; locked?: boolean }) =>
    call<TimelineClip>(`/api/v1/timeline/clips/${enc(clipId)}`, { method: "PATCH", body: JSON.stringify(body) }),
  reassignCharacter: (clipId: string, characterId: string | null) => call<TimelineClip>(`/api/v1/timeline/clips/${enc(clipId)}/character`, { method: "PATCH", body: JSON.stringify({ character_id: characterId }) }),
  assignSegment: (clipId: string, segmentId: string | null) => call<TimelineClip>(`/api/v1/timeline/clips/${enc(clipId)}/segment`, { method: "PATCH", body: JSON.stringify({ segment_id: segmentId }) }),
  clipWaveform: (clipId: string, buckets = 200) => call<Waveform>(`/api/v1/timeline/clips/${enc(clipId)}/waveform?buckets=${buckets}`),
  addExternal: (timelineId: string, body: { external_audio_id: string; start_ms?: number; track_id?: string; source_in_ms?: number; source_out_ms?: number; character_id?: string; segment_id?: string; volume_db?: number; name?: string; replace_clip_id?: string; snap_ms?: number }) =>
    call<TimelineClip>(`/api/v1/timeline/${enc(timelineId)}/clips/external`, { method: "POST", body: JSON.stringify(body) }),
  replaceWithExternal: (timelineId: string, clipId: string, body: { external_audio_id: string; snap_ms?: number }) =>
    call<TimelineClip>(`/api/v1/timeline/${enc(timelineId)}/clips/${enc(clipId)}/replace-with-external`, { method: "POST", body: JSON.stringify(body) }),
  // tracks
  addTrack: (timelineId: string, kind: TrackKind, name: string, language?: string | null) => call<TimelineTrack>(`/api/v1/timeline/${enc(timelineId)}/tracks`, { method: "POST", body: JSON.stringify({ kind, name, language: language ?? null }) }),
  updateTrack: (trackId: string, body: Partial<Pick<TimelineTrack, "name" | "enabled" | "muted" | "solo" | "locked" | "volume_db" | "pan" | "language">>) =>
    call<TimelineTrack>(`/api/v1/timeline/tracks/${enc(trackId)}`, { method: "PATCH", body: JSON.stringify(body) }),
  // external audio
  listImports: (projectId: string) => call<ExternalAudio[]>(`/api/v1/projects/${enc(projectId)}/audio-imports`),
  uploadImport: async (projectId: string, file: File, videoId?: string): Promise<ExternalAudio> => {
    const form = new FormData();
    form.append("file", file);
    if (videoId) form.append("video_id", videoId);
    const response = await fetch(`${API_BASE}/api/v1/projects/${enc(projectId)}/audio-imports`, { method: "POST", body: form });
    if (!response.ok) throw new ApiError(`Upload failed (${response.status})`, "HTTP_ERROR", response.status);
    return (await response.json()) as ExternalAudio;
  },
  importWaveform: (audioId: string, startMs = 0, endMs = 30000, buckets = 200) =>
    call<Waveform>(`/api/v1/audio-imports/${enc(audioId)}/waveform?start_ms=${startMs}&end_ms=${endMs}&buckets=${buckets}`),
  // lip sync
  lipSync: (videoId: string, language?: string) => call<LipSyncSegment[]>(`/api/v1/videos/${enc(videoId)}/lip-sync-segments${language ? `?language=${language}` : ""}`),
  // export
  exportPlan: (timelineId: string, body: { mode?: string; container?: string; languages?: string[]; include_beds?: boolean; include_original_english?: boolean }) =>
    call<ExportPlan>(`/api/v1/timeline/${enc(timelineId)}/export-plan`, { method: "POST", body: JSON.stringify(body) }),
  exportRender: (timelineId: string, body: { mode?: string; container?: string; languages?: string[]; include_beds?: boolean }) =>
    call<Job>(`/api/v1/timeline/${enc(timelineId)}/export`, { method: "POST", body: JSON.stringify(body) }),
};

export const TRACK_KIND_LABELS: Record<TrackKind, string> = {
  video: "Original Video",
  original_english: "Original English Dialogue",
  urdu_dub: "Urdu Dub",
  hindi_dub: "Hindi Dub",
  background_music: "Background Music",
  sound_effects: "Sound Effects",
  ambience: "Ambience",
  external_audio: "External Audio",
  lip_sync: "Lip Sync",
};
