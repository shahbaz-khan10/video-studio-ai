import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, RefObject } from "react";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import { formatDuration } from "../lib/format";
import type { AudioBundle, AudioMixSettings } from "../types";
import { ErrorState, LoadingState, StatusBadge } from "./States";

const TRACKS = [
  { role: "dialogue", label: "Original Dialogue", detail: "Separated dialogue stem", planned: false },
  { role: "music", label: "Background Music", detail: "Separated music stem", planned: false },
  { role: "sfx", label: "Sound Effects", detail: "Separated effects stem", planned: false },
  { role: "ambience", label: "Ambience", detail: "Separated ambience stem", planned: false },
  { role: "urdu_dub", label: "Urdu Dub", detail: "Generated dub · coming in a later phase", planned: true },
  { role: "hindi_dub", label: "Hindi Dub", detail: "Generated dub · coming in a later phase", planned: true },
] as const;

type TrackRole = (typeof TRACKS)[number]["role"];

interface AudioMixerPanelProps {
  projectId: string;
  videoId: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  onPreviewChange?: (active: boolean) => void;
}

function clamp(value: number): number {
  return Math.min(1, Math.max(0, value));
}

export function AudioMixerPanel({ projectId, videoId, videoRef, onPreviewChange }: AudioMixerPanelProps) {
  const settingsState = useAsync<AudioMixSettings>(() => api.getAudioMixSettings(projectId), [projectId]);
  const bundleState = useAsync<AudioBundle>(() => api.getAudioBundle(videoId), [videoId]);
  const [settings, setSettings] = useState<AudioMixSettings | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [syncError, setSyncError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [lastCorrection, setLastCorrection] = useState<number | null>(null);
  const audioRefs = useRef<Partial<Record<TrackRole, HTMLAudioElement | null>>>({});
  const loadedRef = useRef(false);
  const skipSaveRef = useRef(false);
  const previousVideoMuted = useRef<boolean | null>(null);

  const settingsPayload = settingsState.data;
  const bundle = bundleState.data;

  useEffect(() => {
    loadedRef.current = false;
    skipSaveRef.current = false;
    setSettings(null);
  }, [projectId]);

  useEffect(() => {
    if (!settingsPayload) return;
    skipSaveRef.current = true;
    setSettings(settingsPayload);
    loadedRef.current = true;
  }, [settingsPayload]);

  useEffect(() => {
    const activeJob = bundle?.job && ["queued", "running"].includes(bundle.job.status);
    if (!activeJob) return;
    const timer = window.setInterval(() => bundleState.reload(), 1500);
    return () => window.clearInterval(timer);
  }, [bundle?.job, bundleState.reload]);

  useEffect(() => {
    if (!settings || !loadedRef.current) return;
    if (skipSaveRef.current) {
      skipSaveRef.current = false;
      return;
    }
    setSaveState("saving");
    const timer = window.setTimeout(async () => {
      try {
        const saved = await api.saveAudioMixSettings(projectId, settings);
        setSaveState("saved");
        if (saved.updated_at !== settings.updated_at) {
          skipSaveRef.current = true;
          setSettings(saved);
        }
      } catch {
        setSaveState("error");
      }
    }, 300);
    return () => window.clearTimeout(timer);
  }, [settings, projectId]);

  const availableRoles = useMemo(
    () =>
      TRACKS.filter((track) => {
        const stem = bundle?.stems[track.role];
        return stem?.status === "available" && Boolean(stem.url);
      }).map((track) => track.role),
    [bundle],
  );
  const availableKey = availableRoles.join(",");
  const previewActive = Boolean(settings?.preview_enabled && availableRoles.length > 0);
  const anySolo = Boolean(
    settings &&
      TRACKS.some(
        (track) =>
          availableRoles.includes(track.role) &&
          settings.tracks[track.role]?.solo &&
          settings.tracks[track.role]?.enabled,
      ),
  );

  const effectiveGain = useCallback(
    (role: TrackRole): number => {
      const track = settings?.tracks[role];
      const stem = bundle?.stems[role];
      if (!track || !stem || stem.status !== "available" || !stem.url) return 0;
      if (!track.enabled || track.muted) return 0;
      if (anySolo && !track.solo) return 0;
      return clamp(track.volume * (settings?.master_volume ?? 1));
    },
    [settings, bundle, anySolo],
  );

  useEffect(() => {
    for (const role of availableRoles) {
      const node = audioRefs.current[role];
      if (!node) continue;
      const gain = previewActive ? effectiveGain(role) : 0;
      node.volume = gain;
      node.muted = gain <= 0.0001;
      const rate = videoRef.current?.playbackRate || 1;
      if (Math.abs(node.playbackRate - rate) > 0.001) node.playbackRate = rate;
    }
  }, [settings, previewActive, availableKey, effectiveGain, videoRef]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (previewActive) {
      if (previousVideoMuted.current === null) previousVideoMuted.current = video.muted;
      video.muted = true;
    } else if (previousVideoMuted.current !== null) {
      video.muted = previousVideoMuted.current;
      previousVideoMuted.current = null;
    }
    return () => {
      if (previousVideoMuted.current !== null) {
        video.muted = previousVideoMuted.current;
        previousVideoMuted.current = null;
      }
    };
  }, [previewActive, videoRef]);

  useEffect(() => {
    onPreviewChange?.(previewActive);
  }, [previewActive, onPreviewChange]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !previewActive) return;
    let active = true;
    setSyncError(null);

    const nodes = () =>
      availableRoles
        .map((role) => audioRefs.current[role])
        .filter((node): node is HTMLAudioElement => Boolean(node));

    const align = async (shouldPlay: boolean) => {
      for (const node of nodes()) {
        try {
          const limit = Number.isFinite(node.duration) ? Math.max(0, node.duration - 0.05) : Number.MAX_SAFE_INTEGER;
          const target = Math.min(Math.max(0, video.currentTime), limit);
          if (Math.abs(node.currentTime - target) > 0.03) node.currentTime = target;
          node.playbackRate = video.playbackRate || 1;
          if (shouldPlay) await node.play();
          else node.pause();
        } catch {
          setSyncError("A stem could not start. Press play again after the audio has loaded.");
        }
      }
    };

    const onPlay = () => {
      setPlaying(true);
      void align(true);
    };
    const onPause = () => {
      setPlaying(false);
      nodes().forEach((node) => node.pause());
    };
    const onSeek = () => void align(!video.paused);
    const onRate = () => nodes().forEach((node) => (node.playbackRate = video.playbackRate || 1));
    const onEnded = () => {
      setPlaying(false);
      nodes().forEach((node) => node.pause());
    };

    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onPause);
    video.addEventListener("seeking", onSeek);
    video.addEventListener("seeked", onSeek);
    video.addEventListener("ratechange", onRate);
    video.addEventListener("ended", onEnded);
    if (!video.paused) void align(true);

    const timer = window.setInterval(() => {
      if (!active || video.paused) return;
      let corrected = false;
      for (const node of nodes()) {
        const rate = video.playbackRate || 1;
        if (Math.abs(node.playbackRate - rate) > 0.01) node.playbackRate = rate;
        if (Math.abs(node.currentTime - video.currentTime) > 0.12) {
          try {
            node.currentTime = video.currentTime;
            corrected = true;
          } catch {
            setSyncError("Could not correct one stem after seeking.");
          }
        }
        if (node.paused) void node.play().catch(() => undefined);
      }
      if (corrected) setLastCorrection(Date.now());
    }, 250);

    return () => {
      active = false;
      window.clearInterval(timer);
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("seeking", onSeek);
      video.removeEventListener("seeked", onSeek);
      video.removeEventListener("ratechange", onRate);
      video.removeEventListener("ended", onEnded);
      nodes().forEach((node) => node.pause());
    };
  }, [previewActive, availableKey, videoRef]);

  const updateTrack = (role: TrackRole, patch: Partial<{ volume: number; muted: boolean; solo: boolean; enabled: boolean }>) => {
    setSettings((current) => {
      if (!current) return current;
      const track = current.tracks[role];
      if (!track) return current;
      return {
        ...current,
        tracks: {
          ...current.tracks,
          [role]: { ...track, ...patch },
        },
      };
    });
  };

  const togglePreview = () => {
    if (!settings || availableRoles.length === 0) return;
    setSettings({ ...settings, preview_enabled: !settings.preview_enabled });
  };

  const togglePlayback = async () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      try {
        await video.play();
      } catch {
        setSyncError("The browser blocked playback. Use the video player controls to start playback.");
      }
    } else {
      video.pause();
    }
  };

  const resetMixer = async () => {
    try {
      skipSaveRef.current = true;
      const reset = await api.resetAudioMixSettings(projectId);
      setSettings(reset);
      setSaveState("saved");
    } catch {
      setSaveState("error");
    }
  };

  if (!settings && settingsState.loading) return <LoadingState label="Loading mixer settings…" />;
  if (!settings && settingsState.error) return <ErrorState message={settingsState.error} onRetry={settingsState.reload} />;
  if (!settings) return null;
  if (bundleState.error) return <ErrorState message={bundleState.error} onRetry={bundleState.reload} />;

  const dialogueStem = bundle?.stems.dialogue;
  const dialogueAvailable = availableRoles.includes("dialogue");
  const dialogueValidated = dialogueStem?.confidence === "clean_validated";
  const dialogueWarning = !dialogueAvailable
    ? "No separate dialogue stem is available. Dialogue volume and mute cannot affect the original track without separation."
    : !dialogueValidated
      ? "Dialogue leakage may remain. This stem is an estimate and has not been validated as clean speech."
      : null;

  const videoDuration = videoRef.current?.duration;
  const safeDuration = Number.isFinite(videoDuration) ? (videoDuration as number) * 1000 : (bundle?.source?.duration_ms ?? null);

  return (
    <section className="panel studio-mixer" aria-label="Audio mixer">
      <div className="panel-heading">
        <div>
          <h2>Audio Mixer</h2>
          <p>Independent stem control, synchronized with the original video.</p>
        </div>
        <StatusBadge value={previewActive ? "mixed" : "native"} />
      </div>

      <div className="mixer-audio-elements" aria-hidden="true">
        {TRACKS.filter((track) => availableRoles.includes(track.role)).map((track) => (
          <audio
            key={track.role}
            ref={(node) => {
              audioRefs.current[track.role] = node;
            }}
            data-track={track.role}
            preload="auto"
            src={api.assetUrl(bundle!.stems[track.role].url!)}
          />
        ))}
      </div>

      <div className="mixer-toolbar">
        <button
          className={`button ${previewActive ? "button-quiet" : "button-primary"}`}
          type="button"
          onClick={togglePreview}
          disabled={availableRoles.length === 0}
        >
          {previewActive ? "Use native audio" : "Preview separated mix"}
        </button>
        <button className="button button-quiet" type="button" onClick={togglePlayback}>
          {playing ? "Pause mix" : "Play with video"}
        </button>
        <button className="button button-quiet" type="button" onClick={resetMixer}>
          Reset mixer
        </button>
        <label className="mixer-master">
          <span>Master {Math.round(settings.master_volume * 100)}%</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={settings.master_volume}
            onChange={(event: ChangeEvent<HTMLInputElement>) =>
              setSettings({ ...settings, master_volume: clamp(Number(event.target.value)) })
            }
            aria-label="Master mix volume"
          />
        </label>
        <span className={`mixer-save mixer-save-${saveState}`}>
          {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Saved" : saveState === "error" ? "Save failed" : "Per project"}
        </span>
      </div>

      <div className="mixer-sync" role="status">
        <span>{previewActive ? (playing ? "Playing · synced to video" : "Paused · ready to sync") : "Native video audio is active"}</span>
        <span>Duration {formatDuration(safeDuration)}</span>
        {previewActive && <span>Drift check every 250 ms{lastCorrection ? " · corrected" : ""}</span>}
      </div>

      {dialogueWarning && (
        <div className="audio-limitations mixer-warning" role="note">
          <strong>Dialogue removal</strong>
          <p>{dialogueWarning}</p>
        </div>
      )}
      {syncError && <p className="form-error" role="alert">{syncError}</p>}

      <div className="mixer-tracks" role="table" aria-label="Audio mixer tracks">
        <div className="mixer-track mixer-track-head" role="row">
          <span role="columnheader">Track</span>
          <span role="columnheader">Volume</span>
          <span role="columnheader">Mute</span>
          <span role="columnheader">Solo</span>
          <span role="columnheader">Enabled</span>
          <span role="columnheader">Status</span>
        </div>
        {TRACKS.map((track) => {
          const state = settings.tracks[track.role];
          const available = availableRoles.includes(track.role);
          const controllable = available && !track.planned;
          const gain = effectiveGain(track.role);
          const status = track.planned
            ? "planned"
            : !available
              ? "unavailable"
              : !state.enabled
                ? "disabled"
                : state.muted
                  ? "muted"
                  : anySolo && !state.solo
                    ? "soloed out"
                    : state.solo
                      ? "solo"
                      : "active";
          return (
            <div
              className={`mixer-track mixer-track-${track.role}${state.muted ? " mixer-track-is-muted" : ""}${gain === 0 ? " mixer-track-is-silent" : ""}`}
              role="row"
              key={track.role}
            >
              <div className="mixer-track-name" role="cell">
                <strong>{track.label}</strong>
                <small>{track.detail}</small>
              </div>
              <div className="mixer-track-volume" role="cell">
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={state.volume}
                  disabled={!controllable}
                  onChange={(event) => updateTrack(track.role, { volume: clamp(Number(event.target.value)) })}
                  aria-label={`${track.label} volume`}
                />
                <span>{Math.round(state.volume * 100)}%</span>
              </div>
              <button
                className={`button button-quiet mixer-toggle${state.muted ? " is-on" : ""}`}
                type="button"
                role="cell"
                aria-pressed={state.muted}
                disabled={!controllable}
                onClick={() => updateTrack(track.role, { muted: !state.muted })}
              >
                {state.muted ? "Muted" : "Mute"}
              </button>
              <button
                className={`button button-quiet mixer-toggle${state.solo ? " is-on" : ""}`}
                type="button"
                role="cell"
                aria-pressed={state.solo}
                disabled={!controllable}
                onClick={() => updateTrack(track.role, { solo: !state.solo })}
              >
                Solo
              </button>
              <label className="mixer-enabled" role="cell">
                <input
                  type="checkbox"
                  checked={state.enabled}
                  disabled={!controllable}
                  onChange={(event) => updateTrack(track.role, { enabled: event.target.checked })}
                  aria-label={`${track.label} enabled`}
                />
                <span>{state.enabled ? "On" : "Off"}</span>
              </label>
              <div className="mixer-track-status" role="cell">
                <StatusBadge value={status} />
                {previewActive && available && <span className="mixer-gain">{Math.round(gain * 100)}%</span>}
              </div>
            </div>
          );
        })}
      </div>

      <p className="mixer-note">
        Mix preview mutes the video player's built-in audio only while preview is active, preventing double playback.
        The uploaded video and extracted source audio are never modified.
      </p>
    </section>
  );
}
