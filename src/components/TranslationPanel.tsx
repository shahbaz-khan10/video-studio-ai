import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import { ApiError, api } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type {
  QcFlag,
  SegmentTranslationItem,
  TargetLanguage,
  TranslationBundle,
  TranslationLine,
} from "../types";
import { JobProgress } from "./JobProgress";
import { TranslationContextEditor } from "./TranslationContextEditor";
import { ErrorState, LoadingState } from "./States";

interface TranslationPanelProps {
  projectId: string;
  videoId: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  previewActive: boolean;
  onJobStarted: (jobId: string) => void;
}

type Field = "ur" | "hi" | "roman";
type Filter = "all" | "review" | "stale" | "manual" | "missing" | "long";
interface CellEdit {
  key: string;
  before: string;
  after: string;
  at: number;
}

const PAGE_SIZE = 40;
const LANG_LABEL: Record<TargetLanguage, string> = { ur: "Urdu", hi: "Hindi" };
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All lines" },
  { value: "review", label: "Needs review" },
  { value: "stale", label: "English changed" },
  { value: "manual", label: "Manually edited" },
  { value: "missing", label: "Not translated" },
  { value: "long", label: "Too long for timing" },
];

const cellKey = (segmentId: string, field: Field) => `${segmentId}|${field}`;

function serverValue(line: TranslationLine, field: Field): string {
  if (field === "roman") return line.translations.ur?.roman_text ?? "";
  return line.translations[field]?.text ?? "";
}

function worstSeverity(flags: QcFlag[]): QcFlag["severity"] | null {
  if (flags.some((flag) => flag.severity === "error")) return "error";
  if (flags.some((flag) => flag.severity === "warning")) return "warning";
  if (flags.length) return "info";
  return null;
}

function needsReview(item: SegmentTranslationItem | null): boolean {
  return !!item && (item.stale || item.qc_flags.some((flag) => flag.severity !== "info"));
}

function messageOf(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

export function TranslationPanel({ projectId, videoId, videoRef, previewActive, onJobStarted }: TranslationPanelProps) {
  const [bundle, setBundle] = useState<TranslationBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [undoStack, setUndoStack] = useState<CellEdit[]>([]);
  const [redoStack, setRedoStack] = useState<CellEdit[]>([]);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [languages, setLanguages] = useState<TargetLanguage[]>(["ur", "hi"]);
  const [onlyMissing, setOnlyMissing] = useState(false);
  const [overwriteManual, setOverwriteManual] = useState(false);
  const [showRoman, setShowRoman] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [compare, setCompare] = useState<{ segmentId: string; language: TargetLanguage } | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [jobActive, setJobActive] = useState(false);
  const [loadVersion, setLoadVersion] = useState(0);
  const [savingAll, setSavingAll] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  const dirtyKeys = useMemo(() => Object.keys(drafts), [drafts]);
  const dirtyRef = useRef(0);
  useEffect(() => {
    dirtyRef.current = dirtyKeys.length;
  }, [dirtyKeys]);
  useEffect(() => {
    if (!dirtyKeys.length) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirtyKeys.length]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api
      .getTranslations(videoId)
      .then((result) => {
        if (!active) return;
        setBundle(result);
        const job = result.job;
        if (job && (job.status === "queued" || job.status === "running")) {
          setActiveJobId(job.id);
          setJobActive(true);
        } else if (job && job.status === "failed") {
          setActiveJobId(job.id); // keep the failure and its Retry button visible
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(messageOf(reason, "Could not load translations."));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId, loadVersion]);

  const reload = useCallback((force = false) => {
    if (!force && dirtyRef.current && !window.confirm("Discard unsaved translation edits and reload?")) return;
    setDrafts({});
    setLoadVersion((value) => value + 1);
  }, []);

  // Refresh server data but keep unsaved drafts (they still override the cells).
  const refresh = useCallback(() => setLoadVersion((value) => value + 1), []);

  const lines = bundle?.lines ?? [];
  const lineById = useMemo(() => new Map(lines.map((line) => [line.segment_id, line])), [lines]);

  const replaceLine = useCallback((next: TranslationLine) => {
    setBundle((current) =>
      current
        ? { ...current, lines: current.lines.map((line) => (line.segment_id === next.segment_id ? next : line)) }
        : current,
    );
  }, []);

  const valueOf = useCallback(
    (line: TranslationLine, field: Field) => drafts[cellKey(line.segment_id, field)] ?? serverValue(line, field),
    [drafts],
  );

  const applyValue = useCallback(
    (key: string, value: string) => {
      const [segmentId, field] = key.split("|") as [string, Field];
      const line = lineById.get(segmentId);
      setDrafts((current) => {
        const next = { ...current };
        if (line && serverValue(line, field) === value) delete next[key];
        else next[key] = value;
        return next;
      });
    },
    [lineById],
  );

  const editCell = useCallback(
    (line: TranslationLine, field: Field, value: string) => {
      const key = cellKey(line.segment_id, field);
      const before = drafts[key] ?? serverValue(line, field);
      if (before === value) return;
      const now = Date.now();
      setUndoStack((stack) => {
        const last = stack[stack.length - 1];
        if (last && last.key === key && now - last.at < 1200) {
          return [...stack.slice(0, -1), { ...last, after: value, at: now }];
        }
        return [...stack.slice(-199), { key, before, after: value, at: now }];
      });
      setRedoStack([]);
      applyValue(key, value);
    },
    [drafts, applyValue],
  );

  const undo = useCallback(() => {
    const last = undoStack[undoStack.length - 1];
    if (!last) return;
    applyValue(last.key, last.before);
    setUndoStack(undoStack.slice(0, -1));
    setRedoStack([...redoStack, last]);
  }, [undoStack, redoStack, applyValue]);

  const redo = useCallback(() => {
    const last = redoStack[redoStack.length - 1];
    if (!last) return;
    applyValue(last.key, last.after);
    setRedoStack(redoStack.slice(0, -1));
    setUndoStack([...undoStack, last]);
  }, [undoStack, redoStack, applyValue]);

  const saveCell = useCallback(
    async (segmentId: string, language: TargetLanguage) => {
      const line = lineById.get(segmentId);
      if (!line) return;
      const textKey = cellKey(segmentId, language);
      const romanKey = cellKey(segmentId, "roman");
      const payload: { text?: string; roman_text?: string; base_revision?: number } = {};
      if (textKey in drafts) payload.text = drafts[textKey];
      if (language === "ur" && romanKey in drafts) payload.roman_text = drafts[romanKey];
      if (payload.text === undefined && payload.roman_text === undefined) return;
      const current = line.translations[language];
      if (current) payload.base_revision = current.revision;
      setBusy((value) => ({ ...value, [`${segmentId}|save|${language}`]: true }));
      setActionError(null);
      try {
        const next = await api.saveTranslation(segmentId, language, payload);
        replaceLine(next);
        setDrafts((value) => {
          const copy = { ...value };
          delete copy[textKey];
          if (language === "ur") delete copy[romanKey];
          return copy;
        });
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 409) {
          setActionError(`${reason.message} Your text is still in the editor; reload to see the other change.`);
        } else {
          setActionError(messageOf(reason, "Could not save the translation."));
        }
      } finally {
        setBusy((value) => ({ ...value, [`${segmentId}|save|${language}`]: false }));
      }
    },
    [drafts, lineById, replaceLine],
  );

  const saveAll = useCallback(async () => {
    if (savingAll) return;
    setSavingAll(true);
    const targets = new Set<string>();
    Object.keys(drafts).forEach((key) => {
      const [segmentId, field] = key.split("|");
      targets.add(`${segmentId}|${field === "roman" ? "ur" : field}`);
    });
    for (const target of targets) {
      const [segmentId, language] = target.split("|") as [string, TargetLanguage];
      // eslint-disable-next-line no-await-in-loop
      await saveCell(segmentId, language);
    }
    setSavingAll(false);
  }, [drafts, saveCell, savingAll]);

  useEffect(() => {
    const node = panelRef.current;
    if (!node) return;
    const onKey = (event: KeyboardEvent) => {
      const mod = event.ctrlKey || event.metaKey;
      if (!mod) return;
      const key = event.key.toLowerCase();
      if (key === "s") {
        event.preventDefault();
        void saveAll();
        return;
      }
      const target = event.target as HTMLElement;
      const typing = target.tagName === "TEXTAREA" || target.tagName === "INPUT";
      // Inside a text box the browser's own per-field undo wins; Alt+Z/Y always use the editor history.
      if (typing && !event.altKey) return;
      if (key === "z" && !event.shiftKey) {
        event.preventDefault();
        undo();
      } else if (key === "y" || (key === "z" && event.shiftKey)) {
        event.preventDefault();
        redo();
      }
    };
    node.addEventListener("keydown", onKey);
    return () => node.removeEventListener("keydown", onKey);
  }, [saveAll, undo, redo]);

  const translateLine = async (line: TranslationLine, langs: TargetLanguage[], force: boolean) => {
    const manual = langs.filter((lang) => line.translations[lang]?.origin === "manual");
    const unsaved = langs.filter((lang) => cellKey(line.segment_id, lang) in drafts);
    if (unsaved.length && !window.confirm("This line has unsaved edits that will be discarded. Continue?")) return;
    if (force && manual.length) {
      const ok = window.confirm(
        `Replace your manual ${manual.map((lang) => LANG_LABEL[lang]).join(" and ")} edit with a new AI translation? Your version stays in the history.`,
      );
      if (!ok) return;
    }
    const busyKey = `${line.segment_id}|translate|${langs.join(",")}`;
    setBusy((value) => ({ ...value, [busyKey]: true }));
    setActionError(null);
    setNotice(null);
    try {
      const next = await api.translateSegment(videoId, line.segment_id, langs, force);
      replaceLine(next);
      setDrafts((value) => {
        const copy = { ...value };
        langs.forEach((lang) => delete copy[cellKey(line.segment_id, lang)]);
        if (langs.includes("ur")) delete copy[cellKey(line.segment_id, "roman")];
        return copy;
      });
      if (!force && manual.length) {
        setNotice("Your manual edit was kept. A fresh AI suggestion is available under Compare.");
      }
    } catch (reason) {
      setActionError(messageOf(reason, "Translation failed."));
    } finally {
      setBusy((value) => ({ ...value, [busyKey]: false }));
    }
  };

  const startTranslateAll = async () => {
    if (jobActive || !languages.length) return;
    if (overwriteManual && !window.confirm("Overwrite ALL manual edits with fresh AI translations? They stay in each line's history.")) {
      return;
    }
    if (
      dirtyKeys.length &&
      !window.confirm("You have unsaved edits. They stay in the editor and are not affected by the AI pass. Start translating?")
    ) {
      return;
    }
    setActionError(null);
    try {
      const job = await api.translateAll(videoId, {
        languages,
        only_missing: onlyMissing,
        overwrite_manual: overwriteManual,
      });
      setActiveJobId(job.id);
      setJobActive(true);
      onJobStarted(job.id);
    } catch (reason) {
      setActionError(messageOf(reason, "Could not start translation."));
    }
  };

  const restore = async (segmentId: string, language: TargetLanguage, target: "ai" | "history", index?: number) => {
    setActionError(null);
    try {
      const next = await api.restoreTranslation(segmentId, language, target, index);
      replaceLine(next);
      setDrafts((value) => {
        const copy = { ...value };
        delete copy[cellKey(segmentId, language)];
        if (language === "ur") delete copy[cellKey(segmentId, "roman")];
        return copy;
      });
    } catch (reason) {
      setActionError(messageOf(reason, "Could not restore that version."));
    }
  };

  const regenerateRoman = async (segmentId: string) => {
    if (cellKey(segmentId, "ur") in drafts) {
      setActionError("Save the Urdu text first, then regenerate Roman Urdu from it.");
      return;
    }
    try {
      replaceLine(await api.romanizeTranslation(segmentId));
      setDrafts((value) => {
        const copy = { ...value };
        delete copy[cellKey(segmentId, "roman")];
        return copy;
      });
    } catch (reason) {
      setActionError(messageOf(reason, "Could not regenerate Roman Urdu."));
    }
  };

  const seekTo = (ms: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = Math.max(0, ms) / 1000;
    if (!previewActive) void video.play().catch(() => undefined);
  };

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    return lines.filter((line) => {
      const items = languages.map((lang) => line.translations[lang]);
      if (filter === "review" && !items.some(needsReview)) return false;
      if (filter === "stale" && !items.some((item) => item?.stale)) return false;
      if (filter === "manual" && !items.some((item) => item?.origin === "manual")) return false;
      if (filter === "missing" && !items.some((item) => !item || !item.text.trim())) return false;
      if (filter === "long" && !items.some((item) => item?.fit?.status === "long")) return false;
      if (!query) return true;
      const haystack = [
        line.text,
        line.character_name ?? "",
        line.speaker_label ?? "",
        line.translations.ur?.text ?? "",
        line.translations.ur?.roman_text ?? "",
        line.translations.hi?.text ?? "",
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [lines, filter, search, languages]);

  useEffect(() => setPage(0), [filter, search]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visible = filtered.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);

  if (loading && !bundle) return <LoadingState label="Loading translations…" />;
  if (error && !bundle) return <ErrorState message={error} onRetry={() => reload(true)} />;
  if (!bundle) return null;

  const engine = bundle.engine;
  const compareLine = compare ? lineById.get(compare.segmentId) ?? null : null;
  const compareItem = compareLine && compare ? compareLine.translations[compare.language] : null;

  return (
    <div className="translation-panel" ref={panelRef}>
      <div className="translation-engine">
        <div>
          <strong>{engine.label}</strong>
          <small>
            {engine.model} · {engine.license}
            {engine.commercial_use === false ? " · non-commercial licence" : ""} · {engine.device}
            {engine.cached ? " · downloaded" : ` · first run downloads ~${engine.requirements.download_gb} GB`}
          </small>
        </div>
        <div className="translation-engine-meta">
          <span className="chip">~{engine.requirements.ram_cpu_gb} GB RAM</span>
          {engine.refiner_configured ? (
            <span className="chip">Context LLM · {engine.refiner_model}</span>
          ) : (
            <span className="chip chip-planned" title="Set TRANSLATION_REFINER_URL to a self-hosted open LLM">
              Context LLM off
            </span>
          )}
        </div>
      </div>

      <div className="translation-toolbar">
        <fieldset className="translation-langs">
          <legend>Targets</legend>
          {(["ur", "hi"] as TargetLanguage[]).map((lang) => (
            <label key={lang} className="transcript-check">
              <input
                type="checkbox"
                checked={languages.includes(lang)}
                onChange={(event) =>
                  setLanguages((current) =>
                    event.target.checked ? [...current, lang].sort() as TargetLanguage[] : current.filter((item) => item !== lang),
                  )
                }
              />
              {LANG_LABEL[lang]}
            </label>
          ))}
        </fieldset>
        <label className="transcript-check">
          <input type="checkbox" checked={onlyMissing} onChange={(event) => setOnlyMissing(event.target.checked)} />
          Only missing or changed lines
        </label>
        <label className="transcript-check">
          <input type="checkbox" checked={overwriteManual} onChange={(event) => setOverwriteManual(event.target.checked)} />
          Overwrite manual edits
        </label>
        <button
          type="button"
          className="button button-primary"
          onClick={() => void startTranslateAll()}
          disabled={jobActive || !languages.length || !lines.length}
        >
          {jobActive ? "Translating…" : "Translate all"}
        </button>
        <div className="translation-toolbar-spacer" />
        <button type="button" className="button button-quiet" onClick={undo} disabled={!undoStack.length} title="Undo (Ctrl+Alt+Z)">
          Undo
        </button>
        <button type="button" className="button button-quiet" onClick={redo} disabled={!redoStack.length} title="Redo (Ctrl+Alt+Y)">
          Redo
        </button>
        <button
          type="button"
          className="button"
          onClick={() => void saveAll()}
          disabled={!dirtyKeys.length || savingAll}
          title="Save all edits (Ctrl+S)"
        >
          {savingAll ? "Saving…" : `Save edits${dirtyKeys.length ? ` (${dirtyKeys.length})` : ""}`}
        </button>
        <button type="button" className="button button-quiet" onClick={() => reload()}>
          Reload
        </button>
      </div>

      {activeJobId && (
        <div className="translation-job">
          <JobProgress
            jobId={activeJobId}
            onSettled={() => {
              setJobActive(false);
              refresh();
            }}
            onJobChange={(jobId) => {
              setActiveJobId(jobId);
              setJobActive(true);
            }}
          />
        </div>
      )}

      {(["ur", "hi"] as TargetLanguage[]).some((lang) => bundle.summary[lang]) && (
        <div className="translation-summary">
          {(["ur", "hi"] as TargetLanguage[]).map((lang) => {
            const summary = bundle.summary[lang];
            if (!summary) return null;
            return (
              <span key={lang}>
                <strong>{LANG_LABEL[lang]}</strong> {summary.translated}/{summary.total} translated · {summary.manual} edited ·{" "}
                <span className={summary.flagged ? "text-warning" : ""}>{summary.flagged} to review</span>
                {summary.stale ? <span className="text-warning"> · {summary.stale} English changed</span> : null}
                {summary.timing_long ? <span className="text-warning"> · {summary.timing_long} too long</span> : null}
              </span>
            );
          })}
        </div>
      )}

      {notice && <p className="translation-notice">{notice}</p>}
      {actionError && (
        <p className="form-error" role="alert">
          {actionError}
        </p>
      )}

      <TranslationContextEditor
        projectId={projectId}
        profiles={bundle.profiles}
        relationships={bundle.relationships}
        glossary={bundle.glossary}
        onChanged={(next) => setBundle((current) => (current ? { ...current, ...next } : current))}
      />

      {!lines.length ? (
        <div className="transcript-empty">
          <strong>No dialogue to translate yet</strong>
          <p>Transcribe the video first (Transcript panel). Run character detection to make translations character-aware.</p>
        </div>
      ) : (
        <>
          <div className="translation-filters">
            <select value={filter} onChange={(event) => setFilter(event.target.value as Filter)} aria-label="Filter lines">
              {FILTERS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
            <input
              type="search"
              placeholder="Search English, Urdu, Roman Urdu, Hindi or speaker…"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <label className="transcript-check">
              <input type="checkbox" checked={showRoman} onChange={(event) => setShowRoman(event.target.checked)} />
              Roman Urdu
            </label>
            <span className="translation-count">
              {filtered.length} of {lines.length} lines
            </span>
          </div>

          <div className="translation-head" aria-hidden="true">
            <span>English · source (read-only)</span>
            {languages.includes("ur") && <span>Urdu · اردو</span>}
            {languages.includes("hi") && <span>Hindi · हिंदी</span>}
          </div>

          <ol className="translation-lines">
            {visible.map((line) => (
              <TranslationRow
                key={line.segment_id}
                line={line}
                languages={languages}
                showRoman={showRoman}
                valueOf={valueOf}
                dirty={(field) => cellKey(line.segment_id, field) in drafts}
                busy={busy}
                sceneNote={bundle.scene_notes[line.segment_id] ?? ""}
                onEdit={editCell}
                onSave={(lang) => void saveCell(line.segment_id, lang)}
                onTranslate={(langs, force) => void translateLine(line, langs, force)}
                onCompare={(lang) => setCompare({ segmentId: line.segment_id, language: lang })}
                onRoman={() => void regenerateRoman(line.segment_id)}
                onSeek={() => seekTo(line.start_ms)}
                onSceneNote={async (note) => {
                  try {
                    await api.saveSceneNote(videoId, line.segment_id, note);
                    setBundle((current) =>
                      current
                        ? { ...current, scene_notes: { ...current.scene_notes, [line.segment_id]: note } }
                        : current,
                    );
                  } catch (reason) {
                    setActionError(messageOf(reason, "Could not save the scene note."));
                  }
                }}
              />
            ))}
          </ol>

          {pageCount > 1 && (
            <div className="translation-pager">
              <button type="button" className="button button-quiet" disabled={page === 0} onClick={() => setPage(page - 1)}>
                Previous
              </button>
              <span>
                Page {page + 1} of {pageCount}
              </span>
              <button
                type="button"
                className="button button-quiet"
                disabled={page >= pageCount - 1}
                onClick={() => setPage(page + 1)}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}

      {compare && compareLine && (
        <CompareDrawer
          line={compareLine}
          language={compare.language}
          item={compareItem}
          onClose={() => setCompare(null)}
          onUseAi={() => void restore(compare.segmentId, compare.language, "ai")}
          onRestore={(index) => void restore(compare.segmentId, compare.language, "history", index)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

interface RowProps {
  line: TranslationLine;
  languages: TargetLanguage[];
  showRoman: boolean;
  valueOf: (line: TranslationLine, field: Field) => string;
  dirty: (field: Field) => boolean;
  busy: Record<string, boolean>;
  sceneNote: string;
  onEdit: (line: TranslationLine, field: Field, value: string) => void;
  onSave: (lang: TargetLanguage) => void;
  onTranslate: (langs: TargetLanguage[], force: boolean) => void;
  onCompare: (lang: TargetLanguage) => void;
  onRoman: () => void;
  onSeek: () => void;
  onSceneNote: (note: string) => void;
}

function OriginBadge({ item, dirty }: { item: SegmentTranslationItem | null; dirty: boolean }) {
  if (dirty) return <span className="badge badge-running">unsaved</span>;
  if (!item || !item.text.trim()) return <span className="badge badge-draft">empty</span>;
  if (item.stale) return <span className="badge badge-failed">English changed</span>;
  if (item.origin === "manual") return <span className="badge badge-edited">edited</span>;
  if (item.origin === "memory") return <span className="badge badge-completed">from your edit</span>;
  return <span className="badge badge-ai">AI · {item.engine ?? "model"}</span>;
}

function FitChip({ item }: { item: SegmentTranslationItem | null }) {
  const fit = item?.fit;
  if (!fit?.status || fit.status === "empty") return null;
  return (
    <span className={`fit-chip fit-${fit.status}`} title={`Estimated ${fit.estimated_s}s of speech for a ${fit.duration_s}s line`}>
      {fit.status === "ok" ? "fits" : fit.status === "tight" ? "tight" : "too long"} · {fit.estimated_s}s/{fit.duration_s}s
    </span>
  );
}

function TranslationRow({
  line,
  languages,
  showRoman,
  valueOf,
  dirty,
  busy,
  sceneNote,
  onEdit,
  onSave,
  onTranslate,
  onCompare,
  onRoman,
  onSeek,
  onSceneNote,
}: RowProps) {
  const [note, setNote] = useState(sceneNote);
  useEffect(() => setNote(sceneNote), [sceneNote]);
  const translating = busy[`${line.segment_id}|translate|${languages.join(",")}`];
  const speaker = line.character_name ?? line.speaker_label ?? "Unassigned";

  return (
    <li className="translation-row">
      {line.scene_start && (
        <div className="translation-scene">
          <span>Scene {line.scene_index + 1}</span>
          <input
            value={note}
            placeholder="Scene context (place, mood, who is present)… used for translation"
            onChange={(event) => setNote(event.target.value)}
            onBlur={() => note !== sceneNote && onSceneNote(note)}
          />
        </div>
      )}
      <div className="translation-row-grid" style={{ gridTemplateColumns: `repeat(${1 + languages.length}, minmax(0, 1fr))` }}>
        <div className="translation-source">
          <div className="translation-meta">
            <button type="button" className="timestamp-button" onClick={onSeek} title="Play from here">
              {formatTimestamp(line.start_ms)}
            </button>
            <strong className={line.character_id ? "" : "muted"}>{speaker}</strong>
          </div>
          <p lang="en">{line.text}</p>
          <div className="translation-actions">
            <button
              type="button"
              className="button button-quiet button-small"
              onClick={() => onTranslate(languages, false)}
              disabled={translating || !languages.length}
            >
              {translating ? "Translating…" : "Translate line"}
            </button>
          </div>
        </div>
        {languages.map((lang) => {
          const item = line.translations[lang];
          const isDirty = dirty(lang) || (lang === "ur" && dirty("roman"));
          const severity = item ? worstSeverity(item.qc_flags) : null;
          const saving = busy[`${line.segment_id}|save|${lang}`];
          const regenerating = busy[`${line.segment_id}|translate|${lang}`];
          return (
            <div key={lang} className={`translation-cell${severity ? ` has-${severity}` : ""}`}>
              <div className="translation-meta">
                <OriginBadge item={item} dirty={isDirty} />
                <FitChip item={item} />
                {item?.suggestion_available && <span className="badge badge-ai">new AI suggestion</span>}
              </div>
              <textarea
                lang={lang}
                dir={lang === "ur" ? "rtl" : "ltr"}
                className={lang === "ur" ? "urdu-text" : "hindi-text"}
                value={valueOf(line, lang)}
                rows={2}
                placeholder={lang === "ur" ? "اردو ترجمہ…" : "हिंदी अनुवाद…"}
                onChange={(event) => onEdit(line, lang, event.target.value)}
                aria-label={`${LANG_LABEL[lang]} translation for line ${line.position + 1}`}
              />
              {lang === "ur" && showRoman && (
                <div className="roman-field">
                  <textarea
                    lang="ur-Latn"
                    value={valueOf(line, "roman")}
                    rows={2}
                    placeholder="Roman Urdu…"
                    onChange={(event) => onEdit(line, "roman", event.target.value)}
                    aria-label={`Roman Urdu for line ${line.position + 1}`}
                  />
                  <small>
                    {item?.roman_origin === "manual" ? "Roman · edited" : item?.roman_origin === "ai" ? "Roman · AI" : "Roman · auto"}
                    <button type="button" className="link-button" onClick={onRoman} disabled={!item?.text}>
                      regenerate
                    </button>
                  </small>
                </div>
              )}
              {item && item.qc_flags.length > 0 && (
                <ul className="qc-flags">
                  {item.qc_flags.map((flag) => (
                    <li key={flag.code} className={`qc-${flag.severity}`} title={flag.code}>
                      {flag.message}
                    </li>
                  ))}
                </ul>
              )}
              <div className="translation-actions">
                <button
                  type="button"
                  className="button button-small"
                  onClick={() => onSave(lang)}
                  disabled={!isDirty || saving}
                >
                  {saving ? "Saving…" : "Save"}
                </button>
                <button
                  type="button"
                  className="button button-quiet button-small"
                  onClick={() => onTranslate([lang], true)}
                  disabled={regenerating}
                  title="Generate a new AI translation for this language"
                >
                  {regenerating ? "…" : item ? "Regenerate" : "Translate"}
                </button>
                <button
                  type="button"
                  className="button button-quiet button-small"
                  onClick={() => onCompare(lang)}
                  disabled={!item}
                >
                  Compare
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------

function CompareDrawer({
  line,
  language,
  item,
  onClose,
  onUseAi,
  onRestore,
}: {
  line: TranslationLine;
  language: TargetLanguage;
  item: SegmentTranslationItem | null;
  onClose: () => void;
  onUseAi: () => void;
  onRestore: (index: number) => void;
}) {
  const dir = language === "ur" ? "rtl" : "ltr";
  const ctx = item?.context ?? {};
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="compare-backdrop" role="dialog" aria-modal="true" aria-label="Compare translation" onClick={onClose}>
      <div className="compare-drawer" onClick={(event) => event.stopPropagation()}>
        <div className="panel-heading">
          <div>
            <h2>Compare · {LANG_LABEL[language]}</h2>
            <p>
              {formatTimestamp(line.start_ms)} · {line.character_name ?? line.speaker_label ?? "Unassigned"}
            </p>
          </div>
          <button type="button" className="button button-quiet" onClick={onClose}>
            Close
          </button>
        </div>
        <dl className="compare-grid">
          <dt>English (now)</dt>
          <dd lang="en">{line.text}</dd>
          {item?.stale && (
            <>
              <dt>English when translated</dt>
              <dd lang="en">{item.source_text}</dd>
            </>
          )}
          <dt>Current {item?.origin === "manual" ? "(your edit)" : "(AI)"}</dt>
          <dd dir={dir} lang={language} className={language === "ur" ? "urdu-text" : ""}>
            {item?.text || "—"}
            {language === "ur" && item?.roman_text && <small className="compare-roman">{item.roman_text}</small>}
          </dd>
          <dt>Latest AI suggestion</dt>
          <dd dir={dir} lang={language} className={language === "ur" ? "urdu-text" : ""}>
            {item?.ai_text || "—"}
          </dd>
          <dt>Raw model output</dt>
          <dd dir={dir} lang={language}>
            {item?.raw_mt || "—"}
          </dd>
          {ctx.refined_from && (
            <>
              <dt>Before LLM refinement</dt>
              <dd dir={dir}>{ctx.refined_from}</dd>
            </>
          )}
        </dl>
        <div className="compare-context">
          <h3>Context used</h3>
          <ul>
            <li>
              Register: <strong>{ctx.register ?? "—"}</strong> ({ctx.register_reason ?? "—"})
            </li>
            <li>
              Speaker: {ctx.speaker?.name ?? ctx.speaker?.label ?? "unknown"} · {ctx.speaker?.gender ?? "unknown"}
              {ctx.speaker?.style ? ` · ${ctx.speaker.style}` : ""}
            </li>
            <li>
              Listener: {ctx.listener?.name ?? "unknown"} · {ctx.listener?.gender ?? "unknown"}
              {ctx.listener?.reason ? ` (${ctx.listener.reason})` : ""}
              {ctx.relation ? ` · ${ctx.relation}` : ""}
            </li>
            <li>Tone: {ctx.tone ?? "—"}{ctx.humor ? " · humour detected" : ""}</li>
            {ctx.scene_note && <li>Scene: {ctx.scene_note}</li>}
            {!!ctx.locked_terms?.length && <li>Locked names/terms: {ctx.locked_terms.join(", ")}</li>}
            {!!ctx.idioms?.length && (
              <li>Idioms: {ctx.idioms.map((idiom) => `“${idiom.idiom}” → ${idiom.rendering} (${idiom.strategy})`).join("; ")}</li>
            )}
            {!!ctx.adjustments?.length && <li>Adjustments: {ctx.adjustments.join("; ")}</li>}
            {!!ctx.previous?.length && (
              <li>Before: {ctx.previous.map((entry) => `${entry.speaker}: “${entry.text}”`).join(" · ")}</li>
            )}
            {!!ctx.following?.length && (
              <li>After: {ctx.following.map((entry) => `${entry.speaker}: “${entry.text}”`).join(" · ")}</li>
            )}
          </ul>
        </div>
        <div className="translation-actions">
          <button
            type="button"
            className="button button-primary"
            onClick={onUseAi}
            disabled={!item?.ai_text || item.ai_text === item.text}
          >
            Use AI suggestion
          </button>
        </div>
        {!!item?.history.length && (
          <div className="compare-history">
            <h3>History</h3>
            <ol reversed>
              {item.history
                .map((entry, index) => ({ entry, index }))
                .reverse()
                .map(({ entry, index }) => (
                  <li key={`${entry.at}-${index}`}>
                    <span dir={dir} lang={language}>
                      {entry.text || "—"}
                    </span>
                    <small>
                      {entry.origin} · {entry.reason} · {new Date(entry.at).toLocaleString()}
                    </small>
                    <button type="button" className="button button-quiet button-small" onClick={() => onRestore(index)}>
                      Restore
                    </button>
                  </li>
                ))}
            </ol>
          </div>
        )}
      </div>
    </div>
  );
}
