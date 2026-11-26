import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import { formatBytes, formatDuration } from "../lib/format";
import type { AudioBundle, AudioStem } from "../types";
import { JobProgress } from "./JobProgress";
import { ErrorState, LoadingState, StatusBadge } from "./States";

const ROLE_ORDER = ["dialogue", "music", "sfx", "ambience"] as const;
const ROLE_LABELS: Record<string, string> = {
  dialogue: "Dialogue / Vocals",
  music: "Background Music",
  sfx: "Sound Effects",
  ambience: "Ambience",
};

function placeholderStem(role: string): AudioStem {
  return {
    role,
    label: ROLE_LABELS[role] ?? role,
    status: "unavailable",
    asset_id: null,
    version: null,
    mime_type: null,
    size_bytes: null,
    url: null,
    download_url: null,
    duration_ms: null,
    sample_rate: null,
    channels: null,
    confidence: null,
    validation: {},
    comparison: {},
  };
}

interface StemPlayerProps {
  stem: AudioStem;
  compact?: boolean;
  previewActive?: boolean;
}

function StemPlayer({ stem, compact = false, previewActive = false }: StemPlayerProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [volume, setVolume] = useState(0.9);
  const [currentMs, setCurrentMs] = useState(0);
  const [liveDurationMs, setLiveDurationMs] = useState<number | null>(null);
  const [playError, setPlayError] = useState<string | null>(null);
  const available = stem.status === "available" && Boolean(stem.url);
  const durationMs = stem.duration_ms ?? liveDurationMs;

  useEffect(() => {
    if (audioRef.current) audioRef.current.volume = volume;
  }, [volume, available]);

  const togglePlayback = async () => {
    const audio = audioRef.current;
    if (!audio) return;
    setPlayError(null);
    if (playing) {
      audio.pause();
      return;
    }
    try {
      await audio.play();
    } catch {
      setPlayError("Playback was blocked by the browser. Press play again.");
    }
  };

  const changeVolume = (event: ChangeEvent<HTMLInputElement>) => {
    const next = Number(event.target.value);
    setVolume(next);
    if (audioRef.current) audioRef.current.volume = next;
  };

  return (
    <article className={`stem-card stem-${stem.role}${compact ? " stem-compact" : ""}`}>
      <div className="stem-head">
        <div>
          <strong>{stem.label}</strong>
          <small>{stem.role}</small>
        </div>
        <StatusBadge value={available ? "available" : "unavailable"} />
      </div>

      {available ? (
        <>
          <audio
            ref={audioRef}
            preload="metadata"
            src={api.assetUrl(stem.url!)}
            onLoadedMetadata={(event) => {
              const duration = event.currentTarget.duration;
              if (Number.isFinite(duration)) setLiveDurationMs(Math.round(duration * 1000));
            }}
            onTimeUpdate={(event) => setCurrentMs(Math.round(event.currentTarget.currentTime * 1000))}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onEnded={() => {
              setPlaying(false);
              setCurrentMs(0);
            }}
            aria-label={`Audio preview for ${stem.label}`}
          />
          <div className="stem-controls">
            <button
              className="button button-quiet stem-play"
              type="button"
              onClick={togglePlayback}
              disabled={previewActive}
              title={previewActive ? "Stop the synchronized mix preview first" : undefined}
            >
              {playing ? "Pause" : "Play"}
            </button>
            <label className="stem-volume">
              <span>Volume</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={volume}
                onChange={changeVolume}
                aria-label={`Volume for ${stem.label}`}
              />
            </label>
            <span className="stem-time">
              {formatDuration(currentMs)} / {formatDuration(durationMs ?? null)}
            </span>
          </div>
          <div className="stem-meta">
            <span>{stem.sample_rate ? `${(stem.sample_rate / 1000).toFixed(1)} kHz` : "rate unknown"}</span>
            <span>{stem.channels ? `${stem.channels} ch` : "channels unknown"}</span>
            {stem.size_bytes ? <span>{formatBytes(stem.size_bytes)}</span> : null}
            {stem.version ? <span>v{stem.version}</span> : null}
          </div>
          <div className="stem-actions">
            {stem.download_url && (
              <a
                className="button button-quiet"
                href={api.assetUrl(stem.download_url)}
                download
              >
                Download
              </a>
            )}
            {stem.confidence && <span className="stem-confidence">{stem.confidence.replace(/_/g, " ")}</span>}
          </div>
          {playError && <p className="form-error" role="alert">{playError}</p>}
        </>
      ) : (
        <p className="planned-note">No file yet. Run separation to generate this stem.</p>
      )}
    </article>
  );
}

interface AudioPreviewPanelProps {
  videoId: string;
  onJobStarted?: (jobId: string) => void;
  previewActive?: boolean;
}

export function AudioPreviewPanel({ videoId, onJobStarted, previewActive = false }: AudioPreviewPanelProps) {
  const state = useAsync<AudioBundle>(() => api.getAudioBundle(videoId), [videoId]);
  const [starting, setStarting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const bundle = state.data;
  const reload = useCallback(() => state.reload(), [state.reload]);
  const activeJobId = bundle?.job && ["queued", "running"].includes(bundle.job.status) ? bundle.job.id : null;

  useEffect(() => {
    if (!activeJobId) return;
    const timer = window.setInterval(() => reload(), 1200);
    return () => window.clearInterval(timer);
  }, [activeJobId, reload]);

  const startSeparation = async () => {
    if (starting || activeJobId) return;
    setStarting(true);
    setActionError(null);
    try {
      const job = await api.separateAudio(videoId);
      onJobStarted?.(job.id);
      reload();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not start audio separation.");
    } finally {
      setStarting(false);
    }
  };

  if (!bundle && state.loading) return <LoadingState label="Reading audio artifacts…" />;
  if (!bundle && state.error) return <ErrorState message={state.error} onRetry={reload} />;

  const readyRoles = bundle ? ROLE_ORDER.filter((role) => bundle.stems[role]?.status === "available") : [];

  return (
    <section className="panel studio-audio">
      <div className="panel-heading">
        <div>
          <h2>Audio Stems</h2>
          <p>Extracted source audio and independently stored separation previews.</p>
        </div>
        <StatusBadge value={bundle?.status ?? "idle"} />
      </div>

      <div className="audio-actions">
        <button
          className="button button-primary"
          type="button"
          onClick={startSeparation}
          disabled={starting || Boolean(activeJobId)}
        >
          {starting ? "Starting…" : activeJobId ? "Separation running" : "Extract & separate audio"}
        </button>
        {bundle?.source?.download_url && (
          <a className="button button-quiet" href={api.assetUrl(bundle.source.download_url)} download>
            Download source audio
          </a>
        )}
        {bundle?.backend && (
          <span className="audio-backend">
            {bundle.backend}
            {bundle.model ? ` · ${bundle.model}` : ""}
          </span>
        )}
        <span className="audio-ready">{readyRoles.length}/4 stems ready</span>
        {previewActive && <span className="audio-backend">Mixer preview active · standalone play paused</span>}
      </div>

      {actionError && <p className="form-error" role="alert">{actionError}</p>}

      {bundle?.job && (
        <div className="audio-job">
          <JobProgress
            jobId={bundle.job.id}
            onSettled={reload}
            onJobChange={(jobId) => onJobStarted?.(jobId)}
          />
        </div>
      )}

      {bundle?.source && (
        <div className="audio-source">
          <h3>Extracted source</h3>
          <StemPlayer
            stem={{ ...bundle.source, label: "Original audio (separate copy)" }}
            compact
            previewActive={previewActive}
          />
        </div>
      )}

      <div className="stem-grid">
        {ROLE_ORDER.map((role) => (
          <StemPlayer
            key={role}
            stem={bundle?.stems[role] ?? placeholderStem(role)}
            previewActive={previewActive}
          />
        ))}
      </div>

      {bundle && bundle.limitations.length > 0 && (
        <div className="audio-limitations" role="note">
          <strong>Separation limitations</strong>
          <ul>
            {bundle.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
