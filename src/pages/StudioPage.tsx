import { useCallback, useState } from "react";
import { JobProgress } from "../components/JobProgress";
import { StatusBadge } from "../components/States";
import { UploadPanel } from "../components/UploadPanel";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import { formatDuration } from "../lib/format";
import type { Job, Project, UploadAccepted } from "../types";

interface StudioPageProps {
  projectId: string;
  maxUploadMb: number;
}

interface Drafts {
  english: string;
  urdu: string;
  hindi: string;
}

const CHARACTER_PLACEHOLDERS = [
  { name: "Character 01", speaker: "Not detected", voice: "Unassigned" },
  { name: "Character 02", speaker: "Not detected", voice: "Unassigned" },
  { name: "Character 03", speaker: "Not detected", voice: "Unassigned" },
];

const TRACKS = [
  { name: "Original English", detail: "Source audio", kind: "source" },
  { name: "Urdu Dub", detail: "No generated audio", kind: "planned" },
  { name: "Hindi Dub", detail: "No generated audio", kind: "planned" },
  { name: "Background Music", detail: "No separated stem", kind: "planned" },
  { name: "Sound Effects", detail: "No separated stem", kind: "planned" },
] as const;

export function StudioPage({ projectId, maxUploadMb }: StudioPageProps) {
  const [selectedVideoId, setSelectedVideoId] = useState<string | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Drafts>({ english: "", urdu: "", hindi: "" });
  const project = useAsync(() => api.getProject(projectId), [projectId]);
  const jobs = useAsync(() => api.listProjectJobs(projectId), [projectId]);

  const refreshAll = useCallback(() => {
    project.reload();
    jobs.reload();
  }, [project.reload, jobs.reload]);

  const onUploaded = useCallback(
    (result: UploadAccepted) => {
      setActiveJobId(result.job.id);
      refreshAll();
    },
    [refreshAll],
  );

  const updateDraft = (field: keyof Drafts, value: string) => {
    setDrafts((current) => ({ ...current, [field]: value }));
  };

  if (!project.data && project.loading) return <LoadingState label="Loading studio…" />;
  if (!project.data && project.error) return <ErrorState message={project.error} onRetry={project.reload} />;
  if (!project.data) return <EmptyState title="Project not found" message="Select another project from the list." />;

  const current: Project = project.data;
  const videos = current.videos;
  const selectedVideo = videos.find((item) => item.id === selectedVideoId) ?? videos[0] ?? null;
  const toolbarJob: Job | null =
    jobs.data?.find((item) => item.id === activeJobId) ??
    jobs.data?.find((item) => item.video_id === selectedVideo?.id) ??
    jobs.data?.[0] ??
    null;
  const durationMs = selectedVideo?.duration_ms ?? 0;
  const rulerMarks = durationMs > 0
    ? [0, 0.25, 0.5, 0.75, 1].map((fraction) => ({
        fraction,
        label: formatDuration(Math.round(durationMs * fraction)),
      }))
    : [
        { fraction: 0, label: "0:00" },
        { fraction: 1, label: "—" },
      ];

  return (
    <div className="page studio-page">
      <div className="breadcrumb">
        <a href="#/projects">Projects</a>
        <span>/</span>
        <a href={`#/projects/${projectId}`}>{current.name}</a>
        <span>/</span>
        <span>Studio</span>
      </div>

      <section className="panel project-header">
        <div>
          <span className="eyebrow">Dubbing studio</span>
          <h2>{current.name}</h2>
          <p>Layout draft for source review, dialogue, voices, translation and audio tracks.</p>
        </div>
        <div className="project-header-meta">
          <StatusBadge value={current.status} />
          <span>Source: {current.source_language.toUpperCase()}</span>
          <span>Targets: {current.target_languages.join(", ").toUpperCase()}</span>
          <a className="button button-quiet" href={`#/projects/${projectId}`}>
            Back to workspace
          </a>
        </div>
      </section>

      {videos.length === 0 ? (
        <>
          <EmptyState
            title="No source video yet"
            message="Upload a source video to enable the studio layout. AI dubbing stages are not enabled in this phase."
          />
          <UploadPanel projectId={projectId} maxUploadMb={maxUploadMb} onUploaded={onUploaded} />
        </>
      ) : (
        <>
          <section className="panel studio-toolbar">
            <div className="studio-toolbar-field">
              <span>Active source</span>
              <select value={selectedVideo?.id ?? ""} onChange={(event) => setSelectedVideoId(event.target.value)}>
                {videos.map((video) => (
                  <option key={video.id} value={video.id}>
                    {video.original_filename}
                  </option>
                ))}
              </select>
            </div>
            <div className="studio-toolbar-status">
              <span>Import status</span>
              <div>
                <StatusBadge value={toolbarJob?.status ?? "idle"} />
                <strong>{toolbarJob?.current_step ?? "No active import step"}</strong>
              </div>
            </div>
            {toolbarJob && (
              <div className="studio-toolbar-job">
                <span>Job</span>
                <JobProgress jobId={toolbarJob.id} onSettled={refreshAll} />
              </div>
            )}
          </section>

          <div className="studio-layout">
            <section className="panel studio-original">
              <div className="panel-heading">
                <div>
                  <h2>Original Video</h2>
                  <p>Real uploaded source, streamed without re-encoding.</p>
                </div>
                <span className="chip">Source</span>
              </div>
              <video
                controls
                preload="metadata"
                src={api.videoUrl(selectedVideo!)}
                aria-label={`Studio preview of ${selectedVideo!.original_filename}`}
              />
              <div className="studio-video-meta">
                <div>
                  <span>File</span>
                  <strong title={selectedVideo!.original_filename}>{selectedVideo!.original_filename}</strong>
                </div>
                <div>
                  <span>Duration</span>
                  <strong>{formatDuration(selectedVideo!.duration_ms)}</strong>
                </div>
                <div>
                  <span>Resolution</span>
                  <strong>
                    {selectedVideo!.width && selectedVideo!.height
                      ? `${selectedVideo!.width}×${selectedVideo!.height}`
                      : "Unknown"}
                  </strong>
                </div>
                <div>
                  <span>Frame rate</span>
                  <strong>{selectedVideo!.fps ? `${Number(selectedVideo!.fps.toFixed(3))} fps` : "Unknown"}</strong>
                </div>
              </div>
            </section>

            <section className="panel studio-transcript">
              <div className="panel-heading">
                <div>
                  <h2>Transcript</h2>
                  <p>Manual draft surface for dialogue review.</p>
                </div>
                <span className="chip chip-planned">AI transcription · planned</span>
              </div>
              <p className="planned-note">
                Speech recognition is not enabled yet. Text entered here stays in this browser session and is not sent
                to the server.
              </p>
              <textarea
                value={drafts.english}
                onChange={(event) => updateDraft("english", event.target.value)}
                placeholder="Draft source dialogue appears here…"
                rows={9}
                aria-label="Draft English transcript"
              />
              <small className="draft-note">Local draft only · not saved</small>
            </section>

            <section className="panel studio-characters">
              <div className="panel-heading">
                <div>
                  <h2>Character List</h2>
                  <p>Speaker identity and voice assignment layout.</p>
                </div>
                <span className="chip chip-planned">Detection · planned</span>
              </div>
              <div className="character-table" role="table" aria-label="Character list layout">
                <div className="character-row character-head" role="row">
                  <span role="columnheader">Character</span>
                  <span role="columnheader">Speaker identity</span>
                  <span role="columnheader">Voice profile</span>
                </div>
                {CHARACTER_PLACEHOLDERS.map((character) => (
                  <div className="character-row" role="row" key={character.name}>
                    <span role="cell">{character.name}</span>
                    <span role="cell">{character.speaker}</span>
                    <span role="cell">{character.voice}</span>
                  </div>
                ))}
              </div>
              <p className="planned-note">No characters detected yet. These rows show the intended columns only.</p>
            </section>

            <section className="panel studio-voices">
              <div className="panel-heading">
                <div>
                  <h2>Voice Profiles</h2>
                  <p>Reference and target voice slots.</p>
                </div>
                <span className="chip chip-planned">Generation · planned</span>
              </div>
              <ul className="voice-list">
                <li>
                  <span className="voice-avatar">EN</span>
                  <div>
                    <strong>Original English</strong>
                    <small>Reference track · {selectedVideo!.audio_codec ?? "audio codec unknown"}</small>
                  </div>
                  <span className="badge badge-draft">reference</span>
                </li>
                <li>
                  <span className="voice-avatar">UR</span>
                  <div>
                    <strong>Urdu voice</strong>
                    <small>No profile generated</small>
                  </div>
                  <span className="chip chip-planned">planned</span>
                </li>
                <li>
                  <span className="voice-avatar">HI</span>
                  <div>
                    <strong>Hindi voice</strong>
                    <small>No profile generated</small>
                  </div>
                  <span className="chip chip-planned">planned</span>
                </li>
              </ul>
            </section>

            <section className="panel studio-translation">
              <div className="panel-heading">
                <div>
                  <h2>Translation Panel</h2>
                  <p>Editable source and target text drafts.</p>
                </div>
                <span className="chip chip-planned">AI translation · planned</span>
              </div>
              <div className="translation-grid">
                <label className="translation-field">
                  <span>English · source</span>
                  <textarea
                    value={drafts.english}
                    onChange={(event) => updateDraft("english", event.target.value)}
                    placeholder="English dialogue draft…"
                    rows={7}
                  />
                </label>
                <label className="translation-field">
                  <span>Urdu · target</span>
                  <textarea
                    value={drafts.urdu}
                    onChange={(event) => updateDraft("urdu", event.target.value)}
                    placeholder="اردو ترجمہ یہاں لکھیں…"
                    rows={7}
                    dir="rtl"
                  />
                </label>
                <label className="translation-field">
                  <span>Hindi · target</span>
                  <textarea
                    value={drafts.hindi}
                    onChange={(event) => updateDraft("hindi", event.target.value)}
                    placeholder="हिंदी अनुवाद यहाँ लिखें…"
                    rows={7}
                  />
                </label>
              </div>
              <small className="draft-note">Local draft only · not saved · no translation service is connected</small>
            </section>

            <section className="panel studio-timeline">
              <div className="panel-heading">
                <div>
                  <h2>Timeline Editor</h2>
                  <p>Static track layout using the real source duration.</p>
                </div>
                <span className="chip chip-planned">Editing · planned</span>
              </div>
              <p className="planned-note">
                Playback, clipping, mixing and export are not enabled in this phase. Lanes show the intended track
                structure only.
              </p>
              <div className="timeline-scroll">
                <div className="timeline-ruler" aria-label="Timeline ruler">
                  {rulerMarks.map((mark) => (
                    <span key={`${mark.fraction}-${mark.label}`} style={{ left: `${mark.fraction * 100}%` }}>
                      {mark.label}
                    </span>
                  ))}
                </div>
                <div className="track-lanes">
                  {TRACKS.map((track) => (
                    <div className="track-lane" key={track.name}>
                      <div className="track-label">
                        <strong>{track.name}</strong>
                        <small>{track.kind === "source" && selectedVideo?.audio_codec ? `Source audio · ${selectedVideo.audio_codec}` : track.detail}</small>
                      </div>
                      <div className={`track-wave track-wave-${track.kind}`} aria-hidden="true" />
                      <span className={track.kind === "source" ? "chip" : "chip chip-planned"}>
                        {track.kind === "source" ? "present" : "planned"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
