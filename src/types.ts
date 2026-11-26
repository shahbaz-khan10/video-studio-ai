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
  audio_stream_count: number;
  subtitle_streams: Record<string, unknown>[];
  aspect_ratio: string | null;
  stream_validation: Record<string, unknown>;
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

export interface AudioStem {
  role: string;
  label: string;
  status: string;
  asset_id: string | null;
  version: number | null;
  mime_type: string | null;
  size_bytes: number | null;
  url: string | null;
  download_url: string | null;
  duration_ms: number | null;
  sample_rate: number | null;
  channels: number | null;
  confidence: string | null;
  validation: Record<string, unknown>;
  comparison: Record<string, unknown>;
}

export interface AudioBundle {
  video_id: string;
  status: string;
  job: Job | null;
  source: AudioStem | null;
  stems: Record<string, AudioStem>;
  backend: string | null;
  model: string | null;
  confidence: string | null;
  limitations: string[];
  notes: string[];
  comparison: Record<string, unknown>;
}

export interface TranscriptWord {
  start_ms: number;
  end_ms: number;
  text: string;
  confidence: number | null;
}

export interface TranscriptSegment {
  id: string;
  position: number;
  start_ms: number;
  end_ms: number;
  text: string;
  confidence: number | null;
  language: string | null;
  speaker: string | null;
  words: TranscriptWord[];
  edited: boolean;
  words_stale: boolean;
}

export interface Transcript {
  metadata_json?: Record<string, unknown>;
  id: string | null;
  video_id: string;
  job_id: string | null;
  version: number;
  status: string;
  language: string;
  source: string | null;
  backend: string | null;
  model: string | null;
  word_timestamps: boolean;
  revision: number;
  segments: TranscriptSegment[];
  limitations: string[];
  notes: string[];
  updated_at: string | null;
}

export interface TranscriptionRequest {
  language: string;
  prefer_dialogue_stem: boolean;
  word_timestamps: boolean;
}

export interface TranscriptSegmentUpdate {
  id?: string | null;
  start_ms: number;
  end_ms: number;
  text: string;
  speaker?: string | null;
}

export interface TranscriptUpdate {
  revision?: number | null;
  segments: TranscriptSegmentUpdate[];
}

export interface MixerTrackSettings {
  volume: number;
  muted: boolean;
  solo: boolean;
  enabled: boolean;
}

export interface MixerTrackState extends MixerTrackSettings {
  role: string;
  label: string;
}

export interface AudioMixSettings {
  project_id: string;
  master_volume: number;
  preview_enabled: boolean;
  tracks: Record<string, MixerTrackState>;
  updated_at: string | null;
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
  audio?: Record<string, unknown>;
  transcription?: Record<string, unknown>;
  characters?: Record<string, unknown>;
  voice?: Record<string, unknown>;
}

export interface Character {
  id: string;
  project_id: string;
  name: string;
  source_label: string | null;
  status: string;
  confidence: number | null;
  notes: string | null;
  voice_characteristics: Record<string, unknown>;
  dialogue_count: number;
  scene_count: number;
  created_at: string;
  updated_at: string;
}

export interface SpeakerTurn {
  id: string;
  video_id: string;
  character_id: string | null;
  label: string;
  start_ms: number;
  end_ms: number;
  confidence: number | null;
  overlap_possible: boolean;
  reviewed: boolean;
  assignment_source: string;
  created_at: string;
}

export interface FaceTrack {
  id: string;
  video_id: string;
  character_id: string | null;
  label: string;
  start_ms: number;
  end_ms: number;
  confidence: number | null;
  representative_url: string | null;
  sample_count: number;
  created_at: string;
}

export interface VisualCandidate {
  id: string;
  video_id: string;
  character_id: string | null;
  face_track_id: string | null;
  segment_id: string | null;
  timestamp_ms: number;
  bbox: number[];
  confidence: number | null;
  frame_url: string | null;
  confirmed_character_id: string | null;
  confirmed: boolean;
  rejected: boolean;
  notes: string | null;
  created_at: string;
}

export interface CharacterAssignment {
  id: string;
  character_id: string;
  segment_id: string;
  video_id: string;
  source: string;
  confidence: number | null;
  reviewed: boolean;
  created_at: string;
  updated_at: string;
}

export interface DialogueItem {
  segment_id: string;
  character_id: string | null;
  start_ms: number;
  end_ms: number;
  text: string;
  source: string;
  confidence: number | null;
  reviewed: boolean;
}

export interface CharacterBundle {
  video_id: string;
  status: string;
  job: Job | null;
  characters: Character[];
  speaker_turns: SpeakerTurn[];
  face_tracks: FaceTrack[];
  visual_candidates: VisualCandidate[];
  assignments: CharacterAssignment[];
  dialogue: DialogueItem[];
  voice_profiles?: Record<string, unknown>[];
  limitations: string[];
  notes: string[];
  diarization: Record<string, unknown>;
  vision: Record<string, unknown>;
  metadata_json: Record<string, unknown>;
  updated_at: string | null;
}

export interface CharacterDetectionRequest {
  prefer_dialogue_stem?: boolean;
  detect_faces?: boolean;
  max_candidates?: number;
}

export interface CharacterUpdate {
  name?: string | null;
  notes?: string | null;
  status?: string | null;
}

export interface CharacterMergeRequest {
  character_ids: string[];
  name?: string | null;
}

export interface CharacterSplitRequest {
  name?: string | null;
  segment_ids?: string[];
  turn_ids?: string[];
  face_track_ids?: string[];
}

export interface SegmentAssignmentUpdate {
  character_id: string | null;
  reviewed?: boolean;
  confidence?: number | null;
}

export interface VisualCandidateUpdate {
  confirmed_character_id?: string | null;
  rejected?: boolean;
  notes?: string | null;
}

// ---------------------------------------------------------------------------
// Phase 8 — translation
// ---------------------------------------------------------------------------

export type TargetLanguage = "ur" | "hi";
export type SpeechRegister = "auto" | "formal" | "neutral" | "informal" | "intimate";

export interface QcFlag {
  code: string;
  severity: "error" | "warning" | "info";
  message: string;
}

export interface DubbingFit {
  status?: "ok" | "tight" | "long" | "empty";
  duration_s?: number;
  estimated_s?: number;
  ratio?: number;
  target_syllables?: number;
  source_syllables?: number;
  length_vs_source?: number | null;
}

export interface TranslationContextInfo {
  scene_index?: number;
  scene_note?: string | null;
  register?: string;
  register_reason?: string;
  tone?: string;
  humor?: boolean;
  speaker?: { name?: string | null; label?: string | null; gender?: string; style?: string | null };
  listener?: { name?: string | null; gender?: string; reason?: string | null };
  relation?: string | null;
  idioms?: { idiom: string; strategy: string; rendering: string }[];
  locked_terms?: string[];
  adjustments?: string[];
  refined_from?: string;
  memory?: boolean;
  placeholder_fallback?: string[];
  previous?: { speaker: string; text: string }[];
  following?: { speaker: string; text: string }[];
}

export interface TranslationHistoryEntry {
  text: string;
  roman_text: string | null;
  origin: string;
  at: string;
  reason: string;
}

export interface SegmentTranslationItem {
  id: string;
  language: TargetLanguage;
  text: string;
  roman_text: string | null;
  origin: "ai" | "manual" | "memory";
  roman_origin: string | null;
  status: string;
  ai_text: string | null;
  ai_roman_text: string | null;
  raw_mt: string | null;
  suggestion_available: boolean;
  source_text: string;
  stale: boolean;
  engine: string | null;
  model: string | null;
  qc_flags: QcFlag[];
  fit: DubbingFit;
  context: TranslationContextInfo;
  history: TranslationHistoryEntry[];
  revision: number;
  error_message: string | null;
  updated_at: string | null;
}

export interface TranslationLine {
  segment_id: string;
  position: number;
  start_ms: number;
  end_ms: number;
  text: string;
  speaker_label: string | null;
  character_id: string | null;
  character_name: string | null;
  scene_index: number;
  scene_start: boolean;
  translations: Record<TargetLanguage, SegmentTranslationItem | null>;
}

export interface TranslationProfile {
  character_id: string;
  name: string;
  status: string;
  gender: "unknown" | "male" | "female";
  register: SpeechRegister;
  speaking_style: string | null;
  name_ur: string | null;
  name_hi: string | null;
  name_roman: string | null;
  names_auto: boolean;
}

export interface CharacterRelationshipItem {
  id?: string;
  speaker_character_id: string;
  listener_character_id: string;
  address_form: "formal" | "informal" | "intimate";
  relation: string | null;
}

export interface GlossaryTerm {
  id?: string;
  source: string;
  ur: string | null;
  hi: string | null;
  roman: string | null;
  case_sensitive: boolean;
  notes: string | null;
}

export interface TranslationEngineInfo {
  backend: string;
  model: string;
  label: string;
  license: string;
  commercial_use: boolean | null;
  device: string;
  available: boolean;
  cached: boolean;
  loaded: boolean;
  refiner_configured: boolean;
  refiner_model: string | null;
  requirements: { ram_cpu_gb: number; vram_gb: number; download_gb: number; params: string };
  notes: string;
}

export interface TranslationSummary {
  total: number;
  translated: number;
  manual: number;
  stale: number;
  flagged: number;
  timing_long: number;
}

export interface TranslationBundle {
  video_id: string;
  project_id: string;
  transcript_id: string | null;
  transcript_revision: number | null;
  engine: TranslationEngineInfo;
  job: Job | null;
  profiles: TranslationProfile[];
  relationships: CharacterRelationshipItem[];
  glossary: GlossaryTerm[];
  scene_notes: Record<string, string>;
  lines: TranslationLine[];
  summary: Partial<Record<TargetLanguage, TranslationSummary>>;
}

export interface TranslateAllRequest {
  languages: TargetLanguage[];
  overwrite_manual?: boolean;
  only_missing?: boolean;
  segment_ids?: string[] | null;
}

// ---------------------------------------------------------------------------
// Phase 9 — character voice profiles
// ---------------------------------------------------------------------------

export type VoiceLanguage = TargetLanguage;
export type VoiceGender = "unknown" | "male" | "female";
export type VoiceAge = "young" | "adult" | "mature";
export type VoicePitch = "low" | "medium" | "high";
export type VoiceTone = "soft" | "deep" | "heavy" | "light" | "neutral";
export type VoiceEmotion = "neutral" | "calm" | "happy" | "sad" | "angry" | "tense" | "warm" | "serious";
export type VoiceConsentStatus = "none" | "pending" | "granted" | "revoked";

export interface VoiceCharacteristics {
  gender: VoiceGender;
  age: VoiceAge;
  pitch: VoicePitch;
  tone: VoiceTone;
  speed: number;
  accent: string | null;
  intensity: number;
  emotion: VoiceEmotion;
}

export interface VoiceEngineParams {
  length_scale?: number | null;
  noise_scale?: number | null;
  noise_w_scale?: number | null;
  speaker_id?: number | null;
  volume?: number | null;
  seed?: number | null;
}

export interface VoiceProfileUpdate {
  language: VoiceLanguage;
  engine?: string | null;
  voice_id?: string | null;
  model_id?: string | null;
  characteristics: VoiceCharacteristics;
  params: VoiceEngineParams;
  preset_id?: string | null;
  reference_id?: string | null;
  notes?: string | null;
  locked?: boolean | null;
}

export interface VoicePresetIn {
  name: string;
  language: VoiceLanguage;
  engine: string;
  model_id?: string | null;
  voice_id?: string | null;
  characteristics: VoiceCharacteristics;
  params: VoiceEngineParams;
}

export interface VoicePresetOut extends VoicePresetIn {
  id: string;
  project_id: string;
  created_at: string;
  updated_at: string;
}

export interface CharacterVoiceProfileOut {
  id: string;
  project_id: string;
  character_id: string | null;
  language: string;
  version: number;
  active: boolean;
  locked: boolean;
  engine: string;
  engine_label: string;
  model_id: string | null;
  voice_id: string | null;
  voice_label: string | null;
  license: string | null;
  commercial_use: boolean | null;
  characteristics: VoiceCharacteristics;
  params: VoiceEngineParams;
  supported_controls: string[];
  ignored_controls: string[];
  reference_id: string | null;
  reference_url: string | null;
  consent_status: VoiceConsentStatus;
  consent_at: string | null;
  revoked_at: string | null;
  usable: boolean;
  preset_id: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface VoiceReferenceOut {
  id: string;
  project_id: string;
  character_id: string;
  language: string;
  filename: string;
  url: string;
  duration_ms: number | null;
  sample_rate: number | null;
  channels: number | null;
  rms: number | null;
  peak: number | null;
  consent_status: VoiceConsentStatus;
  consent_text_version: string | null;
  consent_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface VoiceReferenceUploadOut {
  reference: VoiceReferenceOut;
  profile: CharacterVoiceProfileOut;
}

export interface VoiceOptionOut {
  id: string;
  label: string;
  language: string;
  gender: string;
  quality: string | null;
  sample_rate: number | null;
  license: string;
  commercial_use: boolean | null;
  cached: boolean;
  preview_available: boolean;
  supports_reference: boolean;
  supports_speaker_id?: boolean;
  source: string;
}

export interface VoiceCapabilityOut {
  key: string;
  label: string;
  supported: boolean;
}

export interface VoiceEngineInfoOut {
  backend: string;
  selected: string;
  label: string;
  license: string;
  commercial_use: boolean | null;
  device: string;
  available: boolean;
  loaded: boolean;
  languages: string[];
  voices: VoiceOptionOut[];
  capabilities: VoiceCapabilityOut[];
  requirements: Record<string, unknown>;
  notes: string;
  consent_required: boolean;
}

export interface VoiceProfileBundleOut {
  project_id: string;
  engine: VoiceEngineInfoOut;
  profiles: CharacterVoiceProfileOut[];
  presets: VoicePresetOut[];
  references: VoiceReferenceOut[];
  consent_policy: { version: string; statement: string; required: boolean };
  sample_limits: {
    max_mb: number;
    min_s: number;
    max_s: number;
    min_sample_rate: number;
    formats: string[];
  };
  limitations: string[];
}

export interface VoiceCatalogItem {
  id: string;
  language: string;
  gender: string;
  license: string;
}

export interface VoicePreviewRequest {
  character_id?: string | null;
  language: VoiceLanguage;
  text: string;
  profile_id?: string | null;
  profile?: VoiceProfileUpdate | null;
}
