// Phase 12 — live multitrack mix preview, synchronised with the <video>.
//
// Each available track streams through an <audio> element into a Web Audio
// graph: EQ (biquads) → compressor (+makeup) → fader → balance → master gain →
// limiter. Parameters update live without restarting playback. The elements
// follow the video clock: large offsets are corrected with a seek, small ones
// by nudging playbackRate so there are no audible jumps.
import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { includedRoles, type MixRole, type MixSettings, type MixTarget, type StudioTrack } from "../lib/mixStudioApi";

interface Strip {
  element: HTMLAudioElement;
  highpass: BiquadFilterNode;
  low: BiquadFilterNode;
  mid: BiquadFilterNode;
  high: BiquadFilterNode;
  compressor: DynamicsCompressorNode;
  makeup: GainNode;
  fader: GainNode;
  left: GainNode;
  right: GainNode;
}

const HARD_SYNC_S = 0.2;
const SOFT_SYNC_S = 0.015;

function db(value: number) {
  return Math.pow(10, value / 20);
}

export function useMixPreview(
  videoRef: RefObject<HTMLVideoElement | null>,
  tracks: StudioTrack[],
  settings: MixSettings | null,
  target: MixTarget,
  mode: "off" | "mix" | "original",
  urlFor: (path: string) => string,
) {
  const [state, setState] = useState<{ ready: boolean; corrections: number; maxDriftMs: number; error: string | null }>({
    ready: false,
    corrections: 0,
    maxDriftMs: 0,
    error: null,
  });
  const ctxRef = useRef<AudioContext | null>(null);
  const strips = useRef(new Map<MixRole, Strip>());
  const master = useRef<{ gain: GainNode; limiter: DynamicsCompressorNode } | null>(null);
  const trackKey = tracks.map((track) => `${track.role}:${track.available}:${track.url}`).join("|");

  // Build the graph when the set of track files changes and preview is on.
  useEffect(() => {
    if (mode === "off") return;
    const ctx = ctxRef.current ?? new AudioContext({ latencyHint: "playback" });
    ctxRef.current = ctx;
    const gain = ctx.createGain();
    const limiter = ctx.createDynamicsCompressor();
    gain.connect(limiter).connect(ctx.destination);
    master.current = { gain, limiter };
    for (const track of tracks) {
      if (!track.available || !track.url) continue;
      const element = new Audio();
      element.crossOrigin = "anonymous";
      element.preload = "auto";
      element.src = urlFor(track.url);
      const source = ctx.createMediaElementSource(element);
      const highpass = new BiquadFilterNode(ctx, { type: "highpass", Q: Math.SQRT1_2 });
      const low = new BiquadFilterNode(ctx, { type: "lowshelf", frequency: 120 });
      const mid = new BiquadFilterNode(ctx, { type: "peaking", Q: 1.0 });
      const high = new BiquadFilterNode(ctx, { type: "highshelf", frequency: 8000 });
      const compressor = ctx.createDynamicsCompressor();
      const makeup = ctx.createGain();
      const fader = ctx.createGain();
      const splitter = ctx.createChannelSplitter(2);
      const merger = ctx.createChannelMerger(2);
      const left = ctx.createGain();
      const right = ctx.createGain();
      source.connect(highpass).connect(low).connect(mid).connect(high).connect(compressor).connect(makeup).connect(fader);
      fader.channelCountMode = "explicit";
      fader.channelCount = 2; // mono files are up-mixed to both sides like the render
      fader.connect(splitter);
      splitter.connect(left, 0).connect(merger, 0, 0);
      splitter.connect(right, 1).connect(merger, 0, 1);
      merger.connect(gain);
      strips.current.set(track.role, { element, highpass, low, mid, high, compressor, makeup, fader, left, right });
    }
    setState((value) => ({ ...value, ready: true }));
    return () => {
      strips.current.forEach((strip) => {
        strip.element.pause();
        strip.element.removeAttribute("src");
        strip.element.load();
      });
      strips.current.clear();
      gain.disconnect();
      limiter.disconnect();
      master.current = null;
      setState((value) => ({ ...value, ready: false }));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackKey, mode === "off"]);

  // Apply settings live.
  useEffect(() => {
    const ctx = ctxRef.current;
    if (!ctx || !settings || mode === "off") return;
    const now = ctx.currentTime;
    const audible = new Set(mode === "mix" ? includedRoles(settings, tracks, target) : []);
    strips.current.forEach((strip, role) => {
      const t = settings.tracks[role];
      const eq = t.eq.enabled;
      strip.highpass.frequency.setTargetAtTime(eq && t.eq.highpass_hz > 20.5 ? t.eq.highpass_hz : 10, now, 0.02);
      strip.low.gain.setTargetAtTime(eq ? t.eq.low_gain_db : 0, now, 0.02);
      strip.mid.frequency.setTargetAtTime(t.eq.mid_freq_hz, now, 0.02);
      strip.mid.gain.setTargetAtTime(eq ? t.eq.mid_gain_db : 0, now, 0.02);
      strip.high.gain.setTargetAtTime(eq ? t.eq.high_gain_db : 0, now, 0.02);
      const comp = t.compressor;
      strip.compressor.threshold.setValueAtTime(comp.enabled ? comp.threshold_db : 0, now);
      strip.compressor.ratio.setValueAtTime(comp.enabled ? Math.min(20, comp.ratio) : 1, now);
      strip.compressor.attack.setValueAtTime(comp.attack_ms / 1000, now);
      strip.compressor.release.setValueAtTime(Math.min(1, comp.release_ms / 1000), now);
      strip.compressor.knee.setValueAtTime(2.5, now);
      strip.makeup.gain.setTargetAtTime(comp.enabled ? db(comp.makeup_db) : 1, now, 0.02);
      strip.fader.gain.setTargetAtTime(audible.has(role) ? db(t.volume_db + t.gain_db) : 0, now, 0.03);
      strip.left.gain.setTargetAtTime(Math.min(1, 1 - t.pan), now, 0.02);
      strip.right.gain.setTargetAtTime(Math.min(1, 1 + t.pan), now, 0.02);
    });
    if (master.current) {
      master.current.gain.gain.setTargetAtTime(db(settings.master.gain_db), now, 0.03);
      const lim = settings.master.limiter;
      master.current.limiter.threshold.setValueAtTime(lim.enabled ? lim.ceiling_db : 0, now);
      master.current.limiter.ratio.setValueAtTime(lim.enabled ? 20 : 1, now);
      master.current.limiter.knee.setValueAtTime(0, now);
      master.current.limiter.attack.setValueAtTime(0.003, now);
      master.current.limiter.release.setValueAtTime(0.06, now);
    }
    const video = videoRef.current;
    if (video) video.muted = mode === "mix"; // A/B: "original" plays the film's own audio
  }, [settings, target, mode, tracks, videoRef, state.ready]);

  // Follow the video clock.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || mode === "off" || !state.ready) return;
    let active = true;
    let corrections = 0;
    let maxDrift = 0;
    const each = (fn: (element: HTMLAudioElement) => void) => strips.current.forEach((strip) => fn(strip.element));
    const seekAll = () =>
      each((element) => {
        try {
          element.currentTime = video.currentTime;
        } catch {
          /* not loaded yet */
        }
      });
    const play = () => {
      void ctxRef.current?.resume();
      seekAll();
      each((element) => {
        element.playbackRate = video.playbackRate || 1;
        void element.play().catch(() => setState((value) => ({ ...value, error: "Press play again after the tracks load." })));
      });
    };
    const pause = () => each((element) => element.pause());
    const onSeek = () => {
      seekAll();
      if (!video.paused) play();
    };
    video.addEventListener("play", play);
    video.addEventListener("pause", pause);
    video.addEventListener("seeked", onSeek);
    video.addEventListener("ratechange", play);
    if (!video.paused) play();
    const timer = window.setInterval(() => {
      if (!active || video.paused) return;
      const rate = video.playbackRate || 1;
      each((element) => {
        if (element.ended || element.readyState < 2) return;
        const drift = element.currentTime - video.currentTime;
        maxDrift = Math.max(maxDrift, Math.abs(drift));
        if (Math.abs(drift) > HARD_SYNC_S) {
          element.currentTime = video.currentTime;
          element.playbackRate = rate;
          corrections += 1;
        } else if (Math.abs(drift) > SOFT_SYNC_S) {
          element.playbackRate = rate * (1 - Math.max(-0.05, Math.min(0.05, drift)));
        } else if (element.playbackRate !== rate) {
          element.playbackRate = rate;
        }
        if (element.paused) void element.play().catch(() => undefined);
      });
      // Capture now: the state updater runs later, after maxDrift is reset.
      const driftMs = Math.round(maxDrift * 1000);
      const hard = corrections;
      setState((value) => ({ ...value, corrections: hard, maxDriftMs: driftMs }));
      maxDrift = 0;
    }, 250);
    return () => {
      active = false;
      window.clearInterval(timer);
      video.removeEventListener("play", play);
      video.removeEventListener("pause", pause);
      video.removeEventListener("seeked", onSeek);
      video.removeEventListener("ratechange", play);
      pause();
      video.muted = false;
    };
  }, [videoRef, mode, state.ready]);

  useEffect(
    () => () => {
      void ctxRef.current?.close().catch(() => undefined);
      ctxRef.current = null;
    },
    [],
  );

  return { ...state, enable: () => void ctxRef.current?.resume() };
}
