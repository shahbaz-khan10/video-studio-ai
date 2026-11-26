import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import type {
  Character,
  CharacterVoiceProfileOut,
  VoiceCatalogItem,
  VoiceCharacteristics,
  VoiceEngineParams,
  VoiceLanguage,
  VoicePresetOut,
  VoicePreviewRequest,
  VoiceProfileBundleOut,
  VoiceProfileUpdate,
  VoiceReferenceOut,
} from "../types";
import { ErrorState, LoadingState } from "./States";

interface VoiceProfilePanelProps {
  projectId: string;
  videoId: string;
}

const PREVIEW_TEXT: Record<VoiceLanguage, string> = {
  ur: "السلام علیکم، یہ میری آواز کا نمونہ ہے۔",
  hi: "नमस्ते, यह मेरी आवाज़ का नमूना है।",
};

const DEFAULT_CHARACTERISTICS: VoiceCharacteristics = {
  gender: "unknown",
  age: "adult",
  pitch: "medium",
  tone: "neutral",
  speed: 1,
  accent: null,
  intensity: 1,
  emotion: "neutral",
};

const EMPTY_PARAMS: VoiceEngineParams = {};

function round(value: number, digits = 3): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function draftFromProfile(profile: CharacterVoiceProfileOut): VoiceProfileUpdate {
  return {
    language: (profile.language === "hi" ? "hi" : "ur") as VoiceLanguage,
    engine: profile.engine,
    voice_id: profile.voice_id,
    model_id: profile.model_id,
    characteristics: { ...DEFAULT_CHARACTERISTICS, ...profile.characteristics },
    params: { ...EMPTY_PARAMS, ...profile.params },
    preset_id: profile.preset_id,
    reference_id: profile.reference_id,
    notes: profile.notes,
    locked: profile.locked,
  };
}

function defaultDraft(language: VoiceLanguage, engine: string, voiceId: string | null): VoiceProfileUpdate {
  return {
    language,
    engine,
    voice_id: voiceId,
    characteristics: { ...DEFAULT_CHARACTERISTICS },
    params: { ...EMPTY_PARAMS },
    notes: null,
    locked: false,
  };
}

function draftSignature(draft: VoiceProfileUpdate | null): string {
  return JSON.stringify(draft ?? null);
}

function profileLabel(profile: CharacterVoiceProfileOut, characters: Character[]): string {
  const name = characters.find((item) => item.id === profile.character_id)?.name ?? "Unassigned";
  return `${name} · ${profile.language.toUpperCase()} · v${profile.version}${profile.active ? " · active" : ""}`;
}

function characteristicSummary(profile: CharacterVoiceProfileOut | null): string {
  if (!profile) return "No saved profile yet";
  const values = profile.characteristics;
  const parts = [
    values.gender !== "unknown" ? values.gender : "gender n/a",
    values.age,
    `pitch ${values.pitch}`,
    `tone ${values.tone}`,
    `${round(values.speed, 2)}× speed`,
  ];
  if (values.accent) parts.push(values.accent);
  parts.push(`intensity ${round(values.intensity, 2)}`, values.emotion);
  return parts.join(" · ");
}

function messageOf(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

export function VoiceProfilePanel({ projectId, videoId }: VoiceProfilePanelProps) {
  const [bundle, setBundle] = useState<VoiceProfileBundleOut | null>(null);
  const [catalog, setCatalog] = useState<VoiceCatalogItem[]>([]);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [avatars, setAvatars] = useState<Record<string, string>>({});
  const [selectedCharacterId, setSelectedCharacterId] = useState("");
  const [language, setLanguage] = useState<VoiceLanguage>("ur");
  const [draft, setDraft] = useState<VoiceProfileUpdate | null>(null);
  const [history, setHistory] = useState<CharacterVoiceProfileOut[]>([]);
  const [previewText, setPreviewText] = useState(PREVIEW_TEXT.ur);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [compareLeft, setCompareLeft] = useState("");
  const [compareRight, setCompareRight] = useState("");
  const [compareUrls, setCompareUrls] = useState<{ left: string | null; right: string | null }>({ left: null, right: null });
  const [sampleFile, setSampleFile] = useState<File | null>(null);
  const [consentChecked, setConsentChecked] = useState(false);
  const [presetName, setPresetName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadVersion, setLoadVersion] = useState(0);
  const objectUrls = useRef<Set<string>>(new Set());
  const savedSignature = useRef<string>("null");

  const trackUrl = useCallback((blob: Blob): string => {
    const url = URL.createObjectURL(blob);
    objectUrls.current.add(url);
    return url;
  }, []);

  const dropUrl = useCallback((url: string | null) => {
    if (!url || !objectUrls.current.has(url)) return;
    URL.revokeObjectURL(url);
    objectUrls.current.delete(url);
  }, []);

  useEffect(
    () => () => {
      objectUrls.current.forEach((url) => URL.revokeObjectURL(url));
      objectUrls.current.clear();
    },
    [],
  );

  const reload = useCallback(() => setLoadVersion((value) => value + 1), []);

  useEffect(() => {
    let active = true;
    setError(null);
    Promise.all([
      api.getProjectVoiceProfiles(projectId),
      api.getVoiceCatalog(),
      api.getCharacterBundle(videoId),
      api.listProjectCharacters(projectId),
    ])
      .then(([voiceBundle, voiceCatalog, characterBundle, roster]) => {
        if (!active) return;
        setBundle(voiceBundle);
        setCatalog(voiceCatalog);
        const merged = new Map<string, Character>();
        characterBundle.characters.forEach((item) => merged.set(item.id, item));
        roster.forEach((item) => merged.set(item.id, item));
        setCharacters([...merged.values()]);
        const representative: Record<string, string> = {};
        characterBundle.face_tracks.forEach((track) => {
          if (track.character_id && track.representative_url && !representative[track.character_id]) {
            representative[track.character_id] = track.representative_url;
          }
        });
        setAvatars(representative);
        setSelectedCharacterId((current) => current || characterBundle.characters[0]?.id || roster[0]?.id || "");
      })
      .catch((reason: unknown) => {
        if (active) setError(messageOf(reason, "Could not load voice profiles."));
      });
    return () => {
      active = false;
    };
  }, [projectId, videoId, loadVersion]);

  const activeProfile = useMemo(
    () =>
      bundle?.profiles.find(
        (item) => item.active && item.character_id === selectedCharacterId && item.language === language,
      ) ?? null,
    [bundle, selectedCharacterId, language],
  );

  const voiceOptions = useMemo(
    () => bundle?.engine.voices.filter((voice) => voice.language === language) ?? [],
    [bundle, language],
  );

  const dirty = useMemo(() => draftSignature(draft) !== savedSignature.current, [draft]);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    dropUrl(previewUrl);
    setPreviewUrl(null);
    dropUrl(compareUrls.left);
    dropUrl(compareUrls.right);
    setCompareUrls({ left: null, right: null });
    setPreviewText(PREVIEW_TEXT[language]);
    setSampleFile(null);
    setConsentChecked(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCharacterId, language]);

  useEffect(() => {
    if (!bundle || !selectedCharacterId) {
      setDraft(null);
      setHistory([]);
      savedSignature.current = "null";
      return;
    }
    const next = activeProfile
      ? draftFromProfile(activeProfile)
      : defaultDraft(
          language,
          bundle.engine.selected === "none" ? "piper" : bundle.engine.selected,
          voiceOptions[0]?.id ?? null,
        );
    setDraft(next);
    savedSignature.current = draftSignature(next);
    let active = true;
    api
      .listCharacterVoiceProfiles(selectedCharacterId, language)
      .then((rows) => {
        if (active) setHistory(rows);
      })
      .catch(() => {
        if (active) setHistory([]);
      });
    return () => {
      active = false;
    };
  }, [bundle, selectedCharacterId, language, activeProfile, voiceOptions]);

  useEffect(() => {
    if (!bundle || bundle.profiles.length === 0) return;
    setCompareLeft((current) => current || bundle.profiles[0]?.id || "");
    setCompareRight((current) => current || bundle.profiles[1]?.id || bundle.profiles[0]?.id || "");
  }, [bundle]);

  const controls = useMemo(() => {
    const base =
      activeProfile?.supported_controls ??
      bundle?.engine.capabilities.filter((item) => item.supported).map((item) => item.key) ??
      [];
    const result = new Set(base);
    const selectedVoice = voiceOptions.find((voice) => voice.id === draft?.voice_id);
    if (selectedVoice && selectedVoice.supports_speaker_id === false) result.delete("speaker_id");
    return result;
  }, [activeProfile, bundle, draft?.voice_id, voiceOptions]);

  const supports = useCallback(
    (key: string) => key === "voice" || key === "language" || controls.has(key),
    [controls],
  );

  const references = useMemo(
    () =>
      bundle?.references.filter(
        (item) => item.character_id === selectedCharacterId && item.language === language,
      ) ?? [],
    [bundle, selectedCharacterId, language],
  );

  const presets = useMemo(
    () => bundle?.presets.filter((preset) => preset.language === language) ?? [],
    [bundle, language],
  );

  const catalogForLanguage = useMemo(
    () => catalog.filter((voice) => voice.language === language).slice(0, 8),
    [catalog, language],
  );

  const patchDraft = useCallback((patch: Partial<VoiceProfileUpdate>) => {
    setDraft((current) => (current ? { ...current, ...patch } : current));
  }, []);

  const patchCharacteristics = useCallback((patch: Partial<VoiceCharacteristics>) => {
    setDraft((current) =>
      current
        ? { ...current, characteristics: { ...current.characteristics, ...patch } }
        : current,
    );
  }, []);

  const patchParams = useCallback((patch: Partial<VoiceEngineParams>) => {
    setDraft((current) => (current ? { ...current, params: { ...current.params, ...patch } } : current));
  }, []);

  const setSpeed = useCallback((value: number) => {
    setDraft((current) =>
      current
        ? {
            ...current,
            characteristics: { ...current.characteristics, speed: value },
            params: { ...current.params, length_scale: round(1 / value) },
          }
        : current,
    );
  }, []);

  const runAction = useCallback(
    async (key: string, action: () => Promise<void>) => {
      setBusy(key);
      setError(null);
      try {
        await action();
      } catch (reason: unknown) {
        setError(messageOf(reason, "That voice action failed."));
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const save = useCallback(() => {
    if (!draft || !selectedCharacterId || busy) return;
    void runAction("save", async () => {
      await api.saveVoiceProfile(selectedCharacterId, draft);
      reload();
    });
  }, [busy, draft, reload, runAction, selectedCharacterId]);

  const reset = useCallback(() => {
    if (!selectedCharacterId || busy) return;
    if (!window.confirm("Reset this character's voice profile for the selected language? Older versions stay in history.")) {
      return;
    }
    void runAction("reset", async () => {
      await api.resetVoiceProfile(selectedCharacterId, language);
      reload();
    });
  }, [busy, language, reload, runAction, selectedCharacterId]);

  const toggleLock = useCallback(() => {
    if (!activeProfile || !selectedCharacterId || busy) return;
    void runAction("lock", async () => {
      await api.saveVoiceProfile(selectedCharacterId, {
        language,
        locked: !activeProfile.locked,
      });
      reload();
    });
  }, [activeProfile, busy, language, reload, runAction, selectedCharacterId]);

  const activate = useCallback(
    (profileId: string) => {
      if (busy) return;
      void runAction(`activate:${profileId}`, async () => {
        await api.activateVoiceProfile(profileId);
        reload();
      });
    },
    [busy, reload, runAction],
  );

  const savePreset = useCallback(() => {
    if (!draft || !presetName.trim() || busy) return;
    void runAction("preset", async () => {
      await api.saveVoicePreset(projectId, {
        name: presetName.trim(),
        language,
        engine: draft.engine ?? "piper",
        model_id: draft.model_id ?? null,
        voice_id: draft.voice_id ?? null,
        characteristics: draft.characteristics,
        params: draft.params,
      });
      setPresetName("");
      reload();
    });
  }, [busy, draft, language, presetName, projectId, reload, runAction]);

  const applyPreset = useCallback(
    (preset: VoicePresetOut) => {
      setDraft((current) =>
        current
          ? {
              ...current,
              engine: preset.engine,
              model_id: preset.model_id,
              voice_id: preset.voice_id,
              characteristics: { ...DEFAULT_CHARACTERISTICS, ...preset.characteristics },
              params: { ...EMPTY_PARAMS, ...preset.params },
              preset_id: preset.id,
            }
          : current,
      );
    },
    [],
  );

  const deletePreset = useCallback(
    (presetId: string) => {
      if (!window.confirm("Delete this saved voice preset?")) return;
      void runAction(`preset:${presetId}`, async () => {
        await api.deleteVoicePreset(presetId);
        reload();
      });
    },
    [busy, reload, runAction],
  );

  const uploadSample = useCallback(() => {
    if (!selectedCharacterId || !sampleFile || busy) return;
    if (!consentChecked) {
      setError("Confirm ownership or the speaker's permission before uploading a voice sample.");
      return;
    }
    void runAction("sample", async () => {
      await api.uploadVoiceReference(
        selectedCharacterId,
        sampleFile,
        language,
        true,
        bundle?.consent_policy.version ?? "2026-11-26",
      );
      setSampleFile(null);
      setConsentChecked(false);
      reload();
    });
  }, [busy, bundle, consentChecked, language, reload, runAction, sampleFile, selectedCharacterId]);

  const deleteSample = useCallback(
    (reference: VoiceReferenceOut) => {
      if (!window.confirm(`Delete ${reference.filename}? This cannot be undone.`)) return;
      void runAction(`sample:${reference.id}`, async () => {
        await api.deleteVoiceReference(reference.id);
        reload();
      });
    },
    [busy, reload, runAction],
  );

  const revokeConsent = useCallback(() => {
    if (!selectedCharacterId || busy) return;
    if (!window.confirm("Revoke permission for this character's voice samples? Future use will be disabled.")) return;
    void runAction("consent", async () => {
      await api.updateVoiceConsent(
        selectedCharacterId,
        language,
        "revoked",
        false,
        bundle?.consent_policy.version ?? "2026-11-26",
      );
      reload();
    });
  }, [busy, bundle, language, reload, runAction, selectedCharacterId]);

  const preview = useCallback(() => {
    if (!previewText.trim() || busy) return;
    void runAction("preview", async () => {
      const payload: VoicePreviewRequest = {
        character_id: selectedCharacterId || undefined,
        language,
        text: previewText,
        profile: draft ?? undefined,
      };
      const blob = await api.previewVoice(payload);
      dropUrl(previewUrl);
      setPreviewUrl(trackUrl(blob));
    });
  }, [busy, draft, dropUrl, language, previewText, runAction, selectedCharacterId, trackUrl, previewUrl]);

  const previewCompare = useCallback(() => {
    if (!compareLeft || !compareRight || busy) return;
    void runAction("compare", async () => {
      const left = bundle?.profiles.find((item) => item.id === compareLeft);
      const right = bundle?.profiles.find((item) => item.id === compareRight);
      if (!left || !right) return;
      const leftLanguage = left.language === "hi" ? "hi" : "ur";
      const rightLanguage = right.language === "hi" ? "hi" : "ur";
      const textFor = (itemLanguage: VoiceLanguage) =>
        itemLanguage === language && previewText.trim() ? previewText : PREVIEW_TEXT[itemLanguage];
      const [leftBlob, rightBlob] = await Promise.all([
        api.previewVoice({ profile_id: left.id, language: leftLanguage, text: textFor(leftLanguage) }),
        api.previewVoice({ profile_id: right.id, language: rightLanguage, text: textFor(rightLanguage) }),
      ]);
      dropUrl(compareUrls.left);
      dropUrl(compareUrls.right);
      setCompareUrls({ left: trackUrl(leftBlob), right: trackUrl(rightBlob) });
    });
  }, [busy, bundle, compareLeft, compareRight, compareUrls, dropUrl, language, previewText, runAction, trackUrl]);

  if (loadingFromBundle(bundle, characters)) return <LoadingState label="Loading voice profiles…" />;
  if (!bundle) return <ErrorState message={error ?? "Voice profile data is unavailable."} onRetry={reload} />;

  const selectedCharacter = characters.find((item) => item.id === selectedCharacterId) ?? null;
  const voice = voiceOptions.find((item) => item.id === draft?.voice_id) ?? null;
  const sampleAccept = bundle.sample_limits.formats.map((format) => `.${format.replace(".", "")}`).join(",");

  return (
    <div className="voice-manager">
      {error && <div className="voice-alert" role="alert">{error}</div>}

      <div className="voice-manager-head">
        <div className="voice-tabs" role="tablist" aria-label="Character">
          {characters.length === 0 && <span className="voice-empty">Detect characters first to assign voices.</span>}
          {characters.map((character) => {
            const assigned = bundle.profiles.find(
              (item) => item.active && item.character_id === character.id && item.language === language,
            );
            return (
              <button
                type="button"
                role="tab"
                aria-selected={selectedCharacterId === character.id}
                className={`voice-tab${selectedCharacterId === character.id ? " active" : ""}`}
                key={character.id}
                onClick={() => setSelectedCharacterId(character.id)}
              >
                {avatars[character.id] ? (
                  <img src={api.assetUrl(avatars[character.id])} alt="" />
                ) : (
                  <span>{character.name.slice(0, 2).toUpperCase()}</span>
                )}
                <strong>{character.name}</strong>
                <small>{assigned?.voice_label ?? "No voice"}</small>
              </button>
            );
          })}
        </div>
        <div className="voice-language-switch" aria-label="Voice language">
          {(["ur", "hi"] as VoiceLanguage[]).map((item) => (
            <button
              type="button"
              key={item}
              className={language === item ? "active" : ""}
              onClick={() => setLanguage(item)}
            >
              {item === "ur" ? "Urdu" : "Hindi"}
            </button>
          ))}
        </div>
      </div>

      <div className="voice-manager-grid">
        <section className="voice-column">
          <div className="voice-column-head">
            <div>
              <h3>{selectedCharacter?.name ?? "No character selected"}</h3>
              <p>{characteristicSummary(activeProfile)}</p>
            </div>
            <div className="voice-column-actions">
              <span className={`badge badge-${activeProfile?.usable === false ? "revoked" : "ready"}`}>
                {activeProfile ? `v${activeProfile.version}` : "new"}
              </span>
              {activeProfile && (
                <button type="button" className="button button-quiet" onClick={toggleLock} disabled={!!busy}>
                  {activeProfile.locked ? "Unlock" : "Lock"}
                </button>
              )}
              <button type="button" className="button button-quiet" onClick={reset} disabled={!selectedCharacterId || !!busy}>
                Reset
              </button>
            </div>
          </div>

          {!draft ? (
            <p className="voice-empty">Select a character to configure its {language === "ur" ? "Urdu" : "Hindi"} voice.</p>
          ) : (
            <>
              <div className="voice-field">
                <label htmlFor="voice-engine">Engine</label>
                <select id="voice-engine" value={draft.engine ?? "piper"} disabled>
                  <option value={draft.engine ?? "piper"}>{bundle.engine.label}</option>
                </select>
                <small>
                  {bundle.engine.license} · {bundle.engine.available ? "available" : "metadata only"}
                </small>
              </div>

              <div className="voice-field">
                <label htmlFor="voice-id">Voice</label>
                <select
                  id="voice-id"
                  value={draft.voice_id ?? ""}
                  onChange={(event) => patchDraft({ voice_id: event.target.value || null, preset_id: null })}
                  disabled={!!activeProfile?.locked || voiceOptions.length === 0}
                >
                  <option value="">{voiceOptions.length ? "Choose a voice" : "No configured voice"}</option>
                  {voiceOptions.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label} · {option.gender} · {option.license}
                    </option>
                  ))}
                </select>
                <small>
                  {voice
                    ? `${voice.sample_rate ?? "?"} Hz · ${voice.cached ? "cached" : "not downloaded"} · ${
                        voice.preview_available ? "real preview available" : "preview unavailable"
                      }`
                    : "Configure VOICE_PIPER_MODELS_DIR (or a self-hosted worker) to enable real previews."}
                </small>
              </div>

              <div className="voice-control-grid">
                {supports("gender") && (
                  <label className="voice-field">
                    <span>Gender presentation</span>
                    <select
                      value={draft.characteristics.gender}
                      onChange={(event) =>
                        patchCharacteristics({ gender: event.target.value as VoiceCharacteristics["gender"] })
                      }
                      disabled={!!activeProfile?.locked}
                    >
                      <option value="unknown">Unknown</option>
                      <option value="male">Male</option>
                      <option value="female">Female</option>
                    </select>
                  </label>
                )}
                {supports("age") && (
                  <label className="voice-field">
                    <span>Age</span>
                    <select
                      value={draft.characteristics.age}
                      onChange={(event) =>
                        patchCharacteristics({ age: event.target.value as VoiceCharacteristics["age"] })
                      }
                      disabled={!!activeProfile?.locked}
                    >
                      <option value="young">Young</option>
                      <option value="adult">Adult</option>
                      <option value="mature">Mature</option>
                    </select>
                  </label>
                )}
                {supports("pitch") && (
                  <label className="voice-field">
                    <span>Pitch</span>
                    <select
                      value={draft.characteristics.pitch}
                      onChange={(event) =>
                        patchCharacteristics({ pitch: event.target.value as VoiceCharacteristics["pitch"] })
                      }
                      disabled={!!activeProfile?.locked}
                    >
                      <option value="low">Low</option>
                      <option value="medium">Medium</option>
                      <option value="high">High</option>
                    </select>
                  </label>
                )}
                {supports("tone") && (
                  <label className="voice-field">
                    <span>Tone</span>
                    <select
                      value={draft.characteristics.tone}
                      onChange={(event) =>
                        patchCharacteristics({ tone: event.target.value as VoiceCharacteristics["tone"] })
                      }
                      disabled={!!activeProfile?.locked}
                    >
                      <option value="soft">Soft</option>
                      <option value="deep">Deep</option>
                      <option value="heavy">Heavy</option>
                      <option value="light">Light</option>
                      <option value="neutral">Neutral</option>
                    </select>
                  </label>
                )}
                {supports("speed") && (
                  <label className="voice-field">
                    <span>Speed · {round(draft.characteristics.speed, 2)}×</span>
                    <input
                      type="range"
                      min="0.5"
                      max="2.5"
                      step="0.05"
                      value={draft.characteristics.speed}
                      onChange={(event) => setSpeed(Number(event.target.value))}
                      disabled={!!activeProfile?.locked}
                    />
                  </label>
                )}
                {supports("intensity") && (
                  <label className="voice-field">
                    <span>Intensity · {round(draft.characteristics.intensity, 2)}</span>
                    <input
                      type="range"
                      min="0"
                      max="2"
                      step="0.05"
                      value={draft.characteristics.intensity}
                      onChange={(event) => patchCharacteristics({ intensity: Number(event.target.value) })}
                      disabled={!!activeProfile?.locked}
                    />
                  </label>
                )}
                {supports("accent") && (
                  <label className="voice-field">
                    <span>Accent</span>
                    <input
                      type="text"
                      maxLength={64}
                      value={draft.characteristics.accent ?? ""}
                      onChange={(event) => patchCharacteristics({ accent: event.target.value || null })}
                      disabled={!!activeProfile?.locked}
                    />
                  </label>
                )}
                {supports("emotion") && (
                  <label className="voice-field">
                    <span>Emotional style</span>
                    <select
                      value={draft.characteristics.emotion}
                      onChange={(event) =>
                        patchCharacteristics({ emotion: event.target.value as VoiceCharacteristics["emotion"] })
                      }
                      disabled={!!activeProfile?.locked}
                    >
                      {["neutral", "calm", "happy", "sad", "angry", "tense", "warm", "serious"].map((item) => (
                        <option key={item} value={item}>{item}</option>
                      ))}
                    </select>
                  </label>
                )}
                {supports("variability") && (
                  <label className="voice-field">
                    <span>Variability · {round(draft.params.noise_scale ?? 0.667, 3)}</span>
                    <input
                      type="range"
                      min="0"
                      max="1.5"
                      step="0.01"
                      value={draft.params.noise_scale ?? 0.667}
                      onChange={(event) => patchParams({ noise_scale: Number(event.target.value) })}
                      disabled={!!activeProfile?.locked}
                    />
                  </label>
                )}
                {supports("cadence") && (
                  <label className="voice-field">
                    <span>Cadence · {round(draft.params.noise_w_scale ?? 0.8, 3)}</span>
                    <input
                      type="range"
                      min="0"
                      max="1.5"
                      step="0.01"
                      value={draft.params.noise_w_scale ?? 0.8}
                      onChange={(event) => patchParams({ noise_w_scale: Number(event.target.value) })}
                      disabled={!!activeProfile?.locked}
                    />
                  </label>
                )}
                {supports("speaker_id") && voice?.supports_speaker_id !== false && (
                  <label className="voice-field">
                    <span>Speaker / take</span>
                    <input
                      type="number"
                      min="0"
                      max="256"
                      value={draft.params.speaker_id ?? 0}
                      onChange={(event) => patchParams({ speaker_id: Number(event.target.value) })}
                      disabled={!!activeProfile?.locked}
                    />
                  </label>
                )}
                {supports("volume") && (
                  <label className="voice-field">
                    <span>Volume · {round(draft.params.volume ?? 1, 2)}</span>
                    <input
                      type="range"
                      min="0"
                      max="2"
                      step="0.05"
                      value={draft.params.volume ?? 1}
                      onChange={(event) => patchParams({ volume: Number(event.target.value) })}
                      disabled={!!activeProfile?.locked}
                    />
                  </label>
                )}
                {supports("seed") && (
                  <label className="voice-field">
                    <span>Deterministic seed</span>
                    <input
                      type="number"
                      min="0"
                      value={draft.params.seed ?? ""}
                      onChange={(event) =>
                        patchParams({ seed: event.target.value ? Number(event.target.value) : null })
                      }
                      disabled={!!activeProfile?.locked}
                    />
                  </label>
                )}
              </div>

              {activeProfile?.ignored_controls.length ? (
                <p className="voice-note">
                  Not used by this engine: {activeProfile.ignored_controls.join(", ")}.
                </p>
              ) : null}

              <label className="voice-field">
                <span>Notes</span>
                <textarea
                  rows={2}
                  maxLength={2000}
                  value={draft.notes ?? ""}
                  onChange={(event) => patchDraft({ notes: event.target.value || null })}
                  disabled={!!activeProfile?.locked}
                />
              </label>

              <div className="voice-save-row">
                <button type="button" className="button button-primary" onClick={save} disabled={!dirty || !!busy}>
                  {busy === "save" ? "Saving…" : "Save voice profile"}
                </button>
                <span>{dirty ? "Unsaved changes" : activeProfile ? "Saved" : "Not saved yet"}</span>
              </div>
            </>
          )}
        </section>

        <section className="voice-column">
          <div className="voice-column-head">
            <div>
              <h3>Preview & compare</h3>
              <p>Real engine output only. No audio is fabricated.</p>
            </div>
          </div>

          <label className="voice-field">
            <span>Short sample sentence</span>
            <textarea
              rows={2}
              maxLength={240}
              value={previewText}
              onChange={(event) => setPreviewText(event.target.value)}
            />
          </label>
          <div className="voice-preview-row">
            <button type="button" className="button button-quiet" onClick={preview} disabled={!draft || !!busy}>
              {busy === "preview" ? "Synthesising…" : "Preview draft voice"}
            </button>
            {previewUrl && <audio controls src={previewUrl} aria-label="Voice preview" />}
          </div>
          {!bundle.engine.available && (
            <p className="voice-note">
              {bundle.engine.selected === "none"
                ? "Set VOICE_BACKEND=piper with a voice directory, or configure a self-hosted worker, to hear a preview."
                : bundle.engine.notes}
            </p>
          )}

          <div className="voice-compare">
            <h4>Compare two saved profiles</h4>
            <div className="voice-compare-selects">
              <select value={compareLeft} onChange={(event) => setCompareLeft(event.target.value)}>
                <option value="">First profile</option>
                {bundle.profiles.map((item) => (
                  <option key={item.id} value={item.id}>{profileLabel(item, characters)}</option>
                ))}
              </select>
              <select value={compareRight} onChange={(event) => setCompareRight(event.target.value)}>
                <option value="">Second profile</option>
                {bundle.profiles.map((item) => (
                  <option key={item.id} value={item.id}>{profileLabel(item, characters)}</option>
                ))}
              </select>
              <button type="button" className="button button-quiet" onClick={previewCompare} disabled={!!busy}>
                {busy === "compare" ? "Rendering…" : "Compare"}
              </button>
            </div>
            {(compareUrls.left || compareUrls.right) && (
              <div className="voice-compare-audio">
                <div><span>A</span>{compareUrls.left && <audio controls src={compareUrls.left} />}</div>
                <div><span>B</span>{compareUrls.right && <audio controls src={compareUrls.right} />}</div>
              </div>
            )}
          </div>

          <div className="voice-presets">
            <h4>Saved presets</h4>
            <div className="voice-preset-save">
              <input
                type="text"
                placeholder="Preset name"
                maxLength={120}
                value={presetName}
                onChange={(event) => setPresetName(event.target.value)}
              />
              <button type="button" className="button button-quiet" onClick={savePreset} disabled={!presetName.trim() || !draft || !!busy}>
                Save current
              </button>
            </div>
            {presets.length === 0 ? (
              <p className="voice-empty">No presets for {language === "ur" ? "Urdu" : "Hindi"} yet.</p>
            ) : (
              <ul className="voice-preset-list">
                {presets.map((preset) => (
                  <li key={preset.id}>
                    <div>
                      <strong>{preset.name}</strong>
                      <small>{preset.voice_id ?? "no voice"} · {preset.engine}</small>
                    </div>
                    <button type="button" className="button button-quiet" onClick={() => applyPreset(preset)}>Apply</button>
                    <button type="button" className="button button-quiet" onClick={() => deletePreset(preset.id)}>Delete</button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>

        <section className="voice-column voice-column-wide">
          <div className="voice-column-head">
            <div>
              <h3>Custom voice sample</h3>
              <p>
                {bundle.sample_limits.formats.join(", ")} · {bundle.sample_limits.min_s}–{bundle.sample_limits.max_s}s · max{" "}
                {bundle.sample_limits.max_mb} MB. Decoded and validated on the backend.
              </p>
            </div>
          </div>
          <p className="voice-consent">{bundle.consent_policy.statement}</p>
          <div className="voice-sample-row">
            <input
              type="file"
              accept={`audio/*,${sampleAccept}`}
              onChange={(event) => setSampleFile(event.target.files?.[0] ?? null)}
              disabled={!!activeProfile?.locked || !!busy}
            />
            <label className="voice-consent-check">
              <input
                type="checkbox"
                checked={consentChecked}
                onChange={(event) => setConsentChecked(event.target.checked)}
              />
              <span>I confirm ownership or explicit permission (policy {bundle.consent_policy.version}).</span>
            </label>
            <button type="button" className="button button-quiet" onClick={uploadSample} disabled={!sampleFile || !consentChecked || !!busy}>
              {busy === "sample" ? "Uploading…" : "Upload & associate"}
            </button>
          </div>
          {references.length === 0 ? (
            <p className="voice-empty">No custom sample for this character and language.</p>
          ) : (
            <ul className="voice-reference-list">
              {references.map((reference) => (
                <li key={reference.id}>
                  <div>
                    <strong>{reference.filename}</strong>
                    <small>
                      {reference.duration_ms ? `${round(reference.duration_ms / 1000, 1)}s · ` : ""}
                      {reference.sample_rate ?? "?"} Hz · {reference.consent_status}
                    </small>
                  </div>
                  <audio controls src={api.assetUrl(reference.url)} aria-label={`Sample ${reference.filename}`} />
                  <button type="button" className="button button-quiet" onClick={revokeConsent} disabled={reference.consent_status === "revoked" || !!busy}>
                    Revoke
                  </button>
                  <button type="button" className="button button-quiet" onClick={() => deleteSample(reference)} disabled={!!busy}>
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="voice-column voice-column-wide">
          <div className="voice-column-head">
            <div>
              <h3>Version history</h3>
              <p>Older versions are kept; activating one never mutates it.</p>
            </div>
          </div>
          {history.length === 0 ? (
            <p className="voice-empty">No saved versions for this language yet.</p>
          ) : (
            <ol className="voice-history">
              {history.map((item) => (
                <li key={item.id} className={item.active ? "active" : ""}>
                  <span>v{item.version}</span>
                  <strong>{item.voice_label ?? "No voice"}</strong>
                  <small>
                    {item.engine_label} · {item.characteristics.gender} · {item.characteristics.speed}× · {item.notes ?? "—"}
                  </small>
                  {item.active ? (
                    <span className="badge badge-ready">active</span>
                  ) : (
                    <button type="button" className="button button-quiet" onClick={() => activate(item.id)} disabled={!!busy || !item.usable}>
                      Activate
                    </button>
                  )}
                </li>
              ))}
            </ol>
          )}
        </section>
      </div>

      <div className="voice-manager-footer">
        {catalogForLanguage.length > 0 && (
          <details>
            <summary>Documented {language === "ur" ? "Urdu" : "Hindi"} voices (not downloaded automatically)</summary>
            <ul>
              {catalogForLanguage.map((item) => (
                <li key={item.id}>
                  <strong>{item.id}</strong> · {item.gender} · {item.license}
                </li>
              ))}
            </ul>
          </details>
        )}
        <ul>{bundle.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
      </div>
    </div>
  );
}

function loadingFromBundle(bundle: VoiceProfileBundleOut | null, characters: Character[]): boolean {
  return !bundle && characters.length === 0;
}
