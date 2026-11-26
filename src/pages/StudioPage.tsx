import { useCallback, useRef, useState } from "react";
import { AudioMixerPanel } from "../components/AudioMixerPanel";
import { AudioPreviewPanel } from "../components/AudioPreviewPanel";
import { CharacterPanel } from "../components/CharacterPanel";
import { DubAudioPanel } from "../components/DubAudioPanel";
import { JobProgress } from "../components/JobProgress";
import { StatusBadge } from "../components/States";
import { TranscriptPanel } from "../components/TranscriptPanel";
import { TranslationPanel } from "../components/TranslationPanel";
import { UploadPanel } from "../components/UploadPanel";
import { VoiceProfilePanel } from "../components/VoiceProfilePanel";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { useAsync } from "../hooks/useAsync";
import { api } from "../lib/api";
import { formatDuration } from "../lib/format";
import type { Job, Project, UploadAccepted } from "../types";

interface StudioPageProps {
  projectId: string;
  maxUploadMb: number;
}

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
  const [mixerPreviewActive, setMixerPreviewActive] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
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
                ref={videoRef}
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

            <section className="studio-mixer-slot">
              <AudioMixerPanel
                key={selectedVideo!.id}
                projectId={projectId}
                videoId={selectedVideo!.id}
                videoRef={videoRef}
                onPreviewChange={setMixerPreviewActive}
              />
            </section>

            <section className="studio-audio-slot">
              <AudioPreviewPanel
                videoId={selectedVideo!.id}
                onJobStarted={(jobId) => setActiveJobId(jobId)}
                previewActive={mixerPreviewActive}
              />
            </section>

            <section className="panel studio-transcript">
              <div className="panel-heading">
                <div>
                  <h2>Transcript</h2>
                  <p>Timestamped dialogue from open-source speech recognition.</p>
                </div>
                <span className="chip">Editable · synced</span>
              </div>
              <TranscriptPanel
                key={selectedVideo!.id}
                videoId={selectedVideo!.id}
                videoRef={videoRef}
                previewActive={mixerPreviewActive}
                onJobStarted={(jobId) => setActiveJobId(jobId)}
              />
            </section>

            <section className="panel studio-characters">
              <div className="panel-heading">
                <div>
                  <h2>Character List</h2>
                  <p>Speaker turns, voice metrics and visible person candidates from detection.</p>
                </div>
                <span className="chip">Detection · live</span>
              </div>
              <CharacterPanel
                key={selectedVideo!.id}
                projectId={projectId}
                videoId={selectedVideo!.id}
                videoRef={videoRef}
                previewActive={mixerPreviewActive}
                onJobStarted={(jobId) => setActiveJobId(jobId)}
              />
            </section>

            <section className="panel studio-voices">
              <div className="panel-heading">
                <div>
                  <h2>Voice Profiles</h2>
                  <p>Per-character voice assignment, customization, consented samples and real previews.</p>
                </div>
                <span className="chip">Profiles · live</span>
              </div>
              <VoiceProfilePanel key={selectedVideo!.id} projectId={projectId} videoId={selectedVideo!.id} />
            </section>

            <section className="panel studio-translation">
              <div className="panel-heading">
                <div>
                  <h2>Translation Editor</h2>
                  <p>Context-aware English → Urdu (with Roman Urdu) and Hindi, side by side. English stays unchanged.</p>
                </div>
                <span className="chip">Open-source MT · editable</span>
              </div>
              <TranslationPanel
                key={selectedVideo!.id}
                projectId={projectId}
                videoId={selectedVideo!.id}
                videoRef={videoRef}
                previewActive={mixerPreviewActive}
                onJobStarted={(jobId) => setActiveJobId(jobId)}
              />
            </section>

            <section className="panel studio-dub-audio">
              <div className="panel-heading">
                <div>
                  <h2>Voice Generation</h2>
                  <p>Synthetic Urdu and Hindi dialogue in each character's voice. Preview, regenerate, approve and download.</p>
                </div>
                <span className="chip">AI text-to-speech</span>
              </div>
              <DubAudioPanel key={selectedVideo!.id} videoId={selectedVideo!.id} onJobStarted={(jobId) => setActiveJobId(jobId)} />
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
                structure only; generated stems are previewed in the Audio Stems panel above.
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
