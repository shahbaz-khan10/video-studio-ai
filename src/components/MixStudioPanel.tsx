import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import {
  includedRoles,
  mixStudioApi,
  type MixRole,
  type MixSettings,
  type MixTarget,
  type StudioBundle,
  type StudioTrack,
  type TrackSettings,
} from "../lib/mixStudioApi";
import { useMixPreview } from "../hooks/useMixPreview";
import { JobProgress } from "./JobProgress";
import { ErrorState, LoadingState } from "./States";
import "./DubAudioPanel.css";
import "./MixStudioPanel.css";

interface MixStudioPanelProps {
  videoId: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  onJobStarted: (jobId: string) => void;
}

const TARGET_LABEL: Record<MixTarget, string> = { original: "Original (English)", ur: "Urdu mix", hi: "Hindi mix" };
const LOUDNESS_PRESETS = [
  { value: -23, label: "-23 LUFS · broadcast (EBU R128)" },
  { value: -16, label: "-16 LUFS · web / mobile" },
  { value: -14, label: "-14 LUFS · streaming" },
];

function message(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  unit,
  onChange,
  disabled,
  tag,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit: string;
  onChange: (value: number) => void;
  disabled?: boolean;
  tag?: string;
}) {
  return (
    <label className={`mix-slider${disabled ? " is-disabled" : ""}`}>
      <span>
        {label}
        {tag && <em className="mix-tag">{tag}</em>}
        <b>
          {value > 0 && unit === "dB" ? "+" : ""}
          {Number.isInteger(step) ? value : value.toFixed(step < 0.1 ? 2 : 1)}
          {unit}
        </b>
      </span>
      <input type="range" min={min} max={max} step={step} value={value} disabled={disabled} onChange={(event) => onChange(Number(event.target.value))} />
    </label>
  );
}

export function MixStudioPanel({ videoId, videoRef, onJobStarted }: MixStudioPanelProps) {
  const [bundle, setBundle] = useState<StudioBundle | null>(null);
  const [draft, setDraft] = useState<MixSettings | null>(null);
  const [revision, setRevision] = useState<number | undefined>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [target, setTarget] = useState<MixTarget>("ur");
  const [mode, setMode] = useState<"off" | "mix" | "original">("off");
  const [expanded, setExpanded] = useState<MixRole | null>(null);
  const [busy, setBusy] = useState(false);
  const [presetName, setPresetName] = useState("");
  const [format, setFormat] = useState<"wav" | "aac">("wav");
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobActive, setJobActive] = useState(false);
  const [exact, setExact] = useState<{ url: string; start: number } | null>(null);
  const exactRef = useRef<HTMLAudioElement>(null);

  const refresh = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    mixStudioApi
      .bundle(videoId)
      .then((result) => {
        if (!active) return;
        setBundle(result);
        setDraft((current) => (current && revision === result.settings.revision ? current : clone(result.settings.settings)));
        setRevision(result.settings.revision);
        if (result.job && (result.job.status === "queued" || result.job.status === "running")) {
          setJobId(result.job.id);
          setJobActive(true);
        } else if (result.job?.status === "failed") setJobId(result.job.id);
      })
      .catch((reason: unknown) => active && setError(message(reason, "Could not load the mixing studio.")))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId, version]);

  useEffect(() => () => {
    if (exact) URL.revokeObjectURL(exact.url);
  }, [exact]);

  const tracks: StudioTrack[] = bundle?.tracks ?? [];
  const preview = useMixPreview(videoRef, tracks, draft, target, mode, mixStudioApi.url);
  const saved = bundle?.settings.settings;
  const dirty = useMemo(() => Boolean(draft && saved && JSON.stringify(draft) !== JSON.stringify(saved)), [draft, saved]);
  const audible = useMemo(() => new Set(draft ? includedRoles(draft, tracks, target) : []), [draft, tracks, target]);

  const setTrack = (role: MixRole, patch: (track: TrackSettings) => void) =>
    setDraft((current) => {
      if (!current) return current;
      const next = clone(current);
      patch(next.tracks[role]);
      return next;
    });
  const setMaster = (patch: (settings: MixSettings) => void) =>
    setDraft((current) => {
      if (!current) return current;
      const next = clone(current);
      patch(next);
      return next;
    });

  const act = async (task: () => Promise<void>) => {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await task();
    } catch (reason) {
      setActionError(message(reason, "Action failed."));
    } finally {
      setBusy(false);
    }
  };

  const applyPayload = (payload: StudioBundle["settings"]) => {
    setBundle((value) => (value ? { ...value, settings: payload } : value));
    setDraft(clone(payload.settings));
    setRevision(payload.revision);
  };

  const save = () =>
    act(async () => {
      if (!draft || !bundle) return;
      applyPayload(await mixStudioApi.save(bundle.project_id, draft, revision));
      setNotice("Mixer settings saved for this project.");
    });

  const hearExact = () =>
    act(async () => {
      if (!draft) return;
      const video = videoRef.current;
      const start = Math.max(0, (video?.currentTime ?? 0) * 1000);
      const url = await mixStudioApi.exactPreview(videoId, target, start, 15000, draft);
      setMode("off");
      setExact((old) => {
        if (old) URL.revokeObjectURL(old.url);
        return { url, start };
      });
      window.setTimeout(() => {
        const audio = exactRef.current;
        if (!audio || !video) return;
        video.muted = true;
        video.currentTime = start / 1000;
        void Promise.all([audio.play(), video.play()]).catch(() => undefined);
      }, 80);
    });

  if (loading && !bundle) return <LoadingState label="Loading mixing studio…" />;
  if (error && !bundle) return <ErrorState message={error} onRetry={refresh} />;
  if (!bundle || !draft) return null;

  const renderOnly = "render only";

  return (
    <div className="mix-panel">
      <div className="mix-toolbar">
        <div className="dub-segmented" role="group" aria-label="Mix target">
          {(["original", "ur", "hi"] as MixTarget[]).map((value) => (
            <button key={value} type="button" className={target === value ? "is-active" : ""} onClick={() => setTarget(value)}>
              {TARGET_LABEL[value]}
            </button>
          ))}
        </div>
        <div className="dub-segmented" role="group" aria-label="Preview">
          <button type="button" className={mode === "off" ? "is-active" : ""} onClick={() => setMode("off")}>
            Preview off
          </button>
          <button
            type="button"
            className={mode === "mix" ? "is-active" : ""}
            onClick={() => {
              setMode("mix");
              preview.enable();
            }}
          >
            Live mix
          </button>
          <button type="button" className={mode === "original" ? "is-active" : ""} onClick={() => setMode("original")} title="A/B: the film's own audio">
            Original audio (A/B)
          </button>
        </div>
        <button type="button" className="button button-quiet button-small" disabled={busy} onClick={() => void hearExact()} title="Renders 15 s from the playhead with every effect, exactly as the final file">
          Hear exact render (15 s)
        </button>
        <span className="dub-spacer" />
        <button type="button" className="button button-primary" disabled={!dirty || busy} onClick={() => void save()}>
          {dirty ? "Save mix settings" : "Saved"}
        </button>
        <button
          type="button"
          className="button button-quiet button-small"
          disabled={busy}
          onClick={() => {
            if (!window.confirm("Reset all mixer settings to defaults? (You can restore the previous settings afterwards.)")) return;
            void act(async () => applyPayload(await mixStudioApi.reset(bundle.project_id)));
          }}
        >
          Reset
        </button>
        <button
          type="button"
          className="button button-quiet button-small"
          disabled={busy || !bundle.settings.history.length}
          onClick={() => void act(async () => applyPayload(await mixStudioApi.restorePrevious(bundle.project_id)))}
          title={bundle.settings.history.at(-1) ? `Undo: ${bundle.settings.history.at(-1)!.reason}` : "No earlier settings"}
        >
          Restore previous
        </button>
      </div>

      <p className="mix-note">
        Live preview applies volume, mute/solo, pan, EQ, compressor, gain and limiter in your browser, synced to the video
        {mode === "mix" ? ` (drift ≤ ${preview.maxDriftMs} ms, ${preview.corrections} hard corrections)` : ""}. Noise reduction, fades, ducking and
        loudness normalisation are <em>render only</em> — use “Hear exact render” to hear them.
        {dirty ? " You have unsaved changes." : ""}
      </p>
      {exact && (
        <div className="mix-exact">
          <strong>Exact render from {(exact.start / 1000).toFixed(1)} s</strong>
          <audio ref={exactRef} controls src={exact.url} onEnded={() => videoRef.current?.pause()} />
        </div>
      )}
      {preview.error && <p className="dub-flag dub-flag-warning">{preview.error}</p>}
      {notice && <p className="translation-notice">{notice}</p>}
      {actionError && (
        <p className="form-error" role="alert">
          {actionError}
        </p>
      )}

      <div className="mix-strips">
        {tracks.map((track) => {
          const t = draft.tracks[track.role];
          const isAudible = audible.has(track.role);
          const open = expanded === track.role;
          return (
            <section key={track.role} className={`mix-strip${track.available ? "" : " is-unavailable"}${isAudible ? " is-audible" : ""}`}>
              <header>
                <strong>{track.label}</strong>
                <span className={`badge ${track.available ? (track.stale ? "dub-badge-stale_text" : "dub-badge-approved") : "dub-badge-missing"}`}>
                  {track.available ? (track.stale ? "outdated" : "ready") : "unavailable"}
                </span>
              </header>
              <small className="muted">{track.available ? track.origin : track.reason}</small>
              <small className="muted">
                {track.included[target] ? (isAudible ? "in this mix" : "in this mix · silenced") : target === "original" || track.kind === "bed" ? "not in this mix" : "other language / original only"}
              </small>
              <div className="mix-buttons">
                <button type="button" className={`mix-toggle${t.enabled ? " is-on" : ""}`} disabled={!track.available} onClick={() => setTrack(track.role, (x) => (x.enabled = !x.enabled))}>
                  {t.enabled ? "On" : "Off"}
                </button>
                <button type="button" className={`mix-toggle mute${t.muted ? " is-on" : ""}`} disabled={!track.available} onClick={() => setTrack(track.role, (x) => (x.muted = !x.muted))}>
                  M
                </button>
                <button type="button" className={`mix-toggle solo${t.solo ? " is-on" : ""}`} disabled={!track.available} onClick={() => setTrack(track.role, (x) => (x.solo = !x.solo))}>
                  S
                </button>
              </div>
              <Slider label="Volume" value={t.volume_db} min={-60} max={12} step={0.5} unit="dB" disabled={!track.available} onChange={(v) => setTrack(track.role, (x) => (x.volume_db = v))} />
              <Slider label="Pan" value={t.pan} min={-1} max={1} step={0.05} unit="" disabled={!track.available} onChange={(v) => setTrack(track.role, (x) => (x.pan = v))} />
              <Slider label="Gain" value={t.gain_db} min={-24} max={24} step={0.5} unit="dB" disabled={!track.available} onChange={(v) => setTrack(track.role, (x) => (x.gain_db = v))} />
              <button type="button" className="link-button" onClick={() => setExpanded(open ? null : track.role)} disabled={!track.available}>
                {open ? "Hide effects" : `Effects${[t.eq.enabled && "EQ", t.compressor.enabled && "Comp", t.noise_reduction.enabled && "NR", (t.fade_in_ms || t.fade_out_ms) && "Fade"].filter(Boolean).map((x) => ` · ${x}`).join("")}`}
              </button>
              {open && (
                <div className="mix-effects">
                  <fieldset>
                    <legend>
                      <label className="dub-check">
                        <input type="checkbox" checked={t.eq.enabled} onChange={(e) => setTrack(track.role, (x) => (x.eq.enabled = e.target.checked))} />
                        Equalizer
                      </label>
                    </legend>
                    <Slider label="High-pass" value={t.eq.highpass_hz} min={20} max={400} step={5} unit="Hz" disabled={!t.eq.enabled} onChange={(v) => setTrack(track.role, (x) => (x.eq.highpass_hz = v))} />
                    <Slider label="Low 120 Hz" value={t.eq.low_gain_db} min={-12} max={12} step={0.5} unit="dB" disabled={!t.eq.enabled} onChange={(v) => setTrack(track.role, (x) => (x.eq.low_gain_db = v))} />
                    <Slider label="Mid freq" value={t.eq.mid_freq_hz} min={300} max={8000} step={50} unit="Hz" disabled={!t.eq.enabled} onChange={(v) => setTrack(track.role, (x) => (x.eq.mid_freq_hz = v))} />
                    <Slider label="Mid gain" value={t.eq.mid_gain_db} min={-12} max={12} step={0.5} unit="dB" disabled={!t.eq.enabled} onChange={(v) => setTrack(track.role, (x) => (x.eq.mid_gain_db = v))} />
                    <Slider label="High 8 kHz" value={t.eq.high_gain_db} min={-12} max={12} step={0.5} unit="dB" disabled={!t.eq.enabled} onChange={(v) => setTrack(track.role, (x) => (x.eq.high_gain_db = v))} />
                  </fieldset>
                  <fieldset>
                    <legend>
                      <label className="dub-check">
                        <input type="checkbox" checked={t.compressor.enabled} onChange={(e) => setTrack(track.role, (x) => (x.compressor.enabled = e.target.checked))} />
                        Compressor
                      </label>
                    </legend>
                    <Slider label="Threshold" value={t.compressor.threshold_db} min={-50} max={-3} step={1} unit="dB" disabled={!t.compressor.enabled} onChange={(v) => setTrack(track.role, (x) => (x.compressor.threshold_db = v))} />
                    <Slider label="Ratio" value={t.compressor.ratio} min={1} max={20} step={0.5} unit=":1" disabled={!t.compressor.enabled} onChange={(v) => setTrack(track.role, (x) => (x.compressor.ratio = v))} />
                    <Slider label="Attack" value={t.compressor.attack_ms} min={1} max={200} step={1} unit="ms" disabled={!t.compressor.enabled} onChange={(v) => setTrack(track.role, (x) => (x.compressor.attack_ms = v))} />
                    <Slider label="Release" value={t.compressor.release_ms} min={20} max={1000} step={10} unit="ms" disabled={!t.compressor.enabled} onChange={(v) => setTrack(track.role, (x) => (x.compressor.release_ms = v))} />
                    <Slider label="Makeup" value={t.compressor.makeup_db} min={0} max={18} step={0.5} unit="dB" disabled={!t.compressor.enabled} onChange={(v) => setTrack(track.role, (x) => (x.compressor.makeup_db = v))} />
                  </fieldset>
                  <fieldset>
                    <legend>
                      <label className="dub-check">
                        <input type="checkbox" checked={t.noise_reduction.enabled} onChange={(e) => setTrack(track.role, (x) => (x.noise_reduction.enabled = e.target.checked))} />
                        Noise reduction <em className="mix-tag">{renderOnly}</em>
                      </label>
                    </legend>
                    <Slider label="Reduction" value={t.noise_reduction.amount_db} min={3} max={40} step={1} unit="dB" disabled={!t.noise_reduction.enabled} onChange={(v) => setTrack(track.role, (x) => (x.noise_reduction.amount_db = v))} />
                    <small className="muted">The noise floor is measured from the track automatically.</small>
                  </fieldset>
                  <fieldset>
                    <legend>
                      Fades <em className="mix-tag">{renderOnly}</em>
                    </legend>
                    <Slider label="Fade in" value={t.fade_in_ms} min={0} max={10000} step={100} unit="ms" onChange={(v) => setTrack(track.role, (x) => (x.fade_in_ms = v))} />
                    <Slider label="Fade out" value={t.fade_out_ms} min={0} max={10000} step={100} unit="ms" onChange={(v) => setTrack(track.role, (x) => (x.fade_out_ms = v))} />
                  </fieldset>
                  {track.available && track.url && (
                    <audio controls preload="none" src={mixStudioApi.url(track.url)} aria-label={`${track.label} source file`} />
                  )}
                </div>
              )}
            </section>
          );
        })}

        <section className="mix-strip mix-master">
          <header>
            <strong>Master</strong>
            <span className="badge">{TARGET_LABEL[target]}</span>
          </header>
          <Slider label="Master gain" value={draft.master.gain_db} min={-24} max={24} step={0.5} unit="dB" onChange={(v) => setMaster((s) => (s.master.gain_db = v))} />
          <label className="dub-check">
            <input type="checkbox" checked={draft.master.limiter.enabled} onChange={(e) => setMaster((s) => (s.master.limiter.enabled = e.target.checked))} />
            Limiter
          </label>
          <Slider label="Ceiling" value={draft.master.limiter.ceiling_db} min={-12} max={-0.1} step={0.1} unit="dB" disabled={!draft.master.limiter.enabled} onChange={(v) => setMaster((s) => (s.master.limiter.ceiling_db = v))} />
          <label className="dub-check">
            <input type="checkbox" checked={draft.master.loudness.enabled} onChange={(e) => setMaster((s) => (s.master.loudness.enabled = e.target.checked))} />
            Loudness normalisation <em className="mix-tag">{renderOnly}</em>
          </label>
          <select value={draft.master.loudness.target_lufs} disabled={!draft.master.loudness.enabled} onChange={(e) => setMaster((s) => (s.master.loudness.target_lufs = Number(e.target.value)))}>
            {LOUDNESS_PRESETS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <label className="dub-check">
            <input type="checkbox" checked={draft.ducking.enabled} onChange={(e) => setMaster((s) => (s.ducking.enabled = e.target.checked))} />
            Duck music &amp; effects under dub dialogue <em className="mix-tag">{renderOnly}</em>
          </label>
          <Slider label="Duck threshold" value={draft.ducking.threshold_db} min={-50} max={-3} step={1} unit="dB" disabled={!draft.ducking.enabled} onChange={(v) => setMaster((s) => (s.ducking.threshold_db = v))} />
          <Slider label="Duck ratio" value={draft.ducking.ratio} min={1} max={20} step={0.5} unit=":1" disabled={!draft.ducking.enabled} onChange={(v) => setMaster((s) => (s.ducking.ratio = v))} />
          <Slider label="Release" value={draft.ducking.release_ms} min={50} max={2000} step={50} unit="ms" disabled={!draft.ducking.enabled} onChange={(v) => setMaster((s) => (s.ducking.release_ms = v))} />
          <Slider label="Mix fade in" value={draft.master.fade_in_ms} min={0} max={10000} step={100} unit="ms" tag={renderOnly} onChange={(v) => setMaster((s) => (s.master.fade_in_ms = v))} />
          <Slider label="Mix fade out" value={draft.master.fade_out_ms} min={0} max={10000} step={100} unit="ms" tag={renderOnly} onChange={(v) => setMaster((s) => (s.master.fade_out_ms = v))} />
          <label className="dub-check">
            <input type="checkbox" checked={draft.keep_original_dialogue_in_dub} onChange={(e) => setMaster((s) => (s.keep_original_dialogue_in_dub = e.target.checked))} />
            Keep original English dialogue in dub mixes
          </label>
        </section>
      </div>

      <div className="mix-bottom">
        <section className="mix-box">
          <h3>Presets</h3>
          <div className="mix-row">
            <input value={presetName} placeholder="Preset name" maxLength={120} onChange={(event) => setPresetName(event.target.value)} />
            <button
              type="button"
              className="button button-small"
              disabled={busy || !presetName.trim()}
              onClick={() =>
                void act(async () => {
                  const name = presetName.trim();
                  const exists = bundle.presets.some((item) => item.name === name);
                  if (exists && !window.confirm(`Overwrite the preset “${name}” with the current settings?`)) return;
                  await mixStudioApi.savePreset(bundle.project_id, name, draft, exists);
                  setPresetName("");
                  setNotice(`Preset “${name}” saved.`);
                  refresh();
                })
              }
            >
              Save as preset
            </button>
          </div>
          {!bundle.presets.length && <p className="muted">No presets yet.</p>}
          <ul className="mix-list">
            {bundle.presets.map((item) => (
              <li key={item.id}>
                <span>{item.name}</span>
                <button
                  type="button"
                  className="button button-quiet button-small"
                  disabled={busy}
                  onClick={() => {
                    if (dirty && !window.confirm("Apply this preset and discard your unsaved changes?")) return;
                    void act(async () => {
                      applyPayload(await mixStudioApi.applyPreset(bundle.project_id, item.id));
                      setNotice(`Applied “${item.name}”. Use “Restore previous” to undo.`);
                    });
                  }}
                >
                  Apply
                </button>
                <button
                  type="button"
                  className="button button-quiet button-small"
                  disabled={busy}
                  onClick={() => {
                    if (!window.confirm(`Delete the preset “${item.name}”?`)) return;
                    void act(async () => {
                      await mixStudioApi.deletePreset(bundle.project_id, item.id);
                      refresh();
                    });
                  }}
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className="mix-box">
          <h3>Render</h3>
          <div className="mix-row">
            <select value={target} onChange={(event) => setTarget(event.target.value as MixTarget)}>
              {(["original", "ur", "hi"] as MixTarget[]).map((value) => (
                <option key={value} value={value}>
                  {TARGET_LABEL[value]}
                </option>
              ))}
            </select>
            <select value={format} onChange={(event) => setFormat(event.target.value as "wav" | "aac")}>
              <option value="wav">WAV 48 kHz 24-bit</option>
              <option value="aac">AAC 256 kbps (.m4a)</option>
            </select>
            <button
              type="button"
              className="button button-primary button-small"
              disabled={busy || jobActive}
              onClick={() =>
                void act(async () => {
                  if (dirty && !window.confirm("Render uses the SAVED settings. Save your changes first?")) return;
                  if (dirty) applyPayload(await mixStudioApi.save(bundle.project_id, draft, revision));
                  const job = await mixStudioApi.render(videoId, target, format);
                  setJobId(job.id);
                  setJobActive(true);
                  onJobStarted(job.id);
                })
              }
            >
              {jobActive ? "Rendering…" : "Render mix"}
            </button>
          </div>
          {jobId && (
            <JobProgress
              jobId={jobId}
              onSettled={() => {
                setJobActive(false);
                refresh();
              }}
              onJobChange={(id) => {
                setJobId(id);
                setJobActive(true);
              }}
            />
          )}
          <ul className="mix-renders">
            {bundle.renders.map((item) => (
              <li key={item.id}>
                <div>
                  <strong>
                    {TARGET_LABEL[item.target]} · {item.format.toUpperCase()}
                  </strong>
                  <span className={`badge ${item.status === "ready" ? "dub-badge-approved" : "dub-badge-failed"}`}>{item.status}</span>
                  <small className="muted">{item.created_at ? new Date(item.created_at).toLocaleString() : ""}</small>
                </div>
                {item.status === "ready" ? (
                  <>
                    <small className="muted">
                      {item.stats.integrated_lufs ?? "—"} LUFS · true peak {item.stats.true_peak_dbfs ?? "—"} dBFS · {item.stats.duration_s}s ·{" "}
                      {item.size_bytes ? `${(item.size_bytes / 1e6).toFixed(1)} MB` : ""} · {item.tracks.map((x) => x.label).join(", ")}
                    </small>
                    <audio controls preload="none" src={mixStudioApi.url(item.url!)} />
                    <div className="translation-actions">
                      <a className="button button-quiet button-small" href={mixStudioApi.url(item.download_url!)} download>
                        Download
                      </a>
                      <button
                        type="button"
                        className="button button-quiet button-small"
                        onClick={() => {
                          if (!window.confirm("Delete this rendered file?")) return;
                          void act(async () => {
                            await mixStudioApi.deleteRender(item.id);
                            refresh();
                          });
                        }}
                      >
                        Delete
                      </button>
                    </div>
                  </>
                ) : (
                  <small className="dub-flag dub-flag-error">{item.error_message}</small>
                )}
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
