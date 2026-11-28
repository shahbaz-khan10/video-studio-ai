import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import { formatTimestamp } from "../lib/format";
import { lipsyncApi, type FaceAnalysis, type LipBundle, type LipLanguage, type LipLine, type LipResult } from "../lib/lipsyncApi";
import { JobProgress } from "./JobProgress";
import { ErrorState, LoadingState } from "./States";
import "./DubAudioPanel.css";
import "./LipSyncPanel.css";

interface LipSyncPanelProps {
  videoId: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  onJobStarted: (jobId: string) => void;
}

const LANG: Record<LipLanguage, string> = { ur: "Urdu", hi: "Hindi" };
const STATUS: Record<string, string> = {
  generated: "generated",
  failed: "failed",
  needs_face: "choose face",
  no_face: "no visible face",
  no_audio: "no dub audio",
};
const SOURCE: Record<string, string> = {
  manual: "you chose this face",
  manual_off_screen: "marked off-screen",
  phase7_confirmed: "confirmed in Character List",
  audio_visual_evidence: "mouth moves with the original line",
  needs_face_selection: "speaker unclear",
  no_visible_face: "nobody visible",
};

function message(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback;
}

function lineState(line: LipLine): string {
  const r = line.current ?? line.latest;
  if (!line.dub) return "no_audio";
  if (!r) return "not_started";
  if (r.status === "generated") {
    if (r.stale) return "stale";
    return r.review === "accepted" ? "accepted" : r.review === "rejected" ? "rejected" : "review";
  }
  return r.status;
}

function Score({ label, score }: { label: string; score?: { confidence: number; best_offset_frames: number } | null }) {
  return (
    <div className="lip-score">
      <span>{label}</span>
      <strong>{score ? score.confidence.toFixed(3) : "—"}</strong>
      <small>{score ? `offset ${score.best_offset_frames} fr` : "not measured"}</small>
    </div>
  );
}

function Compare({ result }: { result: LipResult }) {
  const before = useRef<HTMLVideoElement>(null);
  const after = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [listen, setListen] = useState<"after" | "before">("after");

  useEffect(() => {
    const a = after.current;
    const b = before.current;
    if (!a || !b) return;
    const sync = () => {
      if (Math.abs(b.currentTime - a.currentTime) > 0.04) b.currentTime = a.currentTime;
    };
    const ended = () => setPlaying(false);
    a.addEventListener("timeupdate", sync);
    a.addEventListener("seeked", sync);
    a.addEventListener("ended", ended);
    return () => {
      a.removeEventListener("timeupdate", sync);
      a.removeEventListener("seeked", sync);
      a.removeEventListener("ended", ended);
    };
  }, [result.id]);

  useEffect(() => {
    if (after.current) after.current.muted = listen !== "after";
    if (before.current) before.current.muted = listen !== "before";
  }, [listen, result.id]);

  const toggle = async () => {
    const a = after.current;
    const b = before.current;
    if (!a || !b) return;
    if (playing) {
      a.pause();
      b.pause();
      setPlaying(false);
    } else {
      b.currentTime = a.currentTime;
      await Promise.all([a.play(), b.play()]).catch(() => undefined);
      setPlaying(true);
    }
  };

  return (
    <div className="lip-compare">
      <div className="lip-compare-grid">
        <figure>
          <figcaption>Before — original picture + dubbed audio</figcaption>
          {result.before_url && <video ref={before} src={lipsyncApi.url(result.before_url)} preload="auto" playsInline />}
        </figure>
        <figure>
          <figcaption>After — AI lip sync ({result.engine})</figcaption>
          {result.after_url && <video ref={after} src={lipsyncApi.url(result.after_url)} preload="auto" playsInline controls />}
        </figure>
      </div>
      <div className="translation-actions">
        <button type="button" className="button button-small" onClick={() => void toggle()}>
          {playing ? "Pause both" : "Play both in sync"}
        </button>
        <label className="dub-check">
          Hear
          <select value={listen} onChange={(event) => setListen(event.target.value as "after" | "before")}>
            <option value="after">after</option>
            <option value="before">before</option>
          </select>
        </label>
        {result.after_url && (
          <a className="button button-quiet button-small" href={lipsyncApi.url(`${result.after_url}&download=1`)} download>
            Download result
          </a>
        )}
      </div>
    </div>
  );
}

export function LipSyncPanel({ videoId, videoRef, onJobStarted }: LipSyncPanelProps) {
  const [language, setLanguage] = useState<LipLanguage>("ur");
  const [bundle, setBundle] = useState<LipBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [analysis, setAnalysis] = useState<FaceAnalysis | null>(null);
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobActive, setJobActive] = useState(false);
  const [note, setNote] = useState("");
  const [showModels, setShowModels] = useState(false);
  const dubRef = useRef<HTMLAudioElement>(null);

  const refresh = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    lipsyncApi
      .bundle(videoId, language)
      .then((result) => {
        if (!active) return;
        setBundle(result);
        if (result.job && (result.job.status === "queued" || result.job.status === "running")) {
          setJobId(result.job.id);
          setJobActive(true);
        } else if (result.job?.status === "failed") setJobId(result.job.id);
      })
      .catch((reason: unknown) => active && setError(message(reason, "Could not load lip sync.")))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [videoId, language, version]);

  const line = bundle?.lines.find((item) => item.segment_id === selected) ?? null;
  useEffect(() => {
    setAnalysis(null);
    setNote("");
  }, [selected, language]);

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

  const analyze = () => line && act(async () => setAnalysis(await lipsyncApi.analyze(videoId, line.segment_id, language)));

  const generate = (ids: string[]) =>
    act(async () => {
      const job = await lipsyncApi.generate(videoId, language, ids);
      setJobId(job.id);
      setJobActive(true);
      onJobStarted(job.id);
    });

  const pickFace = (box: number[]) =>
    line &&
    analysis &&
    act(async () => {
      await lipsyncApi.chooseFace(videoId, line.segment_id, { time_ms: analysis.frame_time_ms, box, character_id: line.character_id });
      refresh();
      setAnalysis(await lipsyncApi.analyze(videoId, line.segment_id, language));
    });

  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    (bundle?.lines ?? []).forEach((item) => {
      const state = lineState(item);
      out[state] = (out[state] ?? 0) + 1;
    });
    return out;
  }, [bundle]);

  if (loading && !bundle) return <LoadingState label="Loading lip sync…" />;
  if (error && !bundle) return <ErrorState message={error} onRetry={refresh} />;
  if (!bundle) return null;
  const engine = bundle.engine;
  const result = line ? line.latest : null;
  const playOriginal = () => {
    const video = videoRef.current;
    if (!video || !line) return;
    video.currentTime = Math.max(0, line.start_ms - 300) / 1000;
    void video.play().catch(() => undefined);
  };

  return (
    <div className="lip-panel">
      <p className="dub-disclosure" role="note">
        {engine.disclosure} Engine: <strong>{engine.backend}</strong> on {engine.device}
        {engine.backend === "wav2lip" ? " (Wav2Lip, research/non-commercial licence, 96 px mouth region)" : ""}
        {engine.remote_configured ? ` · GPU worker: ${engine.remote_model}` : ""}
        {engine.gpu_detected ? ` · local GPU: ${engine.gpu_detected}` : ""}.{" "}
        <button type="button" className="link-button" onClick={() => setShowModels(!showModels)}>
          {showModels ? "hide models" : "models evaluated"}
        </button>
      </p>
      {showModels && (
        <table className="lip-models">
          <thead>
            <tr>
              <th>Model</th>
              <th>Licence</th>
              <th>VRAM</th>
              <th>Status here</th>
            </tr>
          </thead>
          <tbody>
            {engine.models_evaluated.map((item) => (
              <tr key={item.name}>
                <td>{item.name}</td>
                <td>{item.license}</td>
                <td>{item.vram}</td>
                <td>{item.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="timing-toolbar">
        <div className="dub-segmented" role="group" aria-label="Language driving the lips">
          {(["ur", "hi"] as LipLanguage[]).map((lang) => (
            <button key={lang} type="button" className={language === lang ? "is-active" : ""} onClick={() => setLanguage(lang)}>
              {LANG[lang]} audio
            </button>
          ))}
        </div>
        <span className="dub-summary">
          {bundle.lines.length} lines · {counts.review ?? 0} to review · {counts.accepted ?? 0} accepted · {counts.stale ?? 0} out of date · {counts.needs_face ?? 0} need a face ·{" "}
          {counts.no_face ?? 0} off-screen · {counts.no_audio ?? 0} without dub audio
        </span>
        <span className="dub-spacer" />
        <button
          type="button"
          className="button button-primary"
          disabled={busy || jobActive || checked.size === 0}
          onClick={() => void generate([...checked])}
        >
          {jobActive ? "Processing…" : `Lip-sync ${checked.size || ""} selected`}
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
      {actionError && (
        <p className="form-error" role="alert">
          {actionError}
        </p>
      )}

      <div className="lip-body">
        <ol className="lip-list">
          {bundle.lines.map((item) => {
            const state = lineState(item);
            return (
              <li key={item.segment_id} className={selected === item.segment_id ? "is-selected" : ""}>
                <input
                  type="checkbox"
                  aria-label={`Select line at ${formatTimestamp(item.start_ms)}`}
                  checked={checked.has(item.segment_id)}
                  disabled={!item.dub}
                  onChange={(event) =>
                    setChecked((current) => {
                      const next = new Set(current);
                      if (event.target.checked) next.add(item.segment_id);
                      else next.delete(item.segment_id);
                      return next;
                    })
                  }
                />
                <button type="button" className="lip-row" onClick={() => setSelected(item.segment_id)}>
                  <span className="dub-time">{formatTimestamp(item.start_ms)}</span>
                  <strong>{item.character_name ?? "?"}</strong>
                  <span className={`badge lip-state-${state}`}>{state.replace("_", " ")}</span>
                  <span className="muted">{item.text}</span>
                </button>
              </li>
            );
          })}
        </ol>

        <div className="timing-inspector">
          {!line ? (
            <p className="muted">Select a dialogue line. Lines need dubbed audio (Phases 10–11) before lip sync.</p>
          ) : (
            <>
              <h3>
                {line.character_name ?? "Unassigned"} · {formatTimestamp(line.start_ms)}–{formatTimestamp(line.end_ms)}
              </h3>
              <p className="muted">{line.text}</p>
              <div className="translation-actions">
                <button type="button" className="button button-quiet button-small" onClick={playOriginal}>
                  Play original in video
                </button>
                {line.dub && (
                  <>
                    <button type="button" className="button button-quiet button-small" onClick={() => void dubRef.current?.play()}>
                      Play {LANG[language]} dub
                    </button>
                    <audio ref={dubRef} src={lipsyncApi.url(line.dub.url)} preload="none" />
                  </>
                )}
                <button type="button" className="button button-quiet button-small" disabled={busy} onClick={() => void analyze()}>
                  {busy && !analysis ? "Finding faces…" : "Find faces"}
                </button>
                <button
                  type="button"
                  className="button button-quiet button-small"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      await lipsyncApi.chooseFace(videoId, line.segment_id, { time_ms: line.start_ms, off_screen: true });
                      refresh();
                    })
                  }
                >
                  Mark off-screen
                </button>
                {line.selection && (
                  <button
                    type="button"
                    className="button button-quiet button-small"
                    disabled={busy}
                    onClick={() =>
                      void act(async () => {
                        await lipsyncApi.clearFace(videoId, line.segment_id);
                        refresh();
                      })
                    }
                  >
                    Clear face choice
                  </button>
                )}
                <button
                  type="button"
                  className="button button-small"
                  disabled={busy || jobActive || !line.dub}
                  onClick={() => void generate([line.segment_id])}
                >
                  {line.attempts ? "Regenerate" : "Generate lip sync"}
                </button>
              </div>
              {line.selection && (
                <p className="muted">
                  Face choice: {line.selection.off_screen ? "off-screen (picture left unchanged)" : "chosen manually"}
                </p>
              )}

              {analysis && (
                <div className="lip-analysis">
                  <p className="muted">
                    {analysis.faces.length} face(s) in {analysis.frames} frames · {SOURCE[analysis.source] ?? analysis.source}. Click a face to choose who speaks.
                  </p>
                  <div className="lip-frame">
                    <img src={lipsyncApi.url(analysis.frame_url)} alt="Frame from this line" />
                    {analysis.faces.map((face) => (
                      <button
                        type="button"
                        key={face.track_id}
                        className={`lip-face${analysis.chosen_track === face.track_id ? " is-chosen" : ""}`}
                        style={{ left: `${face.box[0] * 100}%`, top: `${face.box[1] * 100}%`, width: `${face.box[2] * 100}%`, height: `${face.box[3] * 100}%` }}
                        onClick={() => void pickFace(face.box)}
                        title={`evidence ${face.speaking_evidence} · visible ${(face.coverage * 100).toFixed(0)}%`}
                      >
                        <span>
                          {analysis.chosen_track === face.track_id ? "speaker" : "face"} · r={face.speaking_evidence.toFixed(2)}
                        </span>
                      </button>
                    ))}
                  </div>
                  {analysis.flags.map((item) => (
                    <p key={item.code} className={`dub-flag dub-flag-${item.severity}`}>
                      {item.message}
                    </p>
                  ))}
                </div>
              )}

              {result && (
                <div className="lip-result">
                  <h4>
                    Attempt {result.attempt} · {STATUS[result.status] ?? result.status}
                    {result.status === "generated" ? ` · ${result.review}` : ""}
                  </h4>
                  {result.target_source && <p className="muted">Face: {SOURCE[result.target_source] ?? result.target_source}</p>}
                  {result.stale && <p className="dub-flag dub-flag-warning">Out of date: {result.stale_reason} Regenerate before using it.</p>}
                  {result.flags.map((item) => (
                    <p key={item.code} className={`dub-flag dub-flag-${item.severity}`}>
                      {item.message}
                    </p>
                  ))}
                  {result.status === "generated" && (
                    <>
                      <Compare result={result} />
                      <div className="lip-scores">
                        <Score label="Original EN" score={result.qc.sync_original} />
                        <Score label="Before (dub)" score={result.qc.sync_before} />
                        <Score label="After (dub)" score={result.qc.sync_after} />
                        <div className="lip-score">
                          <span>Face visible</span>
                          <strong>{result.qc.face_coverage != null ? `${Math.round(result.qc.face_coverage * 100)}%` : "—"}</strong>
                          <small>{result.qc.face_height_px ?? "—"} px tall</small>
                        </div>
                        <div className="lip-score">
                          <span>Eyes/forehead kept</span>
                          <strong>{result.qc.upper_face_psnr_db ?? "—"} dB</strong>
                          <small>outside face {result.qc.outside_face_psnr_db ?? "—"} dB</small>
                        </div>
                        <div className="lip-score">
                          <span>Frames</span>
                          <strong>{result.qc.output?.frames ?? "—"}</strong>
                          <small>
                            {result.width}×{result.height} · {result.fps} fps · {result.processing_s}s
                          </small>
                        </div>
                      </div>
                      <p className="muted lip-caveat">
                        Sync confidence comes from Wav2Lip's own lip-sync expert, so it favours Wav2Lip output. Watch the result before accepting it.
                      </p>
                      <div className="timing-form">
                        <label className="lip-note">
                          Review note
                          <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="optional" />
                        </label>
                      </div>
                      <div className="translation-actions">
                        <button
                          type="button"
                          className="button button-small"
                          disabled={busy}
                          onClick={() => void act(async () => { await lipsyncApi.review(result.id, "accepted", note); refresh(); })}
                        >
                          Accept
                        </button>
                        <button
                          type="button"
                          className="button button-quiet button-small"
                          disabled={busy}
                          onClick={() => void act(async () => { await lipsyncApi.review(result.id, "rejected", note); refresh(); })}
                        >
                          Reject
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
