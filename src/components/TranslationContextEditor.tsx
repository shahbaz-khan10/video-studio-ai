import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { CharacterRelationshipItem, GlossaryTerm, SpeechRegister, TranslationProfile } from "../types";

interface Props {
  projectId: string;
  profiles: TranslationProfile[];
  relationships: CharacterRelationshipItem[];
  glossary: GlossaryTerm[];
  onChanged: (next: {
    profiles?: TranslationProfile[];
    relationships?: CharacterRelationshipItem[];
    glossary?: GlossaryTerm[];
  }) => void;
}

const REGISTERS: { value: SpeechRegister; label: string }[] = [
  { value: "auto", label: "Auto (from context)" },
  { value: "formal", label: "Formal · aap / آپ / आप" },
  { value: "neutral", label: "Neutral (model default)" },
  { value: "informal", label: "Informal · tum / تم / तुम" },
  { value: "intimate", label: "Intimate · tu / تو / तू" },
];

const ADDRESS = [
  { value: "formal", label: "aap (formal)" },
  { value: "informal", label: "tum (informal)" },
  { value: "intimate", label: "tu (intimate)" },
] as const;

function message(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback;
}

export function TranslationContextEditor({ projectId, profiles, relationships, glossary, onChanged }: Props) {
  const [profileDrafts, setProfileDrafts] = useState<TranslationProfile[]>(profiles);
  const [relDrafts, setRelDrafts] = useState<CharacterRelationshipItem[]>(relationships);
  const [termDrafts, setTermDrafts] = useState<GlossaryTerm[]>(glossary);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setProfileDrafts(profiles), [profiles]);
  useEffect(() => setRelDrafts(relationships), [relationships]);
  useEffect(() => setTermDrafts(glossary), [glossary]);

  const updateProfile = (id: string, patch: Partial<TranslationProfile>) =>
    setProfileDrafts((current) => current.map((item) => (item.character_id === id ? { ...item, ...patch } : item)));

  const saveProfile = async (profile: TranslationProfile) => {
    setError(null);
    try {
      const saved = await api.saveTranslationProfile(profile.character_id, {
        gender: profile.gender,
        register: profile.register,
        speaking_style: profile.speaking_style,
        name_ur: profile.name_ur,
        name_hi: profile.name_hi,
        name_roman: profile.name_roman,
      });
      onChanged({ profiles: profileDrafts.map((item) => (item.character_id === saved.character_id ? saved : item)) });
      setStatus(`Saved ${saved.name}. Re-translate lines to apply it.`);
    } catch (reason) {
      setError(message(reason, "Could not save the character profile."));
    }
  };

  const saveRelationships = async () => {
    setError(null);
    try {
      const saved = await api.saveRelationships(projectId, relDrafts);
      onChanged({ relationships: saved });
      setStatus("Relationships saved.");
    } catch (reason) {
      setError(message(reason, "Could not save relationships."));
    }
  };

  const saveGlossary = async () => {
    setError(null);
    try {
      const saved = await api.saveGlossary(
        projectId,
        termDrafts.filter((term) => term.source.trim()),
      );
      onChanged({ glossary: saved });
      setStatus("Glossary saved.");
    } catch (reason) {
      setError(message(reason, "Could not save the glossary."));
    }
  };

  const nameOf = (id: string) => profileDrafts.find((item) => item.character_id === id)?.name ?? "Unknown";

  return (
    <details className="translation-context">
      <summary>
        Character voices, relationships &amp; glossary
        <small>
          {profiles.length} characters · {relationships.length} relationships · {glossary.length} terms
        </small>
      </summary>
      {status && <p className="translation-notice">{status}</p>}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      <section>
        <h3>Character voices</h3>
        {!profileDrafts.length ? (
          <p className="muted">No characters yet. Run character detection in the Character List panel first.</p>
        ) : (
          <div className="profile-table">
            {profileDrafts.map((profile) => (
              <div className="profile-row" key={profile.character_id}>
                <strong>{profile.name}</strong>
                <label>
                  Gender
                  <select
                    value={profile.gender}
                    onChange={(event) => updateProfile(profile.character_id, { gender: event.target.value as TranslationProfile["gender"] })}
                  >
                    <option value="unknown">Unknown</option>
                    <option value="female">Female</option>
                    <option value="male">Male</option>
                  </select>
                </label>
                <label>
                  Default register
                  <select
                    value={profile.register}
                    onChange={(event) => updateProfile(profile.character_id, { register: event.target.value as SpeechRegister })}
                  >
                    {REGISTERS.map((item) => (
                      <option key={item.value} value={item.value}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="profile-style">
                  Speaking style
                  <input
                    value={profile.speaking_style ?? ""}
                    placeholder="e.g. sarcastic teenager, street slang"
                    onChange={(event) => updateProfile(profile.character_id, { speaking_style: event.target.value })}
                  />
                </label>
                <label>
                  Urdu name
                  <input
                    dir="rtl"
                    lang="ur"
                    className="urdu-text"
                    value={profile.name_ur ?? ""}
                    onChange={(event) => updateProfile(profile.character_id, { name_ur: event.target.value })}
                  />
                </label>
                <label>
                  Hindi name
                  <input
                    lang="hi"
                    value={profile.name_hi ?? ""}
                    onChange={(event) => updateProfile(profile.character_id, { name_hi: event.target.value })}
                  />
                </label>
                <label>
                  Roman
                  <input
                    value={profile.name_roman ?? ""}
                    onChange={(event) => updateProfile(profile.character_id, { name_roman: event.target.value })}
                  />
                </label>
                <div className="profile-save">
                  {profile.names_auto && <small title="Generated automatically — please check">auto names</small>}
                  <button type="button" className="button button-small" onClick={() => void saveProfile(profile)}>
                    Save
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {profileDrafts.length > 1 && (
        <section>
          <h3>How characters address each other</h3>
          <p className="muted">Directed: “Sara → Zainab · aap · daughter to mother”. Used to choose aap / tum / tu.</p>
          {relDrafts.map((rel, index) => (
            <div className="relationship-row" key={`${rel.speaker_character_id}-${rel.listener_character_id}-${index}`}>
              <select
                value={rel.speaker_character_id}
                aria-label="Speaker"
                onChange={(event) =>
                  setRelDrafts((current) => current.map((item, i) => (i === index ? { ...item, speaker_character_id: event.target.value } : item)))
                }
              >
                {profileDrafts.map((item) => (
                  <option key={item.character_id} value={item.character_id}>
                    {item.name}
                  </option>
                ))}
              </select>
              <span>→</span>
              <select
                value={rel.listener_character_id}
                aria-label="Listener"
                onChange={(event) =>
                  setRelDrafts((current) => current.map((item, i) => (i === index ? { ...item, listener_character_id: event.target.value } : item)))
                }
              >
                {profileDrafts.map((item) => (
                  <option key={item.character_id} value={item.character_id}>
                    {item.name}
                  </option>
                ))}
              </select>
              <select
                value={rel.address_form}
                aria-label="Form of address"
                onChange={(event) =>
                  setRelDrafts((current) =>
                    current.map((item, i) =>
                      i === index ? { ...item, address_form: event.target.value as CharacterRelationshipItem["address_form"] } : item,
                    ),
                  )
                }
              >
                {ADDRESS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
              <input
                value={rel.relation ?? ""}
                placeholder="relation, e.g. son to father"
                onChange={(event) =>
                  setRelDrafts((current) => current.map((item, i) => (i === index ? { ...item, relation: event.target.value } : item)))
                }
              />
              <button
                type="button"
                className="button button-quiet button-small"
                onClick={() => setRelDrafts((current) => current.filter((_, i) => i !== index))}
                aria-label={`Remove ${nameOf(rel.speaker_character_id)} to ${nameOf(rel.listener_character_id)}`}
              >
                Remove
              </button>
            </div>
          ))}
          <div className="translation-actions">
            <button
              type="button"
              className="button button-quiet button-small"
              onClick={() =>
                setRelDrafts((current) => [
                  ...current,
                  {
                    speaker_character_id: profileDrafts[0].character_id,
                    listener_character_id: profileDrafts[1].character_id,
                    address_form: "formal",
                    relation: "",
                  },
                ])
              }
            >
              Add relationship
            </button>
            <button type="button" className="button button-small" onClick={() => void saveRelationships()}>
              Save relationships
            </button>
          </div>
        </section>
      )}

      <section>
        <h3>Glossary · fixed names and terms</h3>
        <p className="muted">Terms here are locked to your spelling in every line (e.g. places, brands, invented words).</p>
        {termDrafts.map((term, index) => (
          <div className="glossary-row" key={term.id ?? `new-${index}`}>
            <input
              value={term.source}
              placeholder="English term"
              onChange={(event) => setTermDrafts((current) => current.map((item, i) => (i === index ? { ...item, source: event.target.value } : item)))}
            />
            <input
              dir="rtl"
              lang="ur"
              className="urdu-text"
              value={term.ur ?? ""}
              placeholder="اردو"
              onChange={(event) => setTermDrafts((current) => current.map((item, i) => (i === index ? { ...item, ur: event.target.value } : item)))}
            />
            <input
              lang="hi"
              value={term.hi ?? ""}
              placeholder="हिंदी"
              onChange={(event) => setTermDrafts((current) => current.map((item, i) => (i === index ? { ...item, hi: event.target.value } : item)))}
            />
            <input
              value={term.roman ?? ""}
              placeholder="Roman Urdu"
              onChange={(event) => setTermDrafts((current) => current.map((item, i) => (i === index ? { ...item, roman: event.target.value } : item)))}
            />
            <button
              type="button"
              className="button button-quiet button-small"
              onClick={() => setTermDrafts((current) => current.filter((_, i) => i !== index))}
            >
              Remove
            </button>
          </div>
        ))}
        <div className="translation-actions">
          <button
            type="button"
            className="button button-quiet button-small"
            onClick={() => setTermDrafts((current) => [...current, { source: "", ur: "", hi: "", roman: "", case_sensitive: false, notes: null }])}
          >
            Add term
          </button>
          <button type="button" className="button button-small" onClick={() => void saveGlossary()}>
            Save glossary
          </button>
        </div>
      </section>
    </details>
  );
}
