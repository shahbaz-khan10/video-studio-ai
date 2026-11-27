import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, RefObject } from "react";
import { formatTimestamp } from "../lib/format";
import {
  dubTimingApi,
  type TimingBundle,
  type TimingLanguage,
  type TimingLine,
  type TimingStatus,
  type Waveform,
} from "../lib/dubTimingApi";
import { useDubPreview, type PreviewClip } from "../hooks/useDubPreview";
import { JobProgress } from "./JobProgress";
import { ErrorState, LoadingState } from "./States";
import "./DubAudioPanel.css";
import "./DubTimingPanel.css";

interface DubTimingPanelProps {
  videoId: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  onJobStarted: (jobId: string) => void;
}

const ZOOMS = [10, 20, 40, 90];
const STATUS_LABEL: Record<TimingStatus, string> = {
  no_audio: "no audio",
  pending: "not fitted",
  stale: "take changed",
  fitted: "fits",
  fitted_fast: "sped up",
  needs_attention: "needs correction",
  failed: "failed",
};
const LANG: Record<TimingLanguage, string> = { ur: "Urdu", hi: "Hindi" };

function ms(value: number | null | undefined) {
  return value == null ? "—" : `${(value / 1000).toFixed(2)}s`;
}

function signed(value: number | null | undefined) {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${(value / 1000).toFixed(2)}s`;
}

function message(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback;
}

function WaveCanvas({ wave, color, height = 46 }: { wave: Waveform | null; color: string; height?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const width = canvas.clientWidth || 800;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const g = canvas.getContext("2d");
    if (!g) return;
    g.scale(ratio, ratio);
    g.clearRect(0, 0, width, height);
    if (!wave) return;
    const mid = height / 2;
    g.fillStyle = color;
    const n = wave.peaks.length;
    wave.peaks.forEach(([low, high], i) => {
      const x = (i / n) * width;
      const top = mid - Math.max(0.5, high * mid);
      const bottom = mid - Math.min(-0.5, low * mid);
      g.fillRect(x, top, Math.max(1, width / n - 0.3), bottom - top);
    });
  }, [wave, color, height]);
  return <canvas ref={ref} className="timing-wave" style={{ height }} aria-hidden="true" />;
}

export function DubTimingPanel({ videoId, videoRef, onJobStarted }: DubTimingPanelProps) {
  const [language, setLanguage] = useState<TimingLanguage>("ur");
  const [bundle, setBundle] = useState<TimingBundle | null>(null);
  const [other, setOther] = useState<TimingBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [zoom, setZoom] = useState(20);
  const [viewStart, setViewStart] = useState(0);
  const [waves, setWaves] = useState<Record<string, Waveform | null>>({});
  const [playhead, setPlayhead] = useState(0);
  const [mode, setMode] = useState<"pending" | "failed" | "all">("pending");
  const [overwrite, setOverwrite] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobActive, setJobActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<"off" | TimingLanguage>("off");
  const [muteOriginal, setMuteOriginal] = useState(true);
  const [drag, setDrag] = useState<{ segmentId: string; lane: TimingLanguage; startX: number; deltaMs: number } | null>(null);
  const [form, setForm] = useState<{ start: string; end: string; overlap: string }>({ start: "", end: "", overlap: "0" });
  const laneRef = useRef<HTMLDivElement>(null);

  const otherLanguage: TimingLanguage = language === "ur" ? "hi" : "ur";
  const refresh = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    Promise.all([dubTimingApi.bundle(videoId, language), dubTimingApi.bundle(videoId, otherLanguage)])
      .then(([main, second]) => {
        if (!active) return;
        setBundle(main);
        setOther(second);
        const job = main.job;
        if (job && (job.status === "queued" || job.status === "running")) {
          setJobId(job.id);
          setJobActive(true);
        } else if (job && job.status === "failed") setJobId(job.id);
      })
      .catch((reason: unknown) => active && setError(message(reason, "Could not load dialogue timing.")))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [videoId, language, otherLanguage, version]);

  const durationMs = bundle?.duration_ms ?? Math.max(10000, ...(bundle?.lines.map((line) => line.end_ms + 2000) ?? [0]));
  const spanMs = zoom * 1000;
  const viewEnd = viewStart + spanMs;

  useEffect(() => {
    let active = true;
    const sources: ("original" | TimingLanguage)[] = ["original", "ur", "hi"];
    Promise.all(
      sources.map((source) =>
        dubTimingApi.waveform(videoId, source, viewStart, viewEnd, 700).catch(() => null),
      ),
    ).then((results) => {
      if (!active) return;
      setWaves({ original: results[0], ur: results[1], hi: results[2] });
    });
    return () => {
      active = false;
    };
  }, [videoId, viewStart, viewEnd, version]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const tick = () => setPlayhead(video.currentTime * 1000);
    video.addEventListener("timeupdate", tick);
    video.addEventListener("seeked", tick);
    return () => {
      video.removeEventListener("timeupdate", tick);
      video.removeEventListener("seeked", tick);
    };
  }, [videoRef]);

  const byLanguage = useMemo(() => {
    const map: Partial<Record<TimingLanguage, TimingBundle>> = {};
    if (bundle) map[bundle.language] = bundle;
    if (other) map[other.language] = other;
    return map;
  }, [bundle, other]);

  const previewClips: PreviewClip[] = useMemo(() => {
    if (preview === "off") return [];
    return (byLanguage[preview]?.lines ?? [])
      .filter((line) => line.placement?.audio_url && line.placement.placed_start_ms != null && ["fitted", "fitted_fast", "needs_attention", "stale"].includes(line.status))
      .map((line) => ({
        id: line.placement!.id,
        url: dubTimingApi.url(line.placement!.audio_url!),
        version: line.placement!.updated_at ?? "",
        startMs: line.placement!.placed_start_ms!,
        endMs: line.placement!.placed_end_ms!,
      }));
  }, [preview, byLanguage]);
  const sync = useDubPreview(videoRef, previewClips, preview !== "off", muteOriginal);

  const current = bundle?.lines.find((line) => line.segment_id === selected) ?? null;
  useEffect(() => {
    const placement = current?.placement;
    setForm({
      start: String((placement?.manual ? placement.manual_start_ms : current?.start_ms) ?? ""),
      end: String((placement?.manual ? placement.manual_end_ms : current?.end_ms) ?? ""),
      overlap: String(placement?.allow_overlap_ms ?? 0),
    });
  }, [current?.segment_id, current?.placement?.updated_at]);

  const replace = (next: TimingLine) =>
    setBundle((value) => (value ? { ...value, lines: value.lines.map((line) => (line.segment_id === next.segment_id ? next : line)) } : value));

  const act = async (task: () => Promise<void>) => {
    setBusy(true);
    setActionError(null);
    try {
      await task();
    } catch (reason) {
      setActionError(message(reason, "Action failed."));
    } finally {
      setBusy(false);
    }
  };

  const seek = (timeMs: number, play = false) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = Math.max(0, timeMs) / 1000;
    if (play) void video.play().catch(() => undefined);
  };

  const focus = (line: TimingLine) => {
    setSelected(line.segment_id);
    const start = line.placement?.placed_start_ms ?? line.start_ms;
    if (start < viewStart || start > viewEnd - 1000) setViewStart(Math.max(0, start - spanMs * 0.25));
  };

  const xToMs = (x: number) => {
    const width = laneRef.current?.clientWidth || 1;
    return (x / width) * spanMs;
  };
  const pct = (time: number) => `${((time - viewStart) / spanMs) * 100}%`;

  const onBlockDown = (event: ReactPointerEvent, line: TimingLine, lane: TimingLanguage) => {
    if (lane !== language || line.placement?.approved) {
      setSelected(line.segment_id);
      return;
    }
    (event.target as HTMLElement).setPointerCapture(event.pointerId);
    setSelected(line.segment_id);
    setDrag({ segmentId: line.segment_id, lane, startX: event.clientX, deltaMs: 0 });
  };
  const onBlockMove = (event: ReactPointerEvent) => {
    if (!drag) return;
    setDrag({ ...drag, deltaMs: Math.round(xToMs(event.clientX - drag.startX) / 10) * 10 });
  };
  const onBlockUp = () => {
    if (!drag) return;
    const line = bundle?.lines.find((item) => item.segment_id === drag.segmentId);
    const delta = drag.deltaMs;
    setDrag(null);
    if (!line || Math.abs(delta) < 20) return;
    const windowStart = line.placement?.manual ? line.placement.manual_start_ms! : line.start_ms;
    const windowEnd = line.placement?.manual ? line.placement.manual_end_ms! : line.end_ms;
    void act(async () =>
      replace(await dubTimingApi.update(videoId, line.segment_id, { language, start_ms: Math.max(0, windowStart + delta), end_ms: windowEnd + delta })),
    );
  };

  if (loading && !bundle) return <LoadingState label="Loading dialogue timing…" />;
  if (error && !bundle) return <ErrorState message={error} onRetry={refresh} />;
  if (!bundle) return null;
  const summary = bundle.summary;
  const visibleLines = bundle.lines.filter((line) => line.end_ms > viewStart - 2000 && line.start_ms < viewEnd + 2000);

  const lane = (lang: TimingLanguage) => {
    const data = byLanguage[lang];
    return (
      <div className={`timing-lane ${lang === language ? "is-editable" : ""}`}>
        <div className="timing-lane-label">
          <strong>{LANG[lang]} dub</strong>
          <small>{lang === language ? "drag to move" : "view"}</small>
        </div>
        <div className="timing-lane-body" ref={lang === language ? laneRef : undefined}>
          <WaveCanvas wave={waves[lang] ?? null} color={lang === "ur" ? "rgba(52,211,153,0.75)" : "rgba(56,189,248,0.75)"} />
          {(data?.lines ?? [])
            .filter((line) => line.placement?.placed_start_ms != null && line.end_ms > viewStart - 2000 && line.start_ms < viewEnd + 2000)
            .map((line) => {
              const p = line.placement!;
              const shift = drag && drag.segmentId === line.segment_id && drag.lane === lang ? drag.deltaMs : 0;
              const left = p.placed_start_ms! + shift;
              const width = p.placed_end_ms! - p.placed_start_ms!;
              return (
                <button
                  type="button"
                  key={line.segment_id}
                  className={`timing-block timing-${line.status}${selected === line.segment_id ? " is-selected" : ""}${p.approved ? " is-approved" : ""}`}
                  style={{ left: pct(left), width: `${(width / spanMs) * 100}%` }}
                  title={`${line.character_name ?? "?"} · ${STATUS_LABEL[line.status]} · ×${p.tempo}`}
                  onPointerDown={(event) => onBlockDown(event, line, lang)}
                  onPointerMove={onBlockMove}
                  onPointerUp={onBlockUp}
                >
                  <span>{line.character_name ?? "?"}</span>
                </button>
              );
            })}
        </div>
      </div>
    );
  };

  return (
    <div className="timing-panel">
      <div className="timing-toolbar">
        <div className="dub-segmented" role="group" aria-label="Language">
          {(["ur", "hi"] as TimingLanguage[]).map((lang) => (
            <button key={lang} type="button" className={language === lang ? "is-active" : ""} onClick={() => setLanguage(lang)}>
              {LANG[lang]}
            </button>
          ))}
        </div>
        <select value={mode} onChange={(event) => setMode(event.target.value as typeof mode)} aria-label="Fit mode">
          <option value="pending">New &amp; changed lines</option>
          <option value="failed">Failed / needs correction</option>
          <option value="all">Refit all</option>
        </select>
        <label className="dub-check">
          <input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} />
          Overwrite manual &amp; approved timing
        </label>
        <button
          type="button"
          className="button button-primary"
          disabled={jobActive || busy}
          onClick={() => {
            if (overwrite && !window.confirm("Reset ALL manual and approved timing for this language and refit?")) return;
            void act(async () => {
              const job = await dubTimingApi.fitBatch(videoId, { language, mode, overwrite_manual: overwrite });
              setJobId(job.id);
              setJobActive(true);
              onJobStarted(job.id);
            });
          }}
        >
          {jobActive ? "Fitting…" : `Fit ${LANG[language]} timing`}
        </button>
        <span className="dub-spacer" />
        <label className="dub-check">
          Preview
          <select value={preview} onChange={(event) => setPreview(event.target.value as typeof preview)} aria-label="Dub preview">
            <option value="off">Off (original)</option>
            <option value="ur">Urdu dub</option>
            <option value="hi">Hindi dub</option>
          </select>
        </label>
        <label className="dub-check">
          <input type="checkbox" checked={muteOriginal} onChange={(event) => setMuteOriginal(event.target.checked)} />
          Mute original
        </label>
        {bundle.track?.exists && (
          <a className="button button-quiet button-small" href={dubTimingApi.url(`${bundle.track.url}&download=1`)} download>
            {bundle.track.stale ? "Download track (outdated)" : "Download dialogue track"}
          </a>
        )}
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

      <div className="dub-summary">
        {summary.total ?? 0} lines · {summary.fitted ?? 0} fit naturally · <span className="dub-warn">{summary.fitted_fast ?? 0} sped up</span> ·{" "}
        <span className="dub-bad">{summary.needs_attention ?? 0} need correction</span> · {summary.pending ?? 0} not fitted · {summary.stale ?? 0} take
        changed · {summary.no_audio ?? 0} without audio
        {preview !== "off" && (
          <span>
            {" "}
            · preview {LANG[preview]}: {sync.scheduled} clips scheduled, {sync.corrections} drift corrections
          </span>
        )}
      </div>
      <p className="timing-note">
        Audio timing only — mouth movement is matched in Phase 13. Speed changes stay within ×{bundle.limits.natural_tempo} (natural) and never exceed ×
        {bundle.limits.max_tempo}; words are never cut.
      </p>
      {actionError && (
        <p className="form-error" role="alert">
          {actionError}
        </p>
      )}

      <div className="timing-viewport">
        <div className="timing-viewport-controls">
          <button type="button" className="button button-quiet button-small" onClick={() => setViewStart(Math.max(0, viewStart - spanMs * 0.75))}>
            ◀
          </button>
          <input
            type="range"
            min={0}
            max={Math.max(0, durationMs - spanMs)}
            step={100}
            value={Math.min(viewStart, Math.max(0, durationMs - spanMs))}
            onChange={(event) => setViewStart(Number(event.target.value))}
            aria-label="Timeline position"
          />
          <button type="button" className="button button-quiet button-small" onClick={() => setViewStart(Math.min(Math.max(0, durationMs - spanMs), viewStart + spanMs * 0.75))}>
            ▶
          </button>
          <select value={zoom} onChange={(event) => setZoom(Number(event.target.value))} aria-label="Zoom">
            {ZOOMS.map((value) => (
              <option key={value} value={value}>
                {value}s view
              </option>
            ))}
          </select>
          <button type="button" className="button button-quiet button-small" onClick={() => setViewStart(Math.max(0, playhead - spanMs * 0.25))}>
            Go to playhead
          </button>
          <span className="muted">
            {formatTimestamp(viewStart)} – {formatTimestamp(viewEnd)}
          </span>
        </div>

        <div
          className="timing-ruler"
          onClick={(event) => {
            const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
            seek(viewStart + ((event.clientX - rect.left - 120) / (rect.width - 120)) * spanMs);
          }}
        >
          <span className="timing-ruler-label">time</span>
          <div className="timing-ruler-body">
            {Array.from({ length: 6 }, (_, i) => (
              <span key={i} style={{ left: `${(i / 5) * 100}%` }}>
                {formatTimestamp(viewStart + (i / 5) * spanMs)}
              </span>
            ))}
          </div>
        </div>

        <div className="timing-lanes">
          <div className="timing-lane">
            <div className="timing-lane-label">
              <strong>Original</strong>
              <small>{bundle.original_audio ?? "no audio"}</small>
            </div>
            <div className="timing-lane-body">
              <WaveCanvas wave={waves.original ?? null} color="rgba(196,181,253,0.7)" />
              {visibleLines.map((line) => (
                <button
                  type="button"
                  key={line.segment_id}
                  className={`timing-window${selected === line.segment_id ? " is-selected" : ""}${line.overlaps.length ? " has-overlap" : ""}`}
                  style={{ left: pct(line.start_ms), width: `${((line.end_ms - line.start_ms) / spanMs) * 100}%` }}
                  onClick={() => setSelected(line.segment_id)}
                  title={`${line.character_name ?? "?"}: ${line.source_text}`}
                >
                  <span>{line.character_name ?? "?"}</span>
                </button>
              ))}
            </div>
          </div>
          {lane("ur")}
          {lane("hi")}
          {playhead >= viewStart && playhead <= viewEnd && (
            <div className="timing-playhead" style={{ left: `calc(120px + (100% - 120px) * ${(playhead - viewStart) / spanMs})` }} />
          )}
          {current && (
            <>
              <div className="timing-marker" style={{ left: `calc(120px + (100% - 120px) * ${(current.start_ms - viewStart) / spanMs})` }} />
              <div className="timing-marker" style={{ left: `calc(120px + (100% - 120px) * ${(current.end_ms - viewStart) / spanMs})` }} />
            </>
          )}
        </div>
      </div>

      <div className="timing-body">
        <ol className="timing-list">
          {bundle.lines.map((line) => (
            <li key={line.segment_id}>
              <button type="button" className={`timing-row${selected === line.segment_id ? " is-selected" : ""}`} onClick={() => focus(line)}>
                <span className="dub-time">{formatTimestamp(line.start_ms)}</span>
                <strong>{line.character_name ?? "?"}</strong>
                <span className={`badge timing-badge-${line.status}`}>{STATUS_LABEL[line.status]}</span>
                <span className="muted">
                  {ms(line.duration_ms)} → {ms(line.placement?.fitted_duration_ms ?? line.take?.duration_ms)}
                  {line.placement && line.placement.tempo > 1.001 ? ` · ×${line.placement.tempo}` : ""}
                </span>
                {line.overlaps.length > 0 && <span className="timing-overlap-tag">overlap</span>}
                {line.placement?.manual && <span className="timing-overlap-tag">manual</span>}
                {line.placement?.approved && <span className="timing-overlap-tag">approved</span>}
              </button>
            </li>
          ))}
        </ol>

        <div className="timing-inspector">
          {!current ? (
            <p className="muted">Select a line in the list or timeline to see and adjust its timing.</p>
          ) : (
            <>
              <h3>
                {current.character_name ?? "Unassigned"} · {formatTimestamp(current.start_ms)}
              </h3>
              <p dir={language === "ur" ? "rtl" : "ltr"} lang={language} className={language === "ur" ? "urdu-text" : "hindi-text"}>
                {current.text || "—"}
              </p>
              <p className="muted" lang="en">
                {current.source_text}
              </p>
              <dl className="timing-facts">
                <dt>Original</dt>
                <dd>
                  {ms(current.start_ms)} → {ms(current.end_ms)} ({ms(current.duration_ms)})
                </dd>
                <dt>Generated take</dt>
                <dd>
                  {ms(current.take?.duration_ms)} (difference {signed(current.difference_ms)})
                </dd>
                <dt>Placed</dt>
                <dd>
                  {ms(current.placement?.placed_start_ms)} → {ms(current.placement?.placed_end_ms)} ({ms(current.placement?.fitted_duration_ms)})
                </dd>
                <dt>Fitting</dt>
                <dd>
                  {current.placement
                    ? `${current.placement.strategy ?? "—"} · ×${current.placement.tempo} · pauses −${current.placement.pause_saved_ms} ms${current.placement.overflow_ms ? ` · overflow ${current.placement.overflow_ms} ms` : ""}`
                    : "not fitted"}
                </dd>
                <dt>Room</dt>
                <dd>
                  {current.placement
                    ? `${current.placement.details.lead_room_ms ?? 0} ms before · ${current.placement.details.tail_room_ms ?? 0} ms after`
                    : "—"}
                </dd>
              </dl>
              {current.placement?.details.steps?.length ? <p className="muted">Steps: {current.placement.details.steps.join(" → ")}</p> : null}
              {current.placement?.flags.map((flag) => (
                <p key={flag.code} className={`dub-flag dub-flag-${flag.severity}`}>
                  {flag.message}
                </p>
              ))}
              {current.placement?.suggestions.length ? (
                <div className="timing-suggestions">
                  <strong>Ways to fix</strong>
                  {current.placement.suggestions.map((tip) => (
                    <div key={tip.action}>
                      <span>{tip.message}</span>
                      {tip.action === "regenerate_faster" && (
                        <button
                          type="button"
                          className="button button-quiet button-small"
                          disabled={busy}
                          onClick={() => void act(async () => replace(await dubTimingApi.regenerateFaster(videoId, current.segment_id, language, tip.speed_factor ?? 1.1)))}
                        >
                          Regenerate faster
                        </button>
                      )}
                      {tip.action === "allow_overlap" && (
                        <button
                          type="button"
                          className="button button-quiet button-small"
                          disabled={busy}
                          onClick={() => setForm({ ...form, overlap: String((current.placement?.overflow_ms ?? 0) + 50) })}
                        >
                          Fill in overlap
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="timing-form">
                <label>
                  Start (ms)
                  <input type="number" value={form.start} onChange={(event) => setForm({ ...form, start: event.target.value })} disabled={current.placement?.approved} />
                </label>
                <label>
                  End (ms)
                  <input type="number" value={form.end} onChange={(event) => setForm({ ...form, end: event.target.value })} disabled={current.placement?.approved} />
                </label>
                <label>
                  Allowed overlap (ms)
                  <input
                    type="number"
                    min={0}
                    max={1500}
                    value={form.overlap}
                    onChange={(event) => setForm({ ...form, overlap: event.target.value })}
                    disabled={current.placement?.approved}
                  />
                </label>
              </div>
              <div className="translation-actions">
                <button
                  type="button"
                  className="button button-small"
                  disabled={busy || current.placement?.approved || !current.take}
                  onClick={() =>
                    void act(async () =>
                      replace(
                        await dubTimingApi.update(videoId, current.segment_id, {
                          language,
                          start_ms: Number(form.start),
                          end_ms: Number(form.end),
                          allow_overlap_ms: Number(form.overlap) || 0,
                        }),
                      ),
                    )
                  }
                >
                  Save timing
                </button>
                <button
                  type="button"
                  className="button button-quiet button-small"
                  disabled={busy || current.placement?.approved || !current.take}
                  onClick={() => void act(async () => replace(await dubTimingApi.fitLine(videoId, current.segment_id, language)))}
                >
                  Refit
                </button>
                {current.placement?.manual && (
                  <button
                    type="button"
                    className="button button-quiet button-small"
                    disabled={busy || current.placement.approved}
                    onClick={() => void act(async () => replace(await dubTimingApi.update(videoId, current.segment_id, { language, reset: true })))}
                  >
                    Reset to original
                  </button>
                )}
                {current.placement && ["fitted", "fitted_fast", "needs_attention"].includes(current.placement.status) && (
                  <button
                    type="button"
                    className="button button-quiet button-small"
                    disabled={busy}
                    onClick={() =>
                      void act(async () => replace(await dubTimingApi.update(videoId, current.segment_id, { language, approved: !current.placement!.approved })))
                    }
                  >
                    {current.placement.approved ? "Unlock timing" : "Approve timing"}
                  </button>
                )}
                <button
                  type="button"
                  className="button button-quiet button-small"
                  onClick={() => {
                    setPreview(language);
                    seek((current.placement?.placed_start_ms ?? current.start_ms) - 1500, true);
                  }}
                >
                  Preview in video
                </button>
                <button type="button" className="button button-quiet button-small" onClick={() => seek(current.start_ms - 500, true)}>
                  Play original
                </button>
              </div>
              {current.placement?.audio_url && <audio controls preload="none" src={dubTimingApi.url(current.placement.audio_url)} />}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
