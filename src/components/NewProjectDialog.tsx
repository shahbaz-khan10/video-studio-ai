import { useState, type FormEvent } from "react";
import { api } from "../lib/api";
import type { Project } from "../types";

interface NewProjectDialogProps {
  onClose: () => void;
  onCreated: (project: Project) => void;
}

export function NewProjectDialog({ onClose, onCreated }: NewProjectDialogProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [languages, setLanguages] = useState<string[]>(["ur", "hi"]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleLanguage = (code: string) => {
    setLanguages((current) =>
      current.includes(code) ? current.filter((item) => item !== code) : [...current, code],
    );
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) {
      setError("Project name is required.");
      return;
    }
    if (languages.length === 0) {
      setError("Select at least one target language.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const project = await api.createProject({
        name: name.trim(),
        description: description.trim() || null,
        source_language: "en",
        target_languages: languages,
      });
      onCreated(project);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create project.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-project-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id="new-project-title">New dubbing project</h2>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close dialog">
            ×
          </button>
        </div>
        <form onSubmit={submit}>
          <label className="field">
            <span>Project name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. Monsoon Nights"
              maxLength={160}
              autoFocus
            />
          </label>
          <label className="field">
            <span>Description (optional)</span>
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Notes about this video"
              rows={3}
              maxLength={2000}
            />
          </label>
          <fieldset className="field">
            <legend>Source language</legend>
            <span className="static-value">English</span>
          </fieldset>
          <fieldset className="field">
            <legend>Target languages</legend>
            <div className="checkbox-row">
              <label>
                <input type="checkbox" checked={languages.includes("ur")} onChange={() => toggleLanguage("ur")} />
                Urdu
              </label>
              <label>
                <input type="checkbox" checked={languages.includes("hi")} onChange={() => toggleLanguage("hi")} />
                Hindi
              </label>
            </div>
          </fieldset>
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="modal-actions">
            <button className="button button-quiet" type="button" onClick={onClose}>
              Cancel
            </button>
            <button className="button button-primary" type="submit" disabled={saving}>
              {saving ? "Creating…" : "Create project"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
