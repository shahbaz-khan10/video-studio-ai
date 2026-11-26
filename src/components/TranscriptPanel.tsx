import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import { api } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { Transcript } from "../types";
import { JobProgress } from "./JobProgress";
import { ErrorState, LoadingState, StatusBadge } from "./States";

interface TranscriptPanelProps {
  videoId: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  previewActive: boolean;
  onJobStarted: (jobId: string) => void;
}

interface DraftSegment {
  id: string;
  start_ms: number;
  end_ms: number;
  text: string;
  speaker: string;
}

const LANGUAGES = [
  { value: "auto", label: "Auto detect" },
  { value: "en", label: "English" },
  { value: "ur", label: "Urdu" },
  { value: "hi", label: "Hindi" },
];

function toDraftSegments(transcript: Transcript | null): DraftSegment[] {
  return (transcript?.segments ?? []).map((segment) => ({
    id: segment.id,
    start_ms: segment.start_ms,
    end_ms: segment.end_ms,
    text: segment.text,
    speaker: segment.speaker ?? "",
  }));
}

function secondsToMs(value: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.round(parsed * 1000);
}

export function TranscriptPanel({ videoId, videoRef, previewActive, onJobStarted }: TranscriptPanelProps) {
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [draft, setDraft] = useState<DraftSegment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  const [language, setLanguage] = useState("auto");
  const [preferDialogueStem, setPreferDialogueStem] = useState(true);
  const [wordTimestamps, setWordTimestamps] = useState(true);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [jobActive, setJobActive] = useState(false);
  const [loadVersion, setLoadVersion] = useState(0);
  const [currentMs, setCurrentMs] = useState(0);

  const applyTranscript = useCallback((next: Transcript) => {
    setTranscript(next);
    setDraft(toDraftSegments(next));
  }, []);

  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(toDraftSegments(transcript)),
    [draft, transcript],
  );
  const dirtyRef = useRef(dirty);
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const reload = useCallback((force = false) => {
    if (!force && dirtyRef.current && !window.confirm("Discard unsaved transcript edits and reload?")) return;
    setLoadVersion((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api
      .getTranscript(videoId)
      .then((result) => {
        if (active) applyTranscript(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not load the transcript.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [videoId, loadVersion, applyTranscript]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const update = () => setCurrentMs(Math.round((video.currentTime || 0) * 1000));
    update();
    video.addEventListener("timeupdate", update);
    video.addEventListener("seeked", update);
    return () => {
      video.removeEventListener("timeupdate", update);
      video.removeEventListener("seeked", update);
    };
  }, [videoRef, videoId]);

  const activeIds = useMemo(() => {
    const ids = new Set<string>();
    draft.forEach((segment) => {
      if (currentMs >= segment.start_ms && currentMs <= segment.end_ms) ids.add(segment.id);
    });
    return ids;
  }, [draft, currentMs]);

  const seekTo = useCallback(
    (ms: number, play: boolean) => {
      const video = videoRef.current;
      if (!video) return;
      const safe = Math.max(0, ms);
      video.currentTime = safe / 1000;
      setCurrentMs(safe);
      if (play && !previewActive) {
        void video.play().catch(() => undefined);
      }
    },
    [videoRef, previewActive],
  );

  const updateSegment = (id: string, patch: Partial<DraftSegment>) => {
    setDraft((current) => current.map((segment) => (segment.id === id ? { ...segment, ...patch } : segment)));
  };

  const addSegment = () => {
    const lastEnd = draft.reduce((max, segment) => Math.max(max, segment.end_ms), 0);
    setDraft((current) => [
      ...current,
      {
        id: `new-${Date.now()}-${current.length}`,
        start_ms: lastEnd,
        end_ms: lastEnd + 2000,
        text: "",
        speaker: "",
      },
    ]);
  };

  const startTranscription = async () => {
    if (starting || jobActive) return;
    if (dirty && !window.confirm("Discard unsaved transcript edits and run speech recognition again?")) return;
    setStarting(true);
    setSaveError(null);
    try {
      const job = await api.startTranscription(videoId, {
        language,
        prefer_dialogue_stem: preferDialogueStem,
        word_timestamps: wordTimestamps,
      });
      setActiveJobId(job.id);
      setJobActive(true);
      onJobStarted(job.id);
    } catch (reason) {
      setSaveError(reason instanceof Error ? reason.message : "Could not start transcription.");
    } finally {
      setStarting(false);
    }
  };

  const handleJobSettled = useCallback(() => {
    setJobActive(false);
    reload(true);
  }, [reload]);

  const save = async () => {
    if (!transcript || saving) return;
    const invalid = draft.find((segment) => segment.end_ms <= segment.start_ms);
    if (invalid) {
      setSaveError("A segment must end after it starts. Correct the timing and save again.");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const result = await api.saveTranscript(videoId, {
        revision: transcript.revision,
        segments: draft.map((segment) => ({
          id: segment.id.startsWith("new-") ? null : segment.id,
          start_ms: segment.start_ms,
          end_ms: segment.end_ms,
          text: segment.text,
          speaker: segment.speaker || null,
        })),
      });
      applyTranscript(result);
    } catch (reason) {
      setSaveError(reason instanceof Error ? reason.message : "Could not save the transcript.");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <LoadingState label="Loading transcript…" />;
  if (error) return <ErrorState message={error} onRetry={reload} />;

  const meta = transcript?.metadata_json ?? {};
  const serverSegments = transcript?.segments ?? [];
  const wordCount = serverSegments.reduce((total, segment) => total + segment.words.length, 0);

  return (
    <div className="transcript-panel">
      <div className="transcript-controls">
        <label>
          <span>Language</span>
          <select value={language} onChange={(event) => setLanguage(event.target.value)} aria-label="Transcription language">
            {LANGUAGES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="transcript-check">
          <input
            type="checkbox"
            checked={preferDialogueStem}
            onChange={(event) => setPreferDialogueStem(event.target.checked)}
          />
          Use dialogue stem
        </label>
        <label className="transcript-check">
          <input
            type="checkbox"
            checked={wordTimestamps}
            onChange={(event) => setWordTimestamps(event.target.checked)}
          />
          Word timings
        </label>
        <button
          className="button button-primary"
          type="button"
          onClick={startTranscription}
          disabled={starting || jobActive}
          aria-label="Start transcription"
        >
          {starting ? "Starting…" : "Transcribe video"}
        </button>
      </div>

      {activeJobId && (
        <JobProgress
          jobId={activeJobId}
          onSettled={handleJobSettled}
          onJobChange={(jobId) => {
            setActiveJobId(jobId);
            setJobActive(true);
          }}
        />
      )}

      {transcript && transcript.status !== "idle" ? (
        <>
          <div className="transcript-meta">
            <StatusBadge value={transcript.status} />
            <span>v{transcript.version} · rev {transcript.revision}</span>
            <span>{transcript.language.toUpperCase()}</span>
            <span>{transcript.source ?? "unknown source"}</span>
            <span>{transcript.backend ?? "unknown engine"}</span>
            <span>{transcript.model ?? "unknown model"}</span>
            <span>{serverSegments.length} segments</span>
            {wordCount > 0 && <span>{wordCount} words</span>}
          </div>
          <div className="transcript-playhead">
            <span>Playhead {formatTimestamp(currentMs)}</span>
            <span>{activeIds.size} active segment{activeIds.size === 1 ? "" : "s"}</span>
          </div>
        </>
      ) : (
        <p className="transcript-empty">
          No transcript yet. Run speech recognition to create timestamped dialogue from this video.
        </p>
      )}

      {draft.length > 0 && (
        <div className="transcript-segments">
          {draft.map((segment, index) => {
            const server = serverSegments.find((item) => item.id === segment.id);
            const active = activeIds.has(segment.id);
            return (
              <article
                className={`transcript-segment${active ? " is-active" : ""}`}
                key={segment.id}
                onClick={() => seekTo(segment.start_ms, false)}
              >
                <div className="transcript-segment-head">
                  <span className="segment-index">{index + 1}</span>
                  <strong className="segment-time">
                    {formatTimestamp(segment.start_ms)} → {formatTimestamp(segment.end_ms)}
                  </strong>
                  <div className="segment-badges">
                    {active && <span className="badge badge-running">playing</span>}
                    {server?.edited && <span className="badge badge-draft">edited</span>}
                    {server?.words_stale && <span className="badge badge-draft">words stale</span>}
                    {server?.confidence !== null && server?.confidence !== undefined && (
                      <span className="segment-confidence">{Math.round(server.confidence * 100)}% conf.</span>
                    )}
                    {server?.language && <span className="segment-confidence">{server.language}</span>}
                  </div>
                  <button
                    className="button button-quiet"
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation();
                      seekTo(segment.start_ms, true);
                    }}
                    aria-label={`Play from segment ${index + 1}`}
                  >
                    Play from here
                  </button>
                </div>
                <textarea
                  value={segment.text}
                  rows={2}
                  aria-label={`Dialogue text ${index + 1}`}
                  onClick={(event) => event.stopPropagation()}
                  onChange={(event) => updateSegment(segment.id, { text: event.target.value })}
                  placeholder="Dialogue text"
                />
                <div className="segment-edit-grid">
                  <label>
                    <span>Start (s)</span>
                    <input
                      type="number"
                      step="0.01"
                      min="0"
                      value={(segment.start_ms / 1000).toFixed(2)}
                      onClick={(event) => event.stopPropagation()}
                      onChange={(event) => updateSegment(segment.id, { start_ms: secondsToMs(event.target.value) })}
                      aria-label={`Segment ${index + 1} start time`}
                    />
                  </label>
                  <label>
                    <span>End (s)</span>
                    <input
                      type="number"
                      step="0.01"
                      min="0"
                      value={(segment.end_ms / 1000).toFixed(2)}
                      onClick={(event) => event.stopPropagation()}
                      onChange={(event) => updateSegment(segment.id, { end_ms: secondsToMs(event.target.value) })}
                      aria-label={`Segment ${index + 1} end time`}
                    />
                  </label>
                  <label>
                    <span>Speaker</span>
                    <input
                      type="text"
                      value={segment.speaker}
                      onClick={(event) => event.stopPropagation()}
                      onChange={(event) => updateSegment(segment.id, { speaker: event.target.value })}
                      aria-label={`Segment ${index + 1} speaker`}
                    />
                  </label>
                  <button
                    className="button button-danger"
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation();
                      setDraft((current) => current.filter((item) => item.id !== segment.id));
                    }}
                    aria-label={`Delete segment ${index + 1}`}
                  >
                    Delete
                  </button>
                </div>
                {server && !server.words_stale && server.words.length > 0 && (
                  <details className="segment-words" onClick={(event) => event.stopPropagation()}>
                    <summary>Word timings ({server.words.length})</summary>
                    <div className="word-chips">
                      {server.words.map((word, wordIndex) => (
                        <button
                          className="word-chip"
                          type="button"
                          key={`${wordIndex}-${word.start_ms}`}
                          onClick={() => seekTo(word.start_ms, false)}
                          title={`${formatTimestamp(word.start_ms)} – ${formatTimestamp(word.end_ms)}`}
                        >
                          {word.text}
                        </button>
                      ))}
                    </div>
                  </details>
                )}
              </article>
            );
          })}
        </div>
      )}

      <div className="transcript-actions">
        <button className="button button-quiet" type="button" onClick={addSegment} aria-label="Add transcript segment">
          Add segment
        </button>
        <button
          className="button button-primary"
          type="button"
          onClick={save}
          disabled={!transcript || transcript.status === "idle" || saving || !dirty}
          aria-label="Save transcript"
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
        <button
          className="button button-quiet"
          type="button"
          onClick={() => applyTranscript(transcript!)}
          disabled={!transcript || !dirty}
          aria-label="Revert transcript changes"
        >
          Revert
        </button>
      </div>

      {saveError && <p className="form-error" role="alert">{saveError}</p>}

      {transcript && (transcript.limitations.length > 0 || transcript.notes.length > 0) && (
        <div className="audio-limitations transcript-limitations">
          <strong>Recognition notes</strong>
          <ul>
            {[...transcript.limitations, ...transcript.notes].map((note, index) => (
              <li key={`${index}-${note}`}>{note}</li>
            ))}
          </ul>
        </div>
      )}
      {typeof meta.last_edited_at === "string" && (
        <small className="draft-note">Last edited {meta.last_edited_at}</small>
      )}
    </div>
  );
}
