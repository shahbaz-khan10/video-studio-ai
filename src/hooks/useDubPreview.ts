// Phase 11 — play fitted dub clips in sync with the <video> element.
//
// Each clip is scheduled on a Web Audio clock relative to video.currentTime.
// Play, pause, seek and rate changes reschedule everything; a watchdog compares
// the video clock with the audio clock and reschedules if they drift apart.
import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

export interface PreviewClip {
  id: string;
  url: string;
  version: string;
  startMs: number;
  endMs: number;
}

const LOOKAHEAD_S = 20;
const DRIFT_LIMIT_S = 0.04;

export function useDubPreview(videoRef: RefObject<HTMLVideoElement | null>, clips: PreviewClip[], enabled: boolean, muteOriginal: boolean) {
  const [status, setStatus] = useState<{ scheduled: number; corrections: number; error: string | null }>({
    scheduled: 0,
    corrections: 0,
    error: null,
  });
  const ctxRef = useRef<AudioContext | null>(null);
  const buffers = useRef(new Map<string, Promise<AudioBuffer | null>>());
  const playing = useRef(new Map<string, AudioBufferSourceNode>());
  const anchor = useRef({ ctx: 0, video: 0, rate: 1 });
  const clipsRef = useRef(clips);
  clipsRef.current = clips;

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const previous = video.muted;
    if (enabled) video.muted = muteOriginal;
    return () => {
      video.muted = previous;
    };
  }, [videoRef, enabled, muteOriginal]);

  useEffect(() => {
    const video = videoRef.current;
    if (!enabled || !video) return;
    let active = true;
    let corrections = 0;
    const ctx = ctxRef.current ?? new AudioContext({ latencyHint: "interactive" });
    ctxRef.current = ctx;

    const load = (clip: PreviewClip) => {
      const key = `${clip.id}:${clip.version}`;
      let promise = buffers.current.get(key);
      if (!promise) {
        promise = fetch(clip.url)
          .then((response) => (response.ok ? response.arrayBuffer() : Promise.reject(new Error(`audio ${response.status}`))))
          .then((data) => ctx.decodeAudioData(data))
          .catch(() => null);
        buffers.current.set(key, promise);
      }
      return promise;
    };

    const stopAll = () => {
      playing.current.forEach((node) => {
        try {
          node.stop();
        } catch {
          /* already stopped */
        }
      });
      playing.current.clear();
    };

    const schedule = async () => {
      if (!active || video.paused) return;
      if (ctx.state === "suspended") await ctx.resume().catch(() => undefined);
      const rate = video.playbackRate || 1;
      const nowVideo = video.currentTime;
      const nowCtx = ctx.currentTime;
      anchor.current = { ctx: nowCtx, video: nowVideo, rate };
      const upcoming = clipsRef.current.filter(
        (clip) => clip.endMs / 1000 > nowVideo && clip.startMs / 1000 < nowVideo + LOOKAHEAD_S,
      );
      for (const clip of upcoming) {
        const key = `${clip.id}:${clip.version}`;
        if (playing.current.has(key)) continue;
        const buffer = await load(clip);
        if (!active || !buffer || video.paused || playing.current.has(key)) continue;
        // Read the video clock right now (after the possibly slow decode):
        // during start-up the video can stall, so an extrapolated anchor drifts.
        const videoTime = video.currentTime;
        const offset = videoTime - clip.startMs / 1000;
        if (offset >= buffer.duration) continue;
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.playbackRate.value = rate;
        source.connect(ctx.destination);
        if (offset >= 0) source.start(ctx.currentTime, offset);
        else source.start(ctx.currentTime + -offset / rate);
        source.onended = () => playing.current.delete(key);
        playing.current.set(key, source);
        anchor.current = { ctx: ctx.currentTime, video: video.currentTime, rate };
      }
      setStatus((value) => ({ ...value, scheduled: playing.current.size }));
    };

    // Several events fire together around a seek (seeked, waiting, playing);
    // coalesce them so clips are scheduled once, after the clock settles.
    let pending: number | undefined;
    const restart = () => {
      stopAll();
      window.clearTimeout(pending);
      pending = window.setTimeout(() => void schedule(), 30);
    };
    const onPause = () => stopAll();
    // "playing" (not "play") fires once frames actually advance, so the
    // anchor is taken from a running clock.
    video.addEventListener("playing", restart);
    video.addEventListener("seeked", restart);
    video.addEventListener("ratechange", restart);
    video.addEventListener("pause", onPause);
    video.addEventListener("waiting", onPause);
    if (!video.paused) restart();

    const watchdog = window.setInterval(() => {
      if (!active || video.paused) return;
      const expected = anchor.current.video + (ctx.currentTime - anchor.current.ctx) * anchor.current.rate;
      if (Math.abs(video.currentTime - expected) > DRIFT_LIMIT_S) {
        corrections += 1;
        setStatus((value) => ({ ...value, corrections }));
        restart();
      } else {
        void schedule(); // pick up clips entering the look-ahead window
      }
    }, 400);

    return () => {
      active = false;
      window.clearInterval(watchdog);
      window.clearTimeout(pending);
      video.removeEventListener("playing", restart);
      video.removeEventListener("seeked", restart);
      video.removeEventListener("ratechange", restart);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("waiting", onPause);
      stopAll();
    };
  }, [videoRef, enabled, clips]);

  useEffect(
    () => () => {
      void ctxRef.current?.close().catch(() => undefined);
      ctxRef.current = null;
    },
    [],
  );

  return status;
}
