"use client";

import { useEffect, useState } from "react";
import { getReactivationEmailPreference, saveReactivationEmailPreference } from "@/lib/api";

export function ReactivationEmailPreference({ token }: { token: string }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    getReactivationEmailPreference(token)
      .then((preference) => { if (!cancelled) setEnabled(preference.enabled); })
      .catch(() => { if (!cancelled) setError("Email preferences are unavailable right now."); });
    return () => { cancelled = true; };
  }, [token]);

  async function toggle(checked: boolean) {
    setSaving(true);
    setError("");
    try {
      const updated = await saveReactivationEmailPreference(token, checked);
      setEnabled(updated.enabled);
    } catch {
      setError("Unable to save this choice. Adult athletes only.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="support-panel">
      <h3>Email reminders</h3>
      <label className="settings-toggle-row">
        <input
          type="checkbox"
          checked={enabled ?? false}
          disabled={enabled === null || saving}
          onChange={(event) => void toggle(event.target.checked)}
        />
        Email me one reminder if I haven&apos;t used UNLXCK for seven days.
      </label>
      <p className="muted">
        Optional. We use your recent app activity to decide when to send it.
        You can opt out here or from the email itself.
      </p>
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}
