// Phase 14 - professional multitrack timeline editor + external audio import.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, PointerEvent as ReactPointerEvent, RefObject } from "react";
import { formatTimestamp } from "../lib/format";
import {
  timelineApi,
  TRACK_KIND_LABELS,
  type DialogueSegment,
  type ExternalAudio,
  type LipSyncSegment,
  type Timeline,
  type TimelineClip,
  type TimelineCharacter,
  type TimelineTrack,
  type Waveform,
} from "../lib/timelineApi";
import "./TimelineEditor.css";

interface TimelineEditorProps {
  projectId: string;
  videoId: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  videoDurationMs?: number | null;
}

const MIN_PX_PER_MS = 0.005;
const MAX_PX_PER_MS = 2;
const LABEL_WIDTH = 216;
const AUDIO_FORMATS = ".wav,.mp3,.aac,.m4a,.flac,.ogg,audio/wav,audio/mpeg,audio/aac,audio/mp4,audio/flac,audio/ogg";

function clamp(v: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, v));
}

// ---------------------------------------------------------------------------
// Waveform canvas
// ---------------------------------------------------------------------------

function WaveCanvas({ wave, color, height, width }: { wave: Waveform | null; color: string; height: number; width: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, width * ratio);
    canvas.height = height * ratio;
    const g = canvas.getContext("2d");
    if (!g) return;
    g.scale(ratio, ratio);
    g.clearRect(0, 0, width, height);
    const mid = height / 2;
    g.fillStyle = color;
    const peaks = wave?.peaks ?? [];
    const n = peaks.length;
    if (n === 0) return;
    peaks.forEach(([lo, hi], i) => {
      const x = (i / n) * width;
      const top = mid - Math.max(0.5, hi * mid);
      const bottom = mid - Math.min(-0.5, lo * mid);
      g.fillRect(x, top, Math.max(1, width / n - 0.3), bottom - top);
    });
  }, [wave, color, height, width]);
  return <canvas ref={ref} className="tl-wave" style={{ width, height }} aria-hidden="true" />;
}

// ---------------------------------------------------------------------------
// Clip view
// ---------------------------------------------------------------------------

interface ClipViewProps {
  clip: TimelineClip;
  track: TimelineTrack;
  pxPerMs: number;
  selected: boolean;
  color: string;
  onSelect: (id: string) => void;
  onMoveStart: (clip: TimelineClip, event: ReactPointerEvent) => void;
  onTrimStart: (clip: TimelineClip, edge: "start" | "end", event: ReactPointerEvent) => void;
  lipSync?: LipSyncSegment | null;
}

function ClipView({ clip, track, pxPerMs, selected, color, onSelect, onMoveStart, onTrimStart, lipSync }: ClipViewProps) {
  const width = Math.max(2, clip.duration_ms * pxPerMs);
  const left = clip.start_ms * pxPerMs;
  const isAudio = track.kind !== "video" && clip.source_type !== "video";
  const [wave, setWave] = useState<Waveform | null>(null);
  const buckets = clamp(Math.round(width / 3), 16, 400);

  useEffect(() => {
    let cancelled = false;
    if (!isAudio) return;
    timelineApi
      .clipWaveform(clip.id, buckets)
      .then((w) => !cancelled && setWave(w))
      .catch(() => !cancelled && setWave(null));
    return () => {
      cancelled = true;
    };
  }, [clip.id, clip.source_in_ms, clip.source_out_ms, clip.duration_ms, buckets, isAudio]);

  const fadeInPx = (clip.fade_in_ms * pxPerMs);
  const fadeOutPx = (clip.fade_out_ms * pxPerMs);

  return (
    <div
      className={`tl-clip ${selected ? "is-selected" : ""} ${clip.muted ? "is-muted" : ""} ${clip.locked ? "is-locked" : ""} ${lipSync?.needs_regeneration ? "is-stale" : ""}`}
      style={{ left, width, borderColor: color }}
      onPointerDown={() => onSelect(clip.id)}
      onClick={() => onSelect(clip.id)}
      role="button"
      tabIndex={0}
      aria-label={`${clip.name} ${formatTimestamp(clip.start_ms)} to ${formatTimestamp(clip.start_ms + clip.duration_ms)}`}
    >
      {/* trim handles + move */}
      <span className="tl-clip-trim tl-clip-trim-start" onPointerDown={(event) => onTrimStart(clip, "start", event)} aria-label="Trim start" />
      <span className="tl-clip-trim tl-clip-trim-end" onPointerDown={(event) => onTrimStart(clip, "end", event)} aria-label="Trim end" />
      <span className="tl-clip-move" onPointerDown={(event) => onMoveStart(clip, event)} aria-label="Move clip" />
      <div className="tl-clip-body">
        <span className="tl-clip-name" title={clip.name}>{clip.name}</span>
        {clip.character_name && <span className="tl-clip-char">{clip.character_name}</span>}
        {isAudio && <WaveCanvas wave={wave} color={color} height={28} width={Math.max(2, width - 8)} />}
        {/* fades */}
        {clip.fade_in_ms > 0 && <span className="tl-fade tl-fade-in" style={{ width: fadeInPx }} />}
        {clip.fade_out_ms > 0 && <span className="tl-fade tl-fade-out" style={{ width: fadeOutPx }} />}
        {clip.muted && <span className="tl-clip-badge">M</span>}
        {clip.locked && <span className="tl-clip-badge tl-clip-badge-lock">L</span>}
        {lipSync && (
          <span className={`tl-clip-badge tl-clip-badge-lipsync ${lipSync.needs_regeneration ? "is-stale" : ""}`}
            title={lipSync.needs_regeneration ? `Lip sync needs regeneration: ${lipSync.regeneration_reason ?? ""}` : "Lip sync synced"}>
            {lipSync.needs_regeneration ? "⚠ LS" : "LS"}
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Time ruler
// ---------------------------------------------------------------------------

function Ruler({ durationMs, pxPerMs, width }: { durationMs: number; pxPerMs: number; width: number }) {
  const ticks = useMemo(() => {
    // adaptive tick spacing in ms
    const candidates = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000];
    const minPx = 72;
    const step = candidates.find((c) => c * pxPerMs >= minPx) ?? candidates[candidates.length - 1];
    const out: { ms: number; major: boolean }[] = [];
    const total = Math.max(durationMs, step);
    for (let ms = 0; ms <= total + step; ms += step) out.push({ ms, major: true });
    return { out, step };
  }, [durationMs, pxPerMs]);
  return (
    <div className="tl-ruler" style={{ width }} aria-label="Time ruler">
      {ticks.out.map((tick) => (
        <div key={tick.ms} className="tl-tick" style={{ left: tick.ms * pxPerMs }}>
          <span className="tl-tick-label">{formatTimestamp(tick.ms)}</span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

export function TimelineEditor({ projectId, videoId, videoRef, videoDurationMs }: TimelineEditorProps) {
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pxPerMs, setPxPerMs] = useState(0.05);
  const [snapMs, setSnapMs] = useState(100);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null);
  const [playheadMs, setPlayheadMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [previewAudio, setPreviewAudio] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [scrollLeft, setScrollLeft] = useState(0);
  const [viewportWidth, setViewportWidth] = useState(1000);
  const scrollRef = useRef<HTMLDivElement>(null);
  const audioPoolRef = useRef(new Map<string, HTMLAudioElement>());

  // external audio import
  const [imports, setImports] = useState<ExternalAudio[]>([]);
  const [pendingImport, setPendingImport] = useState<ExternalAudio | null>(null);
  const [importPreview, setImportPreview] = useState<HTMLAudioElement | null>(null);
  const [importTrackId, setImportTrackId] = useState<string>("");
  const [importCharacterId, setImportCharacterId] = useState<string>("");
  const [importSegmentId, setImportSegmentId] = useState<string>("");
  const [importStartMs, setImportStartMs] = useState<number>(0);
  const [importVolumeDb, setImportVolumeDb] = useState<number>(0);
  const [replaceClipId, setReplaceClipId] = useState<string>("");

  const [characters, setCharacters] = useState<TimelineCharacter[]>([]);
  const [segments, setSegments] = useState<DialogueSegment[]>([]);
  const [lipSync, setLipSync] = useState<LipSyncSegment[]>([]);
  const [exportMode, setExportMode] = useState<"multitrack" | "separate_language">("multitrack");
  const [exportInfo, setExportInfo] = useState<string | null>(null);

  const effectiveSnap = snapEnabled ? snapMs : 0;

  // ---- data loading -------------------------------------------------------
  const reload = useCallback(async () => {
    try {
      const tl = await timelineApi.getOrCreate(videoId);
      setTimeline(tl);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load timeline");
    } finally {
      setLoading(false);
    }
  }, [videoId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    timelineApi.characters(projectId).then(setCharacters).catch(() => setCharacters([]));
    timelineApi.segments(videoId).then(setSegments).catch(() => setSegments([]));
    timelineApi.listImports(projectId).then(setImports).catch(() => setImports([]));
    timelineApi.lipSync(videoId).then(setLipSync).catch(() => setLipSync([]));
  }, [projectId, videoId]);

  // ---- video sync ---------------------------------------------------------
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const onTime = () => setPlayheadMs(video.currentTime * 1000);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onSeeked = onTime;
    video.addEventListener("timeupdate", onTime);
    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onPause);
    video.addEventListener("seeked", onSeeked);
    video.addEventListener("seeking", onSeeked);
    return () => {
      video.removeEventListener("timeupdate", onTime);
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("seeking", onSeeked);
    };
  }, [videoRef, timeline]);

  // ---- timeline audio preview (streaming, no full-memory decode) ----------
  useEffect(() => {
    if (!previewAudio || !timeline) return;
    const video = videoRef.current;
    const pool = audioPoolRef.current;
    const audioClips = timeline.tracks
      .filter((t) => t.kind !== "video" && t.kind !== "lip_sync" && t.enabled && !t.muted)
      .flatMap((t) => t.clips.filter((c) => !c.muted && c.audio_url).map((c) => ({ clip: c, trackVolumeDb: t.volume_db })));
    const active = new Set<string>();
    const align = () => {
      if (!video) return;
      const vt = video.currentTime * 1000;
      for (const { clip, trackVolumeDb } of audioClips) {
        const inside = vt >= clip.start_ms && vt < clip.start_ms + clip.duration_ms;
        let el = pool.get(clip.id);
        if (inside) {
          active.add(clip.id);
          if (!el) {
            el = new Audio();
            el.crossOrigin = "anonymous";
            el.preload = "auto";
            el.src = timelineApi.url(clip.audio_url!);
            pool.set(clip.id, el);
          }
          const offsetSec = (clip.source_in_ms + (vt - clip.start_ms)) / 1000;
          if (Math.abs(el.currentTime - offsetSec) > 0.12) {
            try { el.currentTime = Math.max(0, offsetSec); } catch { /* not loaded */ }
          }
          el.playbackRate = video.playbackRate || 1;
          el.volume = clamp(Math.pow(10, (clip.volume_db + trackVolumeDb) / 20), 0, 1);
          if (!video.paused && el.paused) void el.play().catch(() => undefined);
        } else if (el) {
          el.pause();
        }
      }
      // stop removed
      for (const [id, el] of pool) if (!active.has(id)) el.pause();
      if (video) video.muted = true; // preview timeline audio instead of original
    };
    const play = () => align();
    const pause = () => pool.forEach((el) => el.pause());
    const seek = () => align();
    if (video) {
      video.addEventListener("play", play);
      video.addEventListener("pause", pause);
      video.addEventListener("seeked", seek);
      video.addEventListener("timeupdate", align);
      align();
    }
    const timer = window.setInterval(align, 200);
    return () => {
      window.clearInterval(timer);
      if (video) {
        video.removeEventListener("play", play);
        video.removeEventListener("pause", pause);
        video.removeEventListener("seeked", seek);
        video.removeEventListener("timeupdate", align);
        video.muted = false;
      }
      pool.forEach((el) => { el.pause(); el.removeAttribute("src"); el.load(); });
      pool.clear();
    };
  }, [previewAudio, timeline, videoRef]);

  // ---- viewport measurement ----------------------------------------------
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setViewportWidth(el.clientWidth || 1000);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [timeline]);

  // ---- selection ----------------------------------------------------------
  const selectedClip = useMemo(() => {
    if (!timeline || !selectedClipId) return null;
    for (const track of timeline.tracks) {
      const found = track.clips.find((c) => c.id === selectedClipId);
      if (found) return found;
    }
    return null;
  }, [timeline, selectedClipId]);

  const selectedTrack = useMemo(
    () => timeline?.tracks.find((t) => t.id === selectedClip?.track_id) ?? null,
    [timeline, selectedClip],
  );

  const lipSyncBySegment = useMemo(() => {
    const map = new Map<string, LipSyncSegment>();
    for (const ls of lipSync) if (ls.segment_id) map.set(`${ls.segment_id}:${ls.language ?? ""}`, ls);
    return map;
  }, [lipSync]);

  // ---- editing helpers ----------------------------------------------------
  const apply = useCallback((next: Timeline) => {
    setTimeline(next);
    setSaveState("saved");
  }, []);

  const flashSaving = useCallback(() => setSaveState("saving"), []);

  const moveClip = useCallback(
    async (clip: TimelineClip, newStartMs: number, trackId?: string | null) => {
      if (clip.locked) return;
      flashSaving();
      try {
        await timelineApi.moveClip(clip.id, Math.max(0, newStartMs), trackId, effectiveSnap);
        await reload();
      } catch (err) {
        setSaveState("error");
        setError(err instanceof Error ? err.message : "Move failed");
      }
    },
    [effectiveSnap, reload, flashSaving],
  );

  const trimClip = useCallback(
    async (clip: TimelineClip, edge: "start" | "end", deltaMs: number) => {
      if (clip.locked) return;
      flashSaving();
      try {
        if (edge === "start") await timelineApi.trimStart(clip.id, deltaMs, effectiveSnap);
        else await timelineApi.trimEnd(clip.id, -deltaMs, effectiveSnap);
        await reload();
      } catch (err) {
        setSaveState("error");
        setError(err instanceof Error ? err.message : "Trim failed");
      }
    },
    [effectiveSnap, reload, flashSaving],
  );

  // ---- pointer dragging ---------------------------------------------------
  const dragRef = useRef<null | { clip: TimelineClip; startX: number; startMs: number; kind: "move" | "trim-start" | "trim-end"; moved: boolean }>(null);

  const onMoveStart = useCallback((clip: TimelineClip, event: ReactPointerEvent) => {
    event.stopPropagation();
    if (clip.locked) return;
    dragRef.current = { clip, startX: event.clientX, startMs: clip.start_ms, kind: "move", moved: false };
    (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
  }, []);

  const onTrimStart = useCallback((clip: TimelineClip, edge: "start" | "end", event: ReactPointerEvent) => {
    event.stopPropagation();
    if (clip.locked) return;
    dragRef.current = { clip, startX: event.clientX, startMs: clip.start_ms, kind: edge === "start" ? "trim-start" : "trim-end", moved: false };
    (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
  }, []);

  useEffect(() => {
    const onMove = () => {
      const drag = dragRef.current;
      if (!drag) return;
      drag.moved = true;
    };
    const onUp = async (event: PointerEvent) => {
      const drag = dragRef.current;
      dragRef.current = null;
      if (!drag || !drag.moved) return;
      const deltaMs = (event.clientX - drag.startX) / pxPerMs;
      if (drag.kind === "move") {
        await moveClip(drag.clip, drag.startMs + deltaMs);
      } else if (drag.kind === "trim-start") {
        await trimClip(drag.clip, "start", deltaMs);
      } else {
        await trimClip(drag.clip, "end", deltaMs);
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [pxPerMs, moveClip, trimClip]);

  // ---- playhead seek ------------------------------------------------------
  const seekTo = useCallback(
    (ms: number) => {
      const video = videoRef.current;
      const target = clamp(ms, 0, timeline?.duration_ms ?? 0);
      if (video) video.currentTime = target / 1000;
      setPlayheadMs(target);
    },
    [videoRef, timeline],
  );

  const rulerSeek = useCallback(
    (event: ReactPointerEvent) => {
      const lane = scrollRef.current;
      if (!lane || !timeline) return;
      const rect = lane.getBoundingClientRect();
      const x = event.clientX - rect.left + lane.scrollLeft - LABEL_WIDTH;
      seekTo(x / pxPerMs);
    },
    [pxPerMs, seekTo, timeline],
  );

  // ---- playback controls --------------------------------------------------
  const togglePlay = useCallback(async () => {
    const video = videoRef.current;
    if (!video) return;
    try {
      if (video.paused) { await video.play(); } else { video.pause(); }
    } catch { /* blocked */ }
  }, [videoRef]);

  const stop = useCallback(() => {
    const video = videoRef.current;
    if (video) { video.pause(); video.currentTime = 0; }
    setPlayheadMs(0);
  }, [videoRef]);

  const setPlaybackRate = useCallback(
    (value: number) => {
      setRate(value);
      const video = videoRef.current;
      if (video) video.playbackRate = value;
    },
    [videoRef],
  );

  // ---- keyboard shortcuts -------------------------------------------------
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      const meta = event.ctrlKey || event.metaKey;
      if (meta && event.key.toLowerCase() === "z" && !event.shiftKey) { event.preventDefault(); void doUndo(); }
      else if (meta && (event.key.toLowerCase() === "y" || (event.key.toLowerCase() === "z" && event.shiftKey))) { event.preventDefault(); void doRedo(); }
      else if (event.key === " ") { event.preventDefault(); void togglePlay(); }
      else if (event.key === "Delete" || event.key === "Backspace") { if (selectedClipId) void doDelete(); }
      else if (meta && event.key.toLowerCase() === "d") { event.preventDefault(); void doDuplicate(); }
      else if (event.key === "s" || event.key === "S") { if (selectedClipId) void doSplit(); }
      else if (event.key === "ArrowRight") seekTo(playheadMs + (event.shiftKey ? 1000 : 100));
      else if (event.key === "ArrowLeft") seekTo(playheadMs - (event.shiftKey ? 1000 : 100));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedClipId, playheadMs, timeline]);

  // ---- undo / redo --------------------------------------------------------
  const doUndo = useCallback(async () => {
    if (!timeline) return;
    flashSaving();
    try { apply(await timelineApi.undo(timeline.id)); } catch (err) { setSaveState("error"); setError(err instanceof Error ? err.message : "Undo failed"); }
  }, [timeline, apply, flashSaving]);

  const doRedo = useCallback(async () => {
    if (!timeline) return;
    flashSaving();
    try { apply(await timelineApi.redo(timeline.id)); } catch (err) { setSaveState("error"); setError(err instanceof Error ? err.message : "Redo failed"); }
  }, [timeline, apply, flashSaving]);

  // ---- clip actions -------------------------------------------------------
  const doDelete = useCallback(async () => {
    if (!timeline || !selectedClipId) return;
    flashSaving();
    try { await timelineApi.deleteClip(selectedClipId); setSelectedClipId(null); await reload(); } catch (err) { setSaveState("error"); }
  }, [timeline, selectedClipId, reload, flashSaving]);

  const doDuplicate = useCallback(async () => {
    if (!selectedClipId) return;
    flashSaving();
    try { await timelineApi.duplicateClip(selectedClipId); await reload(); } catch { setSaveState("error"); }
  }, [selectedClipId, reload, flashSaving]);

  const doSplit = useCallback(async () => {
    if (!selectedClip) return;
    const at = playheadMs;
    if (at <= selectedClip.start_ms || at >= selectedClip.start_ms + selectedClip.duration_ms) {
      setError("Move the playhead inside the clip to split.");
      return;
    }
    flashSaving();
    try { await timelineApi.splitClip(selectedClip.id, at, effectiveSnap); await reload(); } catch (err) { setSaveState("error"); setError(err instanceof Error ? err.message : "Split failed"); }
  }, [selectedClip, playheadMs, effectiveSnap, reload, flashSaving]);

  const doJoin = useCallback(async () => {
    // join selected + adjacent next clip on same track
    if (!timeline || !selectedClip) return;
    const track = timeline.tracks.find((t) => t.id === selectedClip.track_id);
    if (!track) return;
    const sorted = [...track.clips].sort((a, b) => a.start_ms - b.start_ms);
    const idx = sorted.findIndex((c) => c.id === selectedClip.id);
    const next = sorted[idx + 1];
    if (!next) { setError("No adjacent clip to join."); return; }
    flashSaving();
    try { await timelineApi.joinClips([selectedClip.id, next.id]); await reload(); } catch (err) { setSaveState("error"); setError(err instanceof Error ? err.message : "Join failed"); }
  }, [timeline, selectedClip, reload, flashSaving]);

  const updateSelected = useCallback(
    async (patch: { volume_db?: number; fade_in_ms?: number; fade_out_ms?: number; muted?: boolean; locked?: boolean; name?: string }) => {
      if (!selectedClip) return;
      flashSaving();
      try { await timelineApi.updateClip(selectedClip.id, patch); await reload(); } catch { setSaveState("error"); }
    },
    [selectedClip, reload, flashSaving],
  );

  const reassignCharacter = useCallback(
    async (characterId: string | null) => {
      if (!selectedClip) return;
      flashSaving();
      try { await timelineApi.reassignCharacter(selectedClip.id, characterId); await reload(); } catch { setSaveState("error"); }
    },
    [selectedClip, reload, flashSaving],
  );

  const assignSegment = useCallback(
    async (segmentId: string | null) => {
      if (!selectedClip) return;
      flashSaving();
      try { await timelineApi.assignSegment(selectedClip.id, segmentId); await reload(); } catch { setSaveState("error"); }
    },
    [selectedClip, reload, flashSaving],
  );

  const replaceWithExternal = useCallback(
    async (clipId: string, audioId: string) => {
      if (!timeline) return;
      flashSaving();
      try { await timelineApi.replaceWithExternal(timeline.id, clipId, { external_audio_id: audioId, snap_ms: effectiveSnap }); await reload(); } catch (err) { setSaveState("error"); setError(err instanceof Error ? err.message : "Replace failed"); }
    },
    [timeline, effectiveSnap, reload, flashSaving],
  );

  const toggleTrack = useCallback(
    async (track: TimelineTrack, patch: Partial<Pick<TimelineTrack, "enabled" | "muted" | "solo" | "locked">>) => {
      flashSaving();
      try { await timelineApi.updateTrack(track.id, patch); await reload(); } catch { setSaveState("error"); }
    },
    [reload, flashSaving],
  );

  const addExternalTrack = useCallback(async () => {
    if (!timeline) return;
    const count = timeline.tracks.filter((t) => t.kind === "external_audio").length + 1;
    try { await timelineApi.addTrack(timeline.id, "external_audio", `External Audio ${count}`); await reload(); } catch { /* ignore */ }
  }, [timeline, reload]);

  // ---- external import ----------------------------------------------------
  const onPickImportFile = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (!file) return;
      flashSaving();
      try {
        const row = await timelineApi.uploadImport(projectId, file, videoId);
        setImports((cur) => [row, ...cur]);
        setPendingImport(row);
        setImportStartMs(Math.round(playheadMs));
        // preview before adding
        const audio = new Audio(timelineApi.url(row.file_url));
        setImportPreview((old) => { old?.pause(); return audio; });
        setSaveState("saved");
      } catch (err) {
        setSaveState("error");
        setError(err instanceof Error ? err.message : "Import failed");
      }
    },
    [projectId, videoId, playheadMs, flashSaving],
  );

  const previewPending = useCallback(() => {
    if (!importPreview) return;
    importPreview.currentTime = 0;
    void importPreview.play().catch(() => undefined);
  }, [importPreview]);

  const stopPreview = useCallback(() => importPreview?.pause(), [importPreview]);

  const addPendingToTimeline = useCallback(async () => {
    if (!timeline || !pendingImport) return;
    flashSaving();
    try {
      await timelineApi.addExternal(timeline.id, {
        external_audio_id: pendingImport.id,
        start_ms: Math.max(0, Math.round(importStartMs)),
        track_id: importTrackId || undefined,
        character_id: importCharacterId || undefined,
        segment_id: importSegmentId || undefined,
        volume_db: importVolumeDb,
        replace_clip_id: replaceClipId || undefined,
        snap_ms: effectiveSnap,
      });
      importPreview?.pause();
      setPendingImport(null);
      setReplaceClipId("");
      await reload();
    } catch (err) {
      setSaveState("error");
      setError(err instanceof Error ? err.message : "Add failed");
    }
  }, [timeline, pendingImport, importStartMs, importTrackId, importCharacterId, importSegmentId, importVolumeDb, replaceClipId, effectiveSnap, importPreview, reload, flashSaving]);

  // ---- export -------------------------------------------------------------
  const makeExportPlan = useCallback(async () => {
    if (!timeline) return;
    try {
      const plan = await timelineApi.exportPlan(timeline.id, { mode: exportMode, container: "mkv", include_beds: true });
      const langs = plan.languages_selected.join(", ") || "none";
      setExportInfo(`${plan.mode}; separate streams per track; languages: ${langs}; languages never combined.`);
    } catch (err) { setExportInfo(err instanceof Error ? err.message : "Export plan failed"); }
  }, [timeline, exportMode]);

  // ---- rendering ----------------------------------------------------------
  if (loading) return <div className="panel tl-editor"><p className="planned-note">Loading timeline…</p></div>;
  if (error && !timeline) return <div className="panel tl-editor"><p className="form-error">{error}</p></div>;
  if (!timeline) return null;

  const durationMs = Math.max(timeline.duration_ms, videoDurationMs ?? 0, 1000);
  const totalWidth = durationMs * pxPerMs + 200;
  const visibleStart = Math.max(0, (scrollLeft - LABEL_WIDTH) / pxPerMs - 2000);
  const visibleEnd = (scrollLeft + viewportWidth) / pxPerMs + 2000;
  const snap = effectiveSnap;

  const externalTracks = timeline.tracks.filter((t) => t.kind === "external_audio");
  const dubbedClips = timeline.tracks.filter((t) => t.kind === "urdu_dub" || t.kind === "hindi_dub").flatMap((t) => t.clips);

  return (
    <div className="panel tl-editor" aria-label="Professional timeline editor">
      {/* toolbar */}
      <div className="tl-toolbar">
        <div className="tl-toolbar-group">
          <strong>Timeline</strong>
          <span className={`tl-save tl-save-${saveState}`}>
            {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Saved" : saveState === "error" ? "Save failed" : `r${timeline.revision}`}
          </span>
        </div>
        <div className="tl-toolbar-group tl-playback" role="group" aria-label="Playback">
          <button className="button button-quiet" type="button" onClick={stop} title="Stop">⏹</button>
          <button className="button button-quiet" type="button" onClick={() => seekTo(playheadMs - 1000)} title="Back 1s">⏮</button>
          <button className="button button-primary" type="button" onClick={togglePlay} title="Play/Pause (Space)">{playing ? "⏸" : "▶"}</button>
          <button className="button button-quiet" type="button" onClick={() => seekTo(playheadMs + 1000)} title="Forward 1s">⏭</button>
          <select value={rate} onChange={(e) => setPlaybackRate(Number(e.target.value))} aria-label="Playback rate">
            {[0.25, 0.5, 1, 1.5, 2].map((r) => <option key={r} value={r}>{r}×</option>)}
          </select>
          <span className="tl-time">{formatTimestamp(playheadMs)} / {formatTimestamp(durationMs)}</span>
          <label className="tl-check" title="Preview edited audio scheduled per clip">
            <input type="checkbox" checked={previewAudio} onChange={(e) => setPreviewAudio(e.target.checked)} /> Timeline audio
          </label>
        </div>
        <div className="tl-toolbar-group" role="group" aria-label="Zoom">
          <button className="button button-quiet" type="button" onClick={() => setPxPerMs((v) => clamp(v / 1.5, MIN_PX_PER_MS, MAX_PX_PER_MS))} title="Zoom out">−</button>
          <span className="tl-zoom">{Math.round(pxPerMs * 1000)} px/s</span>
          <button className="button button-quiet" type="button" onClick={() => setPxPerMs((v) => clamp(v * 1.5, MIN_PX_PER_MS, MAX_PX_PER_MS))} title="Zoom in">+</button>
          <button className="button button-quiet" type="button" onClick={() => setPxPerMs(0.05)} title="Reset zoom">Fit</button>
        </div>
        <div className="tl-toolbar-group" role="group" aria-label="Snap">
          <label className="tl-check"><input type="checkbox" checked={snapEnabled} onChange={(e) => setSnapEnabled(e.target.checked)} /> Snap</label>
          <select value={snapMs} onChange={(e) => setSnapMs(Number(e.target.value))} aria-label="Snap grid" disabled={!snapEnabled}>
            {[10, 50, 100, 250, 500, 1000].map((s) => <option key={s} value={s}>{s} ms</option>)}
          </select>
        </div>
        <div className="tl-toolbar-group" role="group" aria-label="Edit">
          <button className="button button-quiet" type="button" onClick={doUndo} disabled={!timeline.can_undo} title="Undo (Ctrl+Z)">↶ Undo</button>
          <button className="button button-quiet" type="button" onClick={doRedo} disabled={!timeline.can_redo} title="Redo (Ctrl+Y)">↷ Redo</button>
        </div>
      </div>

      {/* selected clip inspector */}
      {selectedClip && (
        <div className="tl-inspector" role="group" aria-label="Selected clip">
          <span className="tl-inspector-name" title={selectedClip.name}>{selectedClip.name}</span>
          <button className="button button-quiet" type="button" onClick={doSplit} title="Split at playhead (S)">Split</button>
          <button className="button button-quiet" type="button" onClick={doJoin} title="Join with next clip">Join</button>
          <button className="button button-quiet" type="button" onClick={doDuplicate} title="Duplicate (Ctrl+D)">Duplicate</button>
          <button className="button button-quiet" type="button" onClick={doDelete} title="Delete (Del)">Delete</button>
          <label className="tl-check">Vol
            <input type="range" min={-60} max={24} step={0.5} value={selectedClip.volume_db} onChange={(e) => void updateSelected({ volume_db: Number(e.target.value) })} />
            <span>{selectedClip.volume_db.toFixed(1)} dB</span>
          </label>
          <label className="tl-check">Fade in
            <input type="number" min={0} max={selectedClip.duration_ms} value={selectedClip.fade_in_ms} onChange={(e) => void updateSelected({ fade_in_ms: Number(e.target.value) })} />
          </label>
          <label className="tl-check">Fade out
            <input type="number" min={0} max={selectedClip.duration_ms} value={selectedClip.fade_out_ms} onChange={(e) => void updateSelected({ fade_out_ms: Number(e.target.value) })} />
          </label>
          <button className={`button button-quiet ${selectedClip.muted ? "is-on" : ""}`} type="button" onClick={() => void updateSelected({ muted: !selectedClip.muted })}>Mute</button>
          <button className={`button button-quiet ${selectedClip.locked ? "is-on" : ""}`} type="button" onClick={() => void updateSelected({ locked: !selectedClip.locked })}>Lock</button>
          {selectedTrack?.kind !== "video" && selectedClip.source_type !== "video" && (
            <>
              <label className="tl-check">Character
                <select value={selectedClip.character_id ?? ""} onChange={(e) => void reassignCharacter(e.target.value || null)}>
                  <option value="">— none —</option>
                  {characters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </label>
              <label className="tl-check">Dialogue
                <select value={selectedClip.segment_id ?? ""} onChange={(e) => void assignSegment(e.target.value || null)}>
                  <option value="">— none —</option>
                  {segments.map((s) => <option key={s.id} value={s.id}>{s.position + 1}. {s.text.slice(0, 32)}</option>)}
                </select>
              </label>
              {pendingImport && (
                <button className="button button-quiet" type="button" onClick={() => void replaceWithExternal(selectedClip.id, pendingImport.id)}>Replace with imported</button>
              )}
            </>
          )}
        </div>
      )}

      {/* external audio import */}
      <div className="tl-import" aria-label="External audio import">
        <label className="button button-quiet tl-import-btn">
          ＋ Import audio (WAV/MP3/AAC/M4A/FLAC/OGG)
          <input type="file" accept={AUDIO_FORMATS} onChange={onPickImportFile} hidden />
        </label>
        {pendingImport && (
          <div className="tl-import-preview">
            <span title={pendingImport.original_filename}>{pendingImport.original_filename}</span>
            <small>{pendingImport.format} · {pendingImport.duration_ms != null ? `${(pendingImport.duration_ms / 1000).toFixed(2)}s` : "?"} · {pendingImport.sample_rate ?? "?"} Hz{pendingImport.stored_unchanged ? " · original unchanged" : ""}</small>
            <button className="button button-quiet" type="button" onClick={previewPending}>▶ Preview</button>
            <button className="button button-quiet" type="button" onClick={stopPreview}>⏸</button>
            <label className="tl-check">Track
              <select value={importTrackId} onChange={(e) => setImportTrackId(e.target.value)}>
                <option value="">External Audio (auto)</option>
                {externalTracks.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                {timeline.tracks.filter((t) => t.kind !== "video" && t.kind !== "lip_sync").map((t) => <option key={`x${t.id}`} value={t.id}>{t.name}</option>)}
              </select>
            </label>
            <label className="tl-check">Character
              <select value={importCharacterId} onChange={(e) => setImportCharacterId(e.target.value)}>
                <option value="">— none —</option>
                {characters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </label>
            <label className="tl-check">Dialogue
              <select value={importSegmentId} onChange={(e) => setImportSegmentId(e.target.value)}>
                <option value="">— none —</option>
                {segments.map((s) => <option key={s.id} value={s.id}>{s.position + 1}. {s.text.slice(0, 28)}</option>)}
              </select>
            </label>
            <label className="tl-check">At ms
              <input type="number" min={0} value={importStartMs} onChange={(e) => setImportStartMs(Number(e.target.value))} />
            </label>
            <label className="tl-check">Volume dB
              <input type="number" value={importVolumeDb} onChange={(e) => setImportVolumeDb(Number(e.target.value))} />
            </label>
            <button className="button button-primary" type="button" onClick={addPendingToTimeline}>Add to timeline</button>
            <button className="button button-quiet" type="button" onClick={() => setPendingImport(null)}>Cancel</button>
          </div>
        )}
        {imports.length > 0 && !pendingImport && (
          <select value="" onChange={(e) => { const found = imports.find((i) => i.id === e.target.value); if (found) { setPendingImport(found); setImportStartMs(Math.round(playheadMs)); } }} aria-label="Add previously imported audio">
            <option value="">Previously imported…</option>
            {imports.map((i) => <option key={i.id} value={i.id}>{i.original_filename}</option>)}
          </select>
        )}
      </div>

      {/* export workflow */}
      <div className="tl-export" aria-label="Multilingual export">
        <strong>Export</strong>
        <label className="tl-check">Mode
          <select value={exportMode} onChange={(e) => setExportMode(e.target.value as "multitrack" | "separate_language")}>
            <option value="multitrack">One video, multiple selectable audio tracks (MKV)</option>
            <option value="separate_language">Separate Urdu / Hindi video exports (fallback)</option>
          </select>
        </label>
        <span className="tl-export-langs">
          Languages:
          {timeline.tracks.filter((t) => ["original_english", "urdu_dub", "hindi_dub"].includes(t.kind)).map((t) => (
            <label key={t.id} className="tl-check">
              <input type="checkbox" checked={t.enabled} onChange={(e) => void toggleTrack(t, { enabled: e.target.checked })} />
              {t.language === "ur" ? "Urdu" : t.language === "hi" ? "Hindi" : t.language === "en" ? "English" : t.name}
            </label>
          ))}
        </span>
        <button className="button button-quiet" type="button" onClick={makeExportPlan}>Preview export plan</button>
      </div>
      {exportInfo && <p className="planned-note" role="status">{exportInfo}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}

      {/* lip sync strip */}
      {lipSync.length > 0 && (
        <div className="tl-lipsync-strip" aria-label="Lip sync segments">
          <strong>Lip sync</strong>
          {lipSync.map((ls) => (
            <span key={ls.id} className={`chip ${ls.needs_regeneration ? "chip-warn" : ""}`} title={ls.regeneration_reason ?? ls.status}>
              {ls.language?.toUpperCase() ?? ""} {ls.segment_id?.slice(0, 6)} {ls.needs_regeneration ? "⚠ needs regeneration" : "✓ synced"}
            </span>
          ))}
        </div>
      )}

      {/* timeline scroll area */}
      <div
        className="tl-scroll"
        ref={scrollRef}
        onScroll={(e) => setScrollLeft((e.target as HTMLDivElement).scrollLeft)}
      >
        <div className="tl-canvas" style={{ width: totalWidth + LABEL_WIDTH }}>
          {/* ruler */}
          <div className="tl-ruler-row">
            <div className="tl-corner" style={{ width: LABEL_WIDTH }}>Tracks</div>
            <div className="tl-ruler-wrap" onPointerDown={rulerSeek}>
              <Ruler durationMs={durationMs} pxPerMs={pxPerMs} width={totalWidth} />
            </div>
          </div>
          {/* playhead */}
          <div className="tl-playhead" style={{ left: LABEL_WIDTH + playheadMs * pxPerMs }} aria-label="Playhead">
            <span className="tl-playhead-cap" />
          </div>
          {/* tracks */}
          {timeline.tracks.map((track) => (
            <div className={`tl-track kind-${track.kind} ${!track.enabled ? "is-disabled" : ""} ${track.muted ? "is-muted" : ""}`} key={track.id}>
              <div className="tl-track-head" style={{ width: LABEL_WIDTH, borderLeftColor: track.color }}>
                <div className="tl-track-title">
                  <span className="tl-track-dot" style={{ background: track.color }} />
                  <strong title={track.name}>{track.name || TRACK_KIND_LABELS[track.kind]}</strong>
                </div>
                {track.language && <small className="tl-track-lang">{track.language.toUpperCase()}</small>}
                <div className="tl-track-btns">
                  {track.kind !== "video" && track.kind !== "lip_sync" && (
                    <>
                      <button className={`tl-tbtn ${track.enabled ? "" : "off"}`} type="button" title={track.enabled ? "Disable track" : "Enable track"} onClick={() => void toggleTrack(track, { enabled: !track.enabled })}>{track.enabled ? "👁" : "🚫"}</button>
                      <button className={`tl-tbtn ${track.muted ? "on" : ""}`} type="button" title="Mute" onClick={() => void toggleTrack(track, { muted: !track.muted })}>M</button>
                      <button className={`tl-tbtn ${track.solo ? "on" : ""}`} type="button" title="Solo" onClick={() => void toggleTrack(track, { solo: !track.solo })}>S</button>
                      <button className={`tl-tbtn ${track.locked ? "on" : ""}`} type="button" title="Lock" onClick={() => void toggleTrack(track, { locked: !track.locked })}>🔒</button>
                    </>
                  )}
                </div>
              </div>
              <div className="tl-lane" style={{ width: totalWidth }}>
                {track.kind === "video" ? (
                  <VideoTrack track={track} pxPerMs={pxPerMs} videoId={videoId} durationMs={durationMs} />
                ) : (
                  track.clips
                    .filter((clip) => clip.start_ms + clip.duration_ms >= visibleStart && clip.start_ms <= visibleEnd)
                    .map((clip) => (
                      <ClipView
                        key={clip.id}
                        clip={clip}
                        track={track}
                        pxPerMs={pxPerMs}
                        selected={clip.id === selectedClipId}
                        color={track.color}
                        onSelect={setSelectedClipId}
                        onMoveStart={onMoveStart}
                        onTrimStart={onTrimStart}
                        lipSync={track.kind === "urdu_dub" || track.kind === "hindi_dub" ? lipSyncBySegment.get(`${clip.segment_id ?? ""}:${clip.language ?? ""}`) ?? null : null}
                      />
                    ))
                )}
              </div>
            </div>
          ))}
          {/* external track adder */}
          <div className="tl-track tl-track-add">
            <div className="tl-track-head" style={{ width: LABEL_WIDTH }}>
              <button className="button button-quiet" type="button" onClick={addExternalTrack}>＋ External audio track</button>
            </div>
            <div className="tl-lane" style={{ width: totalWidth }} />
          </div>
        </div>
      </div>

      <p className="tl-help">
        Drag clip body to move · drag edges to trim · S split · Del delete · Ctrl+D duplicate · Ctrl+Z/Y undo/redo · Snap grid {snap || "off"} ms · scroll = pan · +/− zoom.
        Language tracks are separate; exports never combine Urdu and Hindi.
      </p>
      {dubbedClips.length === 0 && (
        <p className="planned-note">No dubbed clips yet. Generate Urdu/Hindi dialogue (Phases 10–11) or import external audio; then edit it here.</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Video track with thumbnails
// ---------------------------------------------------------------------------

function VideoTrack({ track, pxPerMs, videoId, durationMs }: { track: TimelineTrack; pxPerMs: number; videoId: string; durationMs: number }) {
  const clip = track.clips[0];
  const width = Math.max(2, (clip?.duration_ms ?? durationMs) * pxPerMs);
  const [thumbs, setThumbs] = useState<{ url: string }[]>([]);
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/v1/videos/${encodeURIComponent(videoId)}/assets?kind=thumbnail`)
      .then((r) => (r.ok ? r.json() : []))
      .then((list) => !cancelled && setThumbs(Array.isArray(list) ? list : []))
      .catch(() => !cancelled && setThumbs([]));
    return () => { cancelled = true; };
  }, [videoId]);
  return (
    <div className="tl-clip tl-clip-video" style={{ left: (clip?.start_ms ?? 0) * pxPerMs, width, borderColor: track.color }}>
      <div className="tl-video-thumbs" aria-hidden="true">
        {thumbs.length > 0
          ? thumbs.map((thumb, i) => <img key={i} src={thumb.url} alt="" loading="lazy" />)
          : <span className="tl-video-placeholder">{track.name}</span>}
      </div>
    </div>
  );
}
