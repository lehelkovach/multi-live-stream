import { useEffect, useState, type FormEvent } from "react";
import { ApiError } from "../api";
import { PLATFORMS, PROFILES, type Destination, type DestinationInput, type Platform, type Profile } from "../types";

interface Props {
  initial: Destination | null; // null => create
  onCancel: () => void;
  onSave: (input: DestinationInput, id: number | null) => Promise<void>;
}

export function DestinationForm({ initial, onCancel, onSave }: Props) {
  const [name, setName] = useState(initial?.name ?? "");
  const [platform, setPlatform] = useState<Platform>(initial?.platform ?? "youtube");
  const [url, setUrl] = useState(initial?.rtmp_url ?? PLATFORMS[0]?.url ?? "");
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [profile, setProfile] = useState<Profile>(initial?.profile ?? "copy");
  const [args, setArgs] = useState(initial?.ffmpeg_args ?? "");
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const pickPlatform = (p: Platform) => {
    setPlatform(p);
    const preset = PLATFORMS.find((x) => x.value === p)?.url ?? "";
    if (preset && (!url || PLATFORMS.some((x) => x.url === url))) setUrl(preset);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSaving(true);
    const input: DestinationInput = {
      name: name.trim(),
      platform,
      rtmp_url: url.trim(),
      enabled,
      profile,
      ffmpeg_args: profile === "custom" ? args.trim() || null : null,
    };
    if (key || !initial) input.stream_key = key.trim();
    try {
      await onSave(input, initial?.id ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="sheet-backdrop" onClick={onCancel} role="presentation">
      <form className="sheet" onClick={(e) => e.stopPropagation()} onSubmit={submit} data-testid="dest-form">
        <h2>{initial ? "Edit destination" : "Add destination"}</h2>

        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} autoFocus data-testid="form-name" />
        </label>

        <label>
          Platform
          <select value={platform} onChange={(e) => pickPlatform(e.target.value as Platform)} data-testid="form-platform">
            {PLATFORMS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          Server URL
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="rtmp://… or rtmps://… or srt://…"
            required
            inputMode="url"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            data-testid="form-url"
          />
        </label>

        <label>
          Stream key
          <span className="input-row">
            <input
              type={showKey ? "text" : "password"}
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder={initial ? (initial.stream_key_masked ? `unchanged (${initial.stream_key_masked})` : "none") : "paste from the platform"}
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              data-testid="form-key"
            />
            <button type="button" className="btn ghost" onClick={() => setShowKey((v) => !v)} aria-label="show key">
              {showKey ? "hide" : "show"}
            </button>
          </span>
        </label>

        <label>
          Profile
          <select value={profile} onChange={(e) => setProfile(e.target.value as Profile)} data-testid="form-profile">
            {PROFILES.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          <span className="muted small">{PROFILES.find((p) => p.value === profile)?.hint}</span>
        </label>

        {profile === "custom" && (
          <label>
            ffmpeg output args
            <input value={args} onChange={(e) => setArgs(e.target.value)} placeholder="-c:v libx264 -b:v 3000k -c:a copy" data-testid="form-args" />
          </label>
        )}

        <label className="check">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled
        </label>

        {error && (
          <p className="error" role="alert" data-testid="form-error">
            {error}
          </p>
        )}

        <div className="sheet-actions">
          <button type="button" className="btn ghost" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={saving} data-testid="form-submit">
            {saving ? "Saving…" : initial ? "Save" : "Add"}
          </button>
        </div>
      </form>
    </div>
  );
}
