import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, RefObject, SetStateAction } from "react";
import { api } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type {
  Character,
  CharacterBundle,
  CharacterDetectionRequest,
  DialogueItem,
  FaceTrack,
  SpeakerTurn,
  VisualCandidate,
  VisualCandidateUpdate,
} from "../types";
import { JobProgress } from "./JobProgress";
import { ErrorState, LoadingState, StatusBadge } from "./States";

interface CharacterPanelProps {
  projectId: string;
  videoId: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  previewActive: boolean;
  onJobStarted: (jobId: string) => void;
}

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

function characterName(characters: Character[], id: string | null): string {
  if (!id) return "Unassigned";
  return characters.find((character) => character.id === id)?.name ?? `Character ${shortId(id)}`;
}

function confidenceLabel(value: number | null): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return `${Math.round(value * 100)}% conf.`;
}

function isUncertain(character: Character): boolean {
  const status = character.status.toLowerCase();
  const needsReview =
    status === "review" ||
    status.includes("needs_review") ||
    status.includes("needs review") ||
    status.includes("uncertain") ||
    status.includes("draft") ||
    status.includes("pending");
  return (
    needsReview ||
    (character.confidence !== null && Number.isFinite(character.confidence) && character.confidence < 0.6)
  );
}

function formatVoiceValue(value: unknown): string {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "n/a";
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) {
    const values = value.slice(0, 3).map(formatVoiceValue);
    return `${values.join(", ")}${value.length > 3 ? "…" : ""}`;
  }
  if (value && typeof value === "object") return JSON.stringify(value)?.slice(0, 64) ?? "n/a";
  return "n/a";
}

function voiceSummary(character: Character): string {
  const entries = Object.entries(character.voice_characteristics ?? {}).slice(0, 4);
  if (entries.length === 0) return "No measurable voice metrics yet";
  return entries.map(([key, value]) => `${key.replace(/_/g, " ")}: ${formatVoiceValue(value)}`).join(" · ");
}

function toggleInList(setter: Dispatch<SetStateAction<string[]>>, id: string) {
  setter((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
}

export function CharacterPanel({ projectId, videoId, videoRef, previewActive, onJobStarted }: CharacterPanelProps) {
  const [bundle, setBundle] = useState<CharacterBundle | null>(null);
  const [nameDrafts, setNameDrafts] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const [preferDialogueStem, setPreferDialogueStem] = useState(true);
  const [detectFaces, setDetectFaces] = useState(true);
  const [maxCandidates, setMaxCandidates] = useState("24");
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [jobActive, setJobActive] = useState(false);
  const [loadVersion, setLoadVersion] = useState(0);
  const [currentMs, setCurrentMs] = useState(0);
  const [mergeSelected, setMergeSelected] = useState<string[]>([]);
  const [mergeName, setMergeName] = useState("");
  const [splitSourceId, setSplitSourceId] = useState("");
  const [splitSegmentIds, setSplitSegmentIds] = useState<string[]>([]);
  const [splitTurnIds, setSplitTurnIds] = useState<string[]>([]);
  const [splitFaceTrackIds, setSplitFaceTrackIds] = useState<string[]>([]);
  const [splitName, setSplitName] = useState("");
  const [confirmTargets, setConfirmTargets] = useState<Record<string, string>>({});
  const [candidateNotes, setCandidateNotes] = useState<Record<string, string>>({});
  const [projectRoster, setProjectRoster] = useState<Character[]>([]);
  const nameInputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  const applyBundle = useCallback((next: CharacterBundle) => {
    setBundle(next);
    setNameDrafts((current) => {
      const result: Record<string, string> = {};
      next.characters.forEach((character) => {
        result[character.id] = current[character.id] ?? character.name;
      });
      return result;
    });
    setMergeSelected((current) => current.filter((id) => next.characters.some((item) => item.id === id)));
    setConfirmTargets((current) => {
      const result: Record<string, string> = {};
      next.visual_candidates.forEach((candidate) => {
        result[candidate.id] = current[candidate.id] ?? candidate.confirmed_character_id ?? "";
      });
      return result;
    });
    setCandidateNotes((current) => {
      const result: Record<string, string> = {};
      next.visual_candidates.forEach((candidate) => {
        result[candidate.id] = current[candidate.id] ?? candidate.notes ?? "";
      });
      return result;
    });
  }, []);

  const dirty = useMemo(
    () =>
      !!bundle &&
      bundle.characters.some((character) => (nameDrafts[character.id] ?? character.name) !== character.name),
    [bundle, nameDrafts],
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
    if (!force && dirtyRef.current && !window.confirm("Discard unsaved character name edits and reload?")) return;
    setLoadVersion((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api
      .getCharacterBundle(videoId)
      .then((result) => {
        if (active) applyBundle(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not load the character bundle.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [videoId, loadVersion, applyBundle]);

  useEffect(() => {
    setMergeSelected([]);
    setMergeName("");
    setSplitSourceId("");
    setSplitSegmentIds([]);
    setSplitTurnIds([]);
    setSplitFaceTrackIds([]);
    setSplitName("");
    setConfirmTargets({});
    setCandidateNotes({});
    setActiveJobId(null);
    setJobActive(false);
    setActionError(null);
  }, [videoId]);

  useEffect(() => {
    let active = true;
    api
      .listProjectCharacters(projectId)
      .then((result) => {
        if (active) setProjectRoster(result);
      })
      .catch(() => {
        if (active) setProjectRoster([]);
      });
    return () => {
      active = false;
    };
  }, [projectId, loadVersion]);

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

  const canDiscardNames = useCallback(
    () => !dirty || window.confirm("Discard unsaved character name edits and continue?"),
    [dirty],
  );

  const startDetection = async () => {
    if (starting || jobActive || !canDiscardNames()) return;
    const parsed = Number(maxCandidates);
    const payload: CharacterDetectionRequest = {
      prefer_dialogue_stem: preferDialogueStem,
      detect_faces: detectFaces,
      max_candidates: Number.isFinite(parsed) ? Math.min(200, Math.max(1, Math.round(parsed))) : 24,
    };
    setStarting(true);
    setActionError(null);
    try {
      const job = await api.startCharacterDetection(videoId, payload);
      setActiveJobId(job.id);
      setJobActive(true);
      onJobStarted(job.id);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not start character detection.");
    } finally {
      setStarting(false);
    }
  };

  const handleJobSettled = useCallback(() => {
    setJobActive(false);
    reload(true);
  }, [reload]);

  const replaceCharacter = useCallback((updated: Character) => {
    setBundle((current) =>
      current
        ? { ...current, characters: current.characters.map((item) => (item.id === updated.id ? updated : item)) }
        : current,
    );
    setNameDrafts((current) => ({ ...current, [updated.id]: updated.name }));
  }, []);

  const saveName = async (character: Character) => {
    if (savingId || busy) return;
    const name = (nameDrafts[character.id] ?? character.name).trim();
    if (!name || name === character.name) return;
    setSavingId(character.id);
    setActionError(null);
    try {
      replaceCharacter(await api.updateCharacter(character.id, { name }));
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not rename the character.");
    } finally {
      setSavingId(null);
    }
  };

  const markReviewed = async (character: Character) => {
    if (busy || savingId) return;
    setBusy(true);
    setActionError(null);
    try {
      replaceCharacter(await api.updateCharacter(character.id, { status: "reviewed" }));
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not mark the character as reviewed.");
    } finally {
      setBusy(false);
    }
  };

  const mergeSelectedCharacters = async () => {
    if (busy || mergeSelected.length < 2 || !canDiscardNames()) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.mergeCharacters(mergeSelected[0], {
        character_ids: mergeSelected,
        name: mergeName.trim() || null,
      });
      setMergeSelected([]);
      setMergeName("");
      reload(true);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not merge the selected characters.");
    } finally {
      setBusy(false);
    }
  };

  const splitCharacter = async () => {
    if (busy || !splitSourceId || !canDiscardNames()) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.splitCharacter(splitSourceId, {
        name: splitName.trim() || null,
        segment_ids: splitSegmentIds,
        turn_ids: splitTurnIds,
        face_track_ids: splitFaceTrackIds,
      });
      setSplitSourceId("");
      setSplitSegmentIds([]);
      setSplitTurnIds([]);
      setSplitFaceTrackIds([]);
      setSplitName("");
      reload(true);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not split the character.");
    } finally {
      setBusy(false);
    }
  };

  const assignSegment = async (item: DialogueItem, characterId: string) => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      applyBundle(
        await api.assignSegmentCharacter(videoId, item.segment_id, {
          character_id: characterId || null,
          reviewed: true,
        }),
      );
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not reassign this dialogue segment.");
    } finally {
      setBusy(false);
    }
  };

  const updateCandidate = async (candidate: VisualCandidate, patch: VisualCandidateUpdate) => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      applyBundle(await api.updateVisualCandidate(candidate.id, patch));
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Could not update the visual candidate.");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <LoadingState label="Loading characters…" />;
  if (error) return <ErrorState message={error} onRetry={() => reload(true)} />;

  const characters = bundle?.characters ?? [];
  const dialogue = bundle?.dialogue ?? [];
  const speakerTurns = bundle?.speaker_turns ?? [];
  const faceTracks = bundle?.face_tracks ?? [];
  const candidates = bundle?.visual_candidates ?? [];
  const reviewedAssignments = (bundle?.assignments ?? []).filter((item) => item.reviewed).length;
  const activeTurns = speakerTurns.filter((turn) => currentMs >= turn.start_ms && currentMs <= turn.end_ms).length;
  const splitDialogue = dialogue.filter((item) => item.character_id === splitSourceId);
  const splitTurns = speakerTurns.filter((turn) => turn.character_id === splitSourceId);
  const splitTracks = faceTracks.filter((track) => track.character_id === splitSourceId);
  const notes = [...(bundle?.limitations ?? []), ...(bundle?.notes ?? [])];

  return (
    <div className="character-panel">
      <div className="character-controls">
        <label className="character-check">
          <input
            type="checkbox"
            checked={preferDialogueStem}
            onChange={(event) => setPreferDialogueStem(event.target.checked)}
          />
          Prefer dialogue stem
        </label>
        <label className="character-check">
          <input type="checkbox" checked={detectFaces} onChange={(event) => setDetectFaces(event.target.checked)} />
          Detect faces
        </label>
        <label className="character-field">
          <span>Max candidates</span>
          <input
            type="number"
            min="1"
            max="200"
            value={maxCandidates}
            onChange={(event) => setMaxCandidates(event.target.value)}
            aria-label="Maximum visual candidates"
          />
        </label>
        <button
          className="button button-primary"
          type="button"
          onClick={startDetection}
          disabled={starting || jobActive}
          aria-label="Start character detection"
        >
          {starting ? "Starting…" : "Detect characters"}
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

      <div className="character-meta">
        <StatusBadge value={bundle?.status ?? "idle"} />
        <span>{characters.length} characters</span>
        <span>{projectRoster.length} in project roster</span>
        <span>{dialogue.length} dialogue items · {reviewedAssignments} reviewed assignments</span>
        <span>Playhead {formatTimestamp(currentMs)} · {activeTurns} active turn{activeTurns === 1 ? "" : "s"}</span>
        {bundle?.updated_at && <span>Updated {bundle.updated_at}</span>}
      </div>

      {characters.length === 0 ? (
        <p className="character-empty">
          No characters detected yet. Run detection to group speaker turns, voice metrics and visible person
          candidates. Voice-over speakers may never appear on screen.
        </p>
      ) : (
        <div className="character-list">
          {characters.map((character) => {
            const uncertain = isUncertain(character);
            const name = nameDrafts[character.id] ?? character.name;
            const confidence = confidenceLabel(character.confidence);
            return (
              <article className={`character-card${uncertain ? " is-uncertain" : ""}`} key={character.id}>
                <div className="character-card-head">
                  <span className="character-id" title={character.id}>ID {shortId(character.id)}</span>
                  <StatusBadge value={character.status} />
                  {uncertain && <span className="badge badge-review">needs review</span>}
                  {confidence && <span className="character-confidence">{confidence}</span>}
                </div>
                <div className="character-name-row">
                  <label className="character-field">
                    <span>Name</span>
                    <input
                      ref={(element) => {
                        nameInputRefs.current[character.id] = element;
                      }}
                      type="text"
                      value={name}
                      onChange={(event) =>
                        setNameDrafts((current) => ({ ...current, [character.id]: event.target.value }))
                      }
                      aria-label={`Rename character ${shortId(character.id)}`}
                    />
                  </label>
                  <button
                    className="button button-quiet"
                    type="button"
                    onClick={() => {
                      const input = nameInputRefs.current[character.id];
                      input?.focus();
                      input?.select();
                    }}
                    aria-label={`Edit name for ${character.name}`}
                  >
                    Rename
                  </button>
                  <button
                    className="button button-primary"
                    type="button"
                    onClick={() => saveName(character)}
                    disabled={!name.trim() || name.trim() === character.name || savingId === character.id || busy}
                    aria-label={`Save name for ${character.name}`}
                  >
                    {savingId === character.id ? "Saving…" : "Save"}
                  </button>
                </div>
                <dl className="character-facts">
                  <div>
                    <dt>Source label</dt>
                    <dd>{character.source_label ?? "unassigned"}</dd>
                  </div>
                  <div>
                    <dt>Dialogue turns</dt>
                    <dd>{character.dialogue_count}</dd>
                  </div>
                  <div>
                    <dt>Scenes</dt>
                    <dd>{character.scene_count}</dd>
                  </div>
                  <div>
                    <dt>Voice metrics</dt>
                    <dd>{voiceSummary(character)}</dd>
                  </div>
                </dl>
                {character.notes && <p className="character-notes">{character.notes}</p>}
                {uncertain && character.confidence !== null && (
                  <p className="character-warning">
                    Low-confidence or review status — confirm the speaker before using this character.
                  </p>
                )}
                <div className="character-actions">
                  <button
                    className="button button-quiet"
                    type="button"
                    onClick={() => toggleInList(setMergeSelected, character.id)}
                    aria-pressed={mergeSelected.includes(character.id)}
                    aria-label={`${mergeSelected.includes(character.id) ? "Remove" : "Select"} ${character.name} for merge`}
                  >
                    {mergeSelected.includes(character.id) ? "Selected for merge" : "Select for merge"}
                  </button>
                  <button
                    className="button button-quiet"
                    type="button"
                    onClick={() => {
                      setSplitSourceId(character.id);
                      setSplitSegmentIds([]);
                      setSplitTurnIds([]);
                      setSplitFaceTrackIds([]);
                    }}
                    aria-label={`Split ${character.name}`}
                  >
                    Split
                  </button>
                  <button
                    className="button button-quiet"
                    type="button"
                    onClick={() => markReviewed(character)}
                    disabled={character.status === "reviewed" || busy || savingId !== null}
                    aria-label={`Mark ${character.name} reviewed`}
                  >
                    Mark reviewed
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <section className="character-tool" aria-label="Merge characters">
        <h3>Merge characters</h3>
        <p>Combine two or more characters that belong to the same speaker.</p>
        <div className="character-tool-grid">
          <fieldset className="character-check-list">
            <legend>Characters to merge</legend>
            {characters.length === 0 ? (
              <span className="character-muted">No characters available.</span>
            ) : (
              characters.map((character) => (
                <label key={character.id}>
                  <input
                    type="checkbox"
                    checked={mergeSelected.includes(character.id)}
                    onChange={() => toggleInList(setMergeSelected, character.id)}
                  />
                  {character.name} <small>{shortId(character.id)}</small>
                </label>
              ))
            )}
          </fieldset>
          <label className="character-field">
            <span>Merged name (optional)</span>
            <input
              type="text"
              value={mergeName}
              onChange={(event) => setMergeName(event.target.value)}
              placeholder="Keep the first character's name"
              aria-label="Merged character name"
            />
          </label>
        </div>
        <button
          className="button button-primary"
          type="button"
          onClick={mergeSelectedCharacters}
          disabled={mergeSelected.length < 2 || busy}
          aria-label="Merge selected characters"
        >
          {busy ? "Working…" : "Merge selected"}
        </button>
      </section>

      <section className="character-tool" aria-label="Split character">
        <h3>Split character</h3>
        <p>Move selected dialogue, turns or face tracks into a new character.</p>
        <div className="character-tool-grid">
          <label className="character-field">
            <span>Source character</span>
            <select
              value={splitSourceId}
              onChange={(event) => {
                setSplitSourceId(event.target.value);
                setSplitSegmentIds([]);
                setSplitTurnIds([]);
                setSplitFaceTrackIds([]);
              }}
              aria-label="Character to split"
            >
              <option value="">Choose a character</option>
              {characters.map((character) => (
                <option key={character.id} value={character.id}>{character.name}</option>
              ))}
            </select>
          </label>
          <label className="character-field">
            <span>New character name (optional)</span>
            <input
              type="text"
              value={splitName}
              onChange={(event) => setSplitName(event.target.value)}
              placeholder="Auto-generated name"
              aria-label="New character name"
            />
          </label>
        </div>
        {splitSourceId && (
          <>
            <fieldset className="character-check-list">
              <legend>Dialogue segments to move</legend>
              {splitDialogue.length === 0 ? (
                <span className="character-muted">No dialogue assigned to this character.</span>
              ) : (
                splitDialogue.map((item) => (
                  <div className="character-check-option" key={item.segment_id}>
                    <button className="character-time" type="button" onClick={() => seekTo(item.start_ms, false)}>
                      {formatTimestamp(item.start_ms)}
                    </button>
                    <label>
                      <input
                        type="checkbox"
                        checked={splitSegmentIds.includes(item.segment_id)}
                        onChange={() => toggleInList(setSplitSegmentIds, item.segment_id)}
                      />
                      <span className="character-check-text">{item.text || "(no dialogue text)"}</span>
                    </label>
                  </div>
                ))
              )}
            </fieldset>
            <fieldset className="character-check-list">
              <legend>Speaker turns (optional)</legend>
              {splitTurns.length === 0 ? (
                <span className="character-muted">No speaker turns assigned to this character.</span>
              ) : (
                splitTurns.map((turn: SpeakerTurn) => (
                  <div className="character-check-option" key={turn.id}>
                    <button className="character-time" type="button" onClick={() => seekTo(turn.start_ms, false)}>
                      {formatTimestamp(turn.start_ms)}
                    </button>
                    <label>
                      <input
                        type="checkbox"
                        checked={splitTurnIds.includes(turn.id)}
                        onChange={() => toggleInList(setSplitTurnIds, turn.id)}
                      />
                      <span className="character-check-text">{turn.label}</span>
                    </label>
                  </div>
                ))
              )}
            </fieldset>
            <fieldset className="character-check-list">
              <legend>Face tracks (optional)</legend>
              {splitTracks.length === 0 ? (
                <span className="character-muted">No face tracks assigned to this character.</span>
              ) : (
                splitTracks.map((track: FaceTrack) => (
                  <div className="character-check-option" key={track.id}>
                    <button className="character-time" type="button" onClick={() => seekTo(track.start_ms, false)}>
                      {formatTimestamp(track.start_ms)}
                    </button>
                    <label>
                      <input
                        type="checkbox"
                        checked={splitFaceTrackIds.includes(track.id)}
                        onChange={() => toggleInList(setSplitFaceTrackIds, track.id)}
                      />
                      <span className="character-check-text">{track.label}</span>
                    </label>
                  </div>
                ))
              )}
            </fieldset>
          </>
        )}
        <button
          className="button button-primary"
          type="button"
          onClick={splitCharacter}
          disabled={!splitSourceId || busy}
          aria-label="Split selected character"
        >
          {busy ? "Working…" : "Split character"}
        </button>
      </section>

      <section className="character-tool" aria-label="Dialogue and segment assignment">
        <h3>Dialogue segments</h3>
        <p>Reassign a segment when a speaker turn was attributed to the wrong character.</p>
        <div className="character-dialogue-list">
          {dialogue.length === 0 ? (
            <p className="character-empty character-empty-small">No dialogue items are available for this video.</p>
          ) : (
            dialogue.map((item, index) => (
              <article className="character-dialogue-row" key={`${item.segment_id}-${index}`}>
                <button
                  className="character-time"
                  type="button"
                  onClick={() => seekTo(item.start_ms, false)}
                  aria-label={`Seek to ${formatTimestamp(item.start_ms)}`}
                >
                  {formatTimestamp(item.start_ms)}
                </button>
                <span className="character-dialogue-text">{item.text || "(no dialogue text)"}</span>
                <span className="character-current">{characterName(characters, item.character_id)}</span>
                {item.reviewed && <span className="badge badge-completed">reviewed</span>}
                <button
                  className="button button-quiet"
                  type="button"
                  onClick={() => seekTo(item.start_ms, true)}
                  aria-label={`Play from ${formatTimestamp(item.start_ms)}`}
                >
                  Play from here
                </button>
                <label className="character-field">
                  <span>Assign to</span>
                  <select
                    value={item.character_id ?? ""}
                    onChange={(event) => assignSegment(item, event.target.value)}
                    disabled={busy}
                    aria-label={`Assign dialogue segment ${index + 1} to character`}
                  >
                    <option value="">Unassigned</option>
                    {characters.map((character) => (
                      <option key={character.id} value={character.id}>{character.name}</option>
                    ))}
                  </select>
                </label>
              </article>
            ))
          )}
        </div>
      </section>

      <section className="character-tool" aria-label="Speaker turns and face tracks">
        <h3>Speaker turns and face tracks</h3>
        <div className="character-tool-grid">
          <div className="character-turn-list">
            <h4>Speaker turns</h4>
            {speakerTurns.length === 0 ? (
              <p className="character-muted">No speaker turns detected.</p>
            ) : (
              speakerTurns.map((turn) => (
                <article className="character-turn" key={turn.id}>
                  <button
                    className="character-time"
                    type="button"
                    onClick={() => seekTo(turn.start_ms, false)}
                    aria-label={`Seek to ${formatTimestamp(turn.start_ms)}`}
                  >
                    {formatTimestamp(turn.start_ms)} → {formatTimestamp(turn.end_ms)}
                  </button>
                  <span className="character-check-text">
                    {turn.label} · {characterName(characters, turn.character_id)}
                  </span>
                  {turn.overlap_possible && <span className="badge badge-review">overlap</span>}
                  {turn.reviewed && <span className="badge badge-completed">reviewed</span>}
                  {confidenceLabel(turn.confidence) && (
                    <span className="character-confidence">{confidenceLabel(turn.confidence)}</span>
                  )}
                </article>
              ))
            )}
          </div>
          <div className="character-track-list">
            <h4>Face tracks</h4>
            {faceTracks.length === 0 ? (
              <p className="character-muted">No face tracks detected.</p>
            ) : (
              faceTracks.map((track) => (
                <article className="character-track" key={track.id}>
                  {track.representative_url ? (
                    <img
                      className="character-track-frame"
                      src={api.assetUrl(track.representative_url)}
                      alt=""
                    />
                  ) : (
                    <span className="character-track-frame character-track-empty">No still</span>
                  )}
                  <div className="character-track-body">
                    <strong>{track.label}</strong>
                    <button
                      className="character-time"
                      type="button"
                      onClick={() => seekTo(track.start_ms, false)}
                      aria-label={`Seek to ${formatTimestamp(track.start_ms)}`}
                    >
                      {formatTimestamp(track.start_ms)} → {formatTimestamp(track.end_ms)}
                    </button>
                    <span className="character-muted">
                      {track.sample_count} sample{track.sample_count === 1 ? "" : "s"} · {characterName(characters, track.character_id)}
                    </span>
                  </div>
                </article>
              ))
            )}
          </div>
        </div>
      </section>

      <section className="character-tool" aria-label="Visual candidate review">
        <h3>Visual candidates</h3>
        <p className="character-disclaimer">
          These are visible person candidates, not speaker identities or real-world recognition. A visible person may
          not be speaking; off-screen speakers and voice-over narration may have no candidate at all.
        </p>
        <div className="character-candidate-grid">
          {candidates.length === 0 ? (
            <p className="character-empty character-empty-small">No visible person candidates detected.</p>
          ) : (
            candidates.map((candidate) => {
              const track = faceTracks.find((item) => item.id === candidate.face_track_id);
              const selected = confirmTargets[candidate.id] ?? candidate.confirmed_character_id ?? "";
              const candidateConfidence = confidenceLabel(candidate.confidence);
              const state = candidate.confirmed ? "confirmed" : candidate.rejected ? "rejected" : "unconfirmed";
              return (
                <article className={`character-candidate is-${state}`} key={candidate.id}>
                  {candidate.frame_url ? (
                    <img
                      className="character-candidate-frame"
                      src={api.assetUrl(candidate.frame_url)}
                      alt="Visible person candidate frame"
                    />
                  ) : (
                    <div className="character-candidate-frame character-candidate-empty">No frame captured</div>
                  )}
                  <div className="character-candidate-body">
                    <div className="character-candidate-head">
                      <strong>Visible person candidate</strong>
                      <StatusBadge value={state} />
                    </div>
                    <button
                      className="character-time"
                      type="button"
                      onClick={() => seekTo(candidate.timestamp_ms, false)}
                      aria-label={`Seek to ${formatTimestamp(candidate.timestamp_ms)}`}
                    >
                      {formatTimestamp(candidate.timestamp_ms)}
                    </button>
                    <span className="character-muted">
                      {track?.label ?? "unknown track"} · {shortId(candidate.id)}
                    </span>
                    {candidateConfidence && <span className="character-confidence">{candidateConfidence}</span>}
                    <label className="character-field">
                      <span>Confirm as</span>
                      <select
                        value={selected}
                        onChange={(event) =>
                          setConfirmTargets((current) => ({ ...current, [candidate.id]: event.target.value }))
                        }
                        aria-label={`Choose character for candidate ${shortId(candidate.id)}`}
                      >
                        <option value="">Choose a character</option>
                        {characters.map((character) => (
                          <option key={character.id} value={character.id}>{character.name}</option>
                        ))}
                      </select>
                    </label>
                    <div className="character-candidate-actions">
                      <button
                        className="button button-primary"
                        type="button"
                        onClick={() =>
                          updateCandidate(candidate, { confirmed_character_id: selected, rejected: false })
                        }
                        disabled={!selected || busy}
                        aria-label={`Confirm candidate as ${characterName(characters, selected)}`}
                      >
                        Confirm as {selected ? characterName(characters, selected) : "character"}
                      </button>
                      <button
                        className="button button-quiet"
                        type="button"
                        onClick={() => updateCandidate(candidate, { rejected: true })}
                        disabled={busy}
                        aria-label="Reject visual candidate"
                      >
                        Reject
                      </button>
                      {candidate.rejected && (
                        <button
                          className="button button-quiet"
                          type="button"
                          onClick={() => updateCandidate(candidate, { rejected: false })}
                          disabled={busy}
                          aria-label="Restore visual candidate"
                        >
                          Restore
                        </button>
                      )}
                    </div>
                    <label className="character-field">
                      <span>Note</span>
                      <input
                        type="text"
                        value={candidateNotes[candidate.id] ?? candidate.notes ?? ""}
                        onChange={(event) =>
                          setCandidateNotes((current) => ({ ...current, [candidate.id]: event.target.value }))
                        }
                        placeholder="Optional review note"
                        aria-label={`Note for candidate ${shortId(candidate.id)}`}
                      />
                    </label>
                    <button
                      className="button button-quiet"
                      type="button"
                      onClick={() => updateCandidate(candidate, { notes: candidateNotes[candidate.id] ?? "" })}
                      disabled={busy}
                      aria-label="Save candidate note"
                    >
                      Save note
                    </button>
                  </div>
                </article>
              );
            })
          )}
        </div>
      </section>

      {notes.length > 0 && (
        <div className="audio-limitations character-limitations">
          <strong>Detection notes</strong>
          <ul>
            {notes.map((note, index) => (
              <li key={`${index}-${note}`}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      {actionError && <p className="form-error" role="alert">{actionError}</p>}
    </div>
  );
}
