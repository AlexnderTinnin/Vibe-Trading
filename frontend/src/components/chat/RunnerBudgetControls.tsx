import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type LiveDecisionBudget, type LiveDecisionSettings } from "@/lib/api";

/** Operator-owned spending controls; the agent cannot edit these settings. */
export function RunnerBudgetControls({ broker, budget, running, onRefresh }: {
  broker: string;
  budget?: LiveDecisionBudget | null;
  running: boolean;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const [settings, setSettings] = useState(budget?.settings);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!dirty) setSettings(budget?.settings);
  }, [budget?.settings, dirty]);
  if (!budget) return null;
  if (budget.error || !settings) return <p role="alert">{t("runnerBudget.unavailable")}</p>;

  const update = (key: keyof LiveDecisionSettings, value: string | number | null) => {
    setSettings({ ...settings, [key]: value });
    setDirty(true);
  };
  const fields = ["interval_seconds", "max_daily_turns", "max_daily_tokens", "max_daily_cost_usd", "input_usd_per_million", "output_usd_per_million"] as const;
  const usage = budget.usage;
  return <div className="grid gap-2 rounded-lg border border-border/60 bg-card/60 p-3 text-xs">
    <p className="font-medium">{t("runnerBudget.warning")}</p>
    <p className="text-muted-foreground">{t("runnerBudget.summary", { minutes: settings.interval_seconds / 60, turns: settings.max_daily_turns, tokens: settings.max_daily_tokens })}</p>
    {settings.daily_at && <p>{t("runnerBudget.dailySummary", { time: settings.daily_at, timezone: settings.timezone })}</p>}
    {usage && <p>{t("runnerBudget.usage", { turns: usage.turns, tokens: usage.tokens, cost: usage.estimated_cost_usd == null ? "—" : `$${usage.estimated_cost_usd.toFixed(2)}` })}</p>}
    <p className="text-muted-foreground">{budget.projected_daily_cost_usd == null
      ? t("runnerBudget.estimateUnknown")
      : t("runnerBudget.estimate", { cost: budget.projected_daily_cost_usd.toFixed(2) })}</p>
    <details>
      <summary className="cursor-pointer font-medium">{t("runnerBudget.configure")}</summary>
      <form className="mt-3 grid gap-3" onSubmit={async (event) => {
        event.preventDefault();
        setSaving(true); setError(null);
        try {
          await api.saveRunnerSettings(broker, settings);
          setDirty(false); onRefresh();
        } catch (err) { setError(err instanceof Error ? err.message : t("runnerBudget.unavailable")); }
        finally { setSaving(false); }
      }}>
        <fieldset disabled={running || saving} className="grid gap-3 sm:grid-cols-2 disabled:opacity-60">
          {fields.map((key) => <label key={key} className="grid gap-1">
            {t(`runnerBudget.${key}`)}
            <input className="rounded-md border bg-background px-2 py-1.5" type="number" min={key === "interval_seconds" ? 60 : key.includes("usd") ? 0 : 1}
              step={key.includes("usd") ? "any" : 1} value={settings[key] ?? ""}
              required={!key.includes("usd")}
              onChange={(event) => update(key, event.target.value === "" && key.includes("usd") ? null : Number(event.target.value))} />
          </label>)}
          <label className="grid gap-1">{t("runnerBudget.daily_at")}<input className="rounded-md border bg-background px-2 py-1.5" type="time" value={settings.daily_at ?? ""} onChange={(e) => update("daily_at", e.target.value || null)} /></label>
          <label className="grid gap-1">{t("runnerBudget.timezone")}<input className="rounded-md border bg-background px-2 py-1.5" value={settings.timezone} onChange={(e) => update("timezone", e.target.value)} required /></label>
          <button className="rounded-md border px-3 py-2 font-medium" type="submit" disabled={!dirty}>{t("runnerBudget.save")}</button>
        </fieldset>
        <p className="text-muted-foreground">{t("runnerBudget.limitsNote")}</p>
        <p className="text-muted-foreground">{t("runnerBudget.pricesNote")}</p>
        {running && <p>{t("runnerBudget.stopToEdit")}</p>}
        {error && <p role="alert" className="text-destructive">{error}</p>}
      </form>
    </details>
  </div>;
}
