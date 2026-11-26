import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { formatTimestamp } from "../lib/format";
import {
  dubAudioApi,
  type Delivery,
  type DubBundle,
  type DubCharacter,
  type DubLanguage,
  type DubLine,
  type DubTake,
  type LineStatus,
} from "../lib/dubAudioApi";
import { JobProgress } from "./JobProgress";
import { ErrorState, LoadingState } from "./States";
import "./DubAudioPanel.css";

interface DubAudioPanelProps {
  videoId: string;
  onJobStarted: (jobId: string) => void;
}

interface Delivery_ {
  emotion: Delivery;
  speed: number;
  pitch: number;
  pauseMs: number | null;
}

const PAGE_SIZE = 30;
const LANG_LABEL: Record<DubLanguage, string> = { ur: "Urdu", hi: "Hindi" };
const STATUS_LABEL: Record<LineStatus, string> = {
  no_text: "no translation",
  missing: "not generated",
  ready: "ready",
  approved: "approved",
  stale_text: "text changed",
  stale_voice: "voice changed",
  failed: "failed",
};
const SOURCE_LABEL = {
  default: "Built-in default voice",
  phase9_profile: "Phase 9 voice profile",
  phase9_profile_fallback: "Phase 9 profile · built-in fallback",
} as const;
const DEFAULT_DELIVERY: Delivery_ = { emotion: "neutral", speed: 1, pitch: 0, pauseMs: null };

function messageOf(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback;
}

export function DubAudioPanel({ videoId, onJobStarted }: DubAudioPanelProps) {
  const [language, setLanguage] = useState<DubLanguage>("ur");
  const [bundle, setBundle] = useState<DubBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [characterFilter, setCharacterFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | LineStatus | "needs_work">("all");
  const [mode, setMode] = useState<"pending" | "missing" | "failed" | "all">("pending");
  const [includeApproved, setIncludeApproved] = useState(false);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [delivery, setDelivery] = useState<Record<string, Delivery_>>({});
  const [openTakes, setOpenTakes] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobActive, setJobActive] = useState(false);
  const [page, setPage] = useState(0);
  const [version, setVersion] = useState(0);
  const [preview, setPreview] = useState<{ key: string; url: string } | null>(null);
  const previewRef = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    dubAudioApi
      .bundle(videoId, language)
      .then((result) => {
        if (!active) return;
        setBundle(result);
        const job = result.job;
        if (job && (job.status === "queued" || job.status === "running")) {
          setJobId(job.id);
          setJobActive(true);
        } else if (job && job.status === "failed") {
          setJobId(job.id);
        }
      })
      .catch((reason: unknown) => active && setError(messageOf(reason, "Could not load voice generation.")))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [videoId, language, version]);

  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview.url);
  }, [preview]);

  const refresh = useCallback(() => setVersion((value) => value + 1), []);

  const replaceLine = useCallback((next: DubLine) => {
    setBundle((current) =>
      current ? { ...current, lines: current.lines.map((line) => (line.segment_id === next.segment_id ? next : line)) } : current,
    );
  }, []);

  const voiceFor = useCallback(
    (line: DubLine): DubCharacter | undefined =>
      bundle?.characters.find((item) =>
        line.character_id ? item.character_id === line.character_id : item.key === `label:${line.speaker_label ?? "unknown"}`,
      ),
    [bundle],
  );

  const run = async (key: string, task: () => Promise<void>) => {
    setBusy((value) => ({ ...value, [key]: true }));
    setActionError(null);
    try {
      await task();
    } catch (reason) {
      setActionError(messageOf(reason, "Action failed."));
    } finally {
      setBusy((value) => ({ ...value, [key]: false }));
    }
  };

  const generateLine = (line: DubLine, variation: boolean) =>
    run(`gen:${line.segment_id}`, async () => {
      const d = delivery[line.segment_id] ?? DEFAULT_DELIVERY;
      const next = await dubAudioApi.generateLine(videoId, line.segment_id, {
        language,
        emotion: d.emotion,
        speed_factor: d.speed !== 1 ? d.speed : null,
        pitch_offset: d.pitch !== 0 ? d.pitch : null,
        pause_ms: d.pauseMs,
        new_variation: variation,
      });
      replaceLine(next);
    });

  const startBatch = () =>
    run("batch", async () => {
      const job = await dubAudioApi.generateBatch(videoId, {
        language,
        mode,
        include_approved: includeApproved,
        character_id: characterFilter !== "all" && !characterFilter.startsWith("label:") ? characterFilter : null,
      });
      setJobId(job.id);
      setJobActive(true);
      onJobStarted(job.id);
    });

  const playPreview = (character: DubCharacter) =>
    run(`preview:${character.key}`, async () => {
      const url = await dubAudioApi.preview(videoId, character.key, language);
      setPreview((old) => {
        if (old) URL.revokeObjectURL(old.url);
        return { key: character.key, url };
      });
      window.setTimeout(() => void previewRef.current?.play().catch(() => undefined), 50);
    });

  const lines = bundle?.lines ?? [];
  const filtered = useMemo(
    () =>
      lines.filter((line) => {
        if (characterFilter !== "all") {
          const key = line.character_id ?? `label:${line.speaker_label ?? "unknown"}`;
          if (key !== characterFilter) return false;
        }
        if (statusFilter === "all") return true;
        if (statusFilter === "needs_work") return ["missing", "failed", "stale_text", "stale_voice"].includes(line.status);
        return line.status === statusFilter;
      }),
    [lines, characterFilter, statusFilter],
  );
  useEffect(() => setPage(0), [characterFilter, statusFilter, language]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visible = filtered.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);

  if (loading && !bundle) return <LoadingState label="Loading voice generation…" />;
  if (error && !bundle) return <ErrorState message={error} onRetry={refresh} />;
  if (!bundle) return null;

  const engine = bundle.engine;
  const summary = bundle.summary;

  return (
    <div className="dub-panel">
      <p className="dub-disclosure" role="note">
        {engine.synthetic_disclosure} Built-in engine: {engine.builtin.label} · {engine.builtin.license}
        {engine.builtin.commercial_use === false ? " (non-commercial)" : ""} · {engine.sample_rate / 1000} kHz mono.
        {engine.phase9_available ? " Characters with a Phase 9 voice profile use their profile's engine." : ""}
      </p>

      <div className="dub-toolbar">
        <div className="dub-segmented" role="group" aria-label="Language">
          {(["ur", "hi"] as DubLanguage[]).map((lang) => (
            <button
              key={lang}
              type="button"
              className={language === lang ? "is-active" : ""}
              onClick={() => setLanguage(lang)}
              aria-pressed={language === lang}
            >
              {LANG_LABEL[lang]}
            </button>
          ))}
        </div>
        <select value={characterFilter} onChange={(event) => setCharacterFilter(event.target.value)} aria-label="Character">
          <option value="all">All characters</option>
          {bundle.characters.map((character) => (
            <option key={character.key} value={character.character_id ?? character.key}>
              {character.name} ({character.lines})
            </option>
          ))}
        </select>
        <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)} aria-label="Status">
          <option value="all">All lines</option>
          <option value="needs_work">Needs audio</option>
          <option value="failed">Failed</option>
          <option value="ready">Ready</option>
          <option value="approved">Approved</option>
          <option value="stale_text">Text changed</option>
          <option value="no_text">No translation</option>
        </select>
        <span className="dub-spacer" />
        <select value={mode} onChange={(event) => setMode(event.target.value as typeof mode)} aria-label="Batch mode">
          <option value="pending">Missing, changed &amp; failed</option>
          <option value="missing">Only missing</option>
          <option value="failed">Retry failed only</option>
          <option value="all">Regenerate all</option>
        </select>
        <label className="dub-check">
          <input type="checkbox" checked={includeApproved} onChange={(event) => setIncludeApproved(event.target.checked)} />
          Include approved
        </label>
        <button type="button" className="button button-primary" disabled={jobActive || busy.batch} onClick={() => void startBatch()}>
          {jobActive ? "Generating…" : `Generate ${LANG_LABEL[language]}${characterFilter !== "all" ? " for character" : ""}`}
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

      <div className="dub-summary">
        {summary.total ?? 0} lines · {summary.ready ?? 0} ready · {summary.approved ?? 0} approved · {summary.missing ?? 0} not generated
        {summary.failed ? <span className="dub-warn"> · {summary.failed} failed</span> : null}
        {summary.stale_text || summary.stale_voice ? (
          <span className="dub-warn"> · {(summary.stale_text ?? 0) + (summary.stale_voice ?? 0)} changed since generated</span>
        ) : null}
        {summary.no_text ? <span> · {summary.no_text} without translation</span> : null}
      </div>

      {actionError && (
        <p className="form-error" role="alert">
          {actionError}
        </p>
      )}

      <div className="dub-voices">
        {bundle.characters.map((character) => (
          <div className="dub-voice" key={character.key}>
            <div className="dub-voice-head">
              <strong>{character.name}</strong>
              <span className={`badge dub-source-${character.voice.source}`}>{SOURCE_LABEL[character.voice.source]}</span>
            </div>
            <small>{character.voice.engine_label}</small>
            <small>
              {[...character.voice.applied, ...character.voice.shaped].join(" · ") || "engine defaults"}
            </small>
            {character.voice.notes.map((note) => (
              <small key={note} className="dub-note">
                {note}
              </small>
            ))}
            <small>
              Delivery: {character.voice.emotions.length > 1 ? character.voice.emotions.join(", ") : "neutral only (engine has no emotion control)"}
            </small>
            <button
              type="button"
              className="button button-quiet button-small"
              disabled={busy[`preview:${character.key}`]}
              onClick={() => void playPreview(character)}
            >
              {busy[`preview:${character.key}`] ? "Synthesising…" : "Preview voice"}
            </button>
            {preview?.key === character.key && <audio ref={previewRef} controls src={preview.url} />}
          </div>
        ))}
      </div>

      {!lines.length ? (
        <div className="transcript-empty">
          <strong>No dialogue yet</strong>
          <p>Transcribe and translate the video first; each translated line can then be voiced here.</p>
        </div>
      ) : (
        <ol className="dub-lines">
          {visible.map((line) => {
            const character = voiceFor(line);
            const emotions = character?.voice.emotions ?? ["neutral"];
            const d = delivery[line.segment_id] ?? DEFAULT_DELIVERY;
            const setD = (patch: Partial<Delivery_>) =>
              setDelivery((value) => ({ ...value, [line.segment_id]: { ...(value[line.segment_id] ?? DEFAULT_DELIVERY), ...patch } }));
            const take = line.current_take;
            const latestFailed = line.takes[0]?.status === "failed" ? line.takes[0] : null;
            const generating = busy[`gen:${line.segment_id}`];
            return (
              <li key={line.segment_id} className={`dub-line dub-status-${line.status}`}>
                <div className="dub-line-text">
                  <div className="dub-line-meta">
                    <span className="dub-time">{formatTimestamp(line.start_ms)}</span>
                    <strong>{line.character_name ?? line.speaker_label ?? "Unassigned"}</strong>
                    <span className={`badge dub-badge-${line.status}`}>{STATUS_LABEL[line.status]}</span>
                    {take && <span className="dub-ai-tag">synthetic · take {take.take_number}</span>}
                  </div>
                  <p dir={language === "ur" ? "rtl" : "ltr"} lang={language} className={language === "ur" ? "urdu-text" : "hindi-text"}>
                    {line.text || <em className="muted">Not translated yet</em>}
                  </p>
                  <small className="muted" lang="en">
                    {line.source_text}
                  </small>
                </div>

                <div className="dub-line-controls">
                  <label>
                    Delivery
                    <select value={d.emotion} onChange={(event) => setD({ emotion: event.target.value as Delivery })}>
                      {engine.all_emotions.map((emotion) => (
                        <option key={emotion} value={emotion} disabled={!emotions.includes(emotion)}>
                          {emotion}
                          {!emotions.includes(emotion) ? " (not supported)" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Speed ×{d.speed.toFixed(2)}
                    <input type="range" min={0.8} max={1.25} step={0.05} value={d.speed} onChange={(event) => setD({ speed: Number(event.target.value) })} />
                  </label>
                  <label>
                    Pitch {d.pitch > 0 ? "+" : ""}
                    {d.pitch} st
                    <input type="range" min={-3} max={3} step={0.5} value={d.pitch} onChange={(event) => setD({ pitch: Number(event.target.value) })} />
                  </label>
                  <label>
                    Pause
                    <select
                      value={d.pauseMs ?? ""}
                      onChange={(event) => setD({ pauseMs: event.target.value === "" ? null : Number(event.target.value) })}
                    >
                      <option value="">default</option>
                      <option value="120">short</option>
                      <option value="450">long</option>
                      <option value="800">dramatic</option>
                    </select>
                  </label>
                </div>

                <div className="dub-line-audio">
                  {take?.audio_url ? (
                    <audio controls preload="none" src={dubAudioApi.audioUrl(take.audio_url, take.id)} aria-label={`Take ${take.take_number}`} />
                  ) : (
                    <span className="muted">No audio yet</span>
                  )}
                  {take && (
                    <small className="muted">
                      {((take.duration_ms ?? 0) / 1000).toFixed(1)}s · {take.emotion}
                      {take.voice.shaped.length ? ` · ${take.voice.shaped.join(", ")}` : ""}
                    </small>
                  )}
                  {[...(take?.qc_flags ?? []), ...(latestFailed?.qc_flags ?? [])]
                    .filter((flag, index, all) => all.findIndex((item) => item.code === flag.code) === index)
                    .map((flag) => (
                      <small key={flag.code} className={`dub-flag dub-flag-${flag.severity}`}>
                        {flag.message}
                      </small>
                    ))}
                  <div className="translation-actions">
                    <button
                      type="button"
                      className="button button-small"
                      disabled={!line.text || generating}
                      onClick={() => void generateLine(line, Boolean(take))}
                    >
                      {generating ? "Generating…" : take ? "Regenerate" : "Generate"}
                    </button>
                    {take && (
                      <button
                        type="button"
                        className="button button-quiet button-small"
                        onClick={() => void run(`approve:${take.id}`, async () => replaceLine(await dubAudioApi.approve(take.id, !take.approved)))}
                      >
                        {take.approved ? "Unapprove" : "Approve"}
                      </button>
                    )}
                    {take?.download_url && (
                      <a className="button button-quiet button-small" href={dubAudioApi.audioUrl(take.download_url)} download>
                        Download
                      </a>
                    )}
                    {line.takes.length > 1 && (
                      <button
                        type="button"
                        className="button button-quiet button-small"
                        onClick={() => setOpenTakes(openTakes === line.segment_id ? null : line.segment_id)}
                      >
                        Takes ({line.takes.length})
                      </button>
                    )}
                  </div>
                  {openTakes === line.segment_id && (
                    <ul className="dub-takes">
                      {line.takes.map((item: DubTake) => (
                        <li key={item.id}>
                          <span>
                            Take {item.take_number} · {item.status}
                            {item.is_current ? " · in use" : ""}
                            {item.approved ? " · approved" : ""} · {item.emotion}
                          </span>
                          {item.audio_url ? (
                            <audio controls preload="none" src={dubAudioApi.audioUrl(item.audio_url, item.id)} />
                          ) : (
                            <small className="dub-flag dub-flag-error">{item.error_message}</small>
                          )}
                          {item.status === "ready" && !item.is_current && (
                            <button
                              type="button"
                              className="button button-quiet button-small"
                              onClick={() => void run(`select:${item.id}`, async () => replaceLine(await dubAudioApi.select(item.id)))}
                            >
                              Use this take
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {pageCount > 1 && (
        <div className="translation-pager">
          <button type="button" className="button button-quiet" disabled={page === 0} onClick={() => setPage(page - 1)}>
            Previous
          </button>
          <span>
            Page {page + 1} of {pageCount}
          </span>
          <button type="button" className="button button-quiet" disabled={page >= pageCount - 1} onClick={() => setPage(page + 1)}>
            Next
          </button>
        </div>
      )}
    </div>
  );
}
