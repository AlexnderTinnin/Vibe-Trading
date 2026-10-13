import { CheckCircle2, KeyRound, Loader2, Plus, Save, Server, ShieldCheck, TestTube2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api, type CustomProviderProfile } from "@/lib/api";

interface Draft {
  id: string;
  label: string;
  base_url: string;
  model: string;
  api_key: string;
}

const emptyDraft: Draft = { id: "", label: "", base_url: "", model: "", api_key: "" };
const fieldClass = "w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60";
const labelClass = "text-sm font-medium";
const hintClass = "text-xs text-muted-foreground";

export function CustomProviderManager({ onActivated }: { onActivated?: () => void }) {
  const { t } = useTranslation();
  const [profiles, setProfiles] = useState<CustomProviderProfile[]>([]);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [testId, setTestId] = useState<string | null>(null);
  const [testPreview, setTestPreview] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testElapsed, setTestElapsed] = useState(0);
  const [testError, setTestError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [activating, setActivating] = useState<string | null>(null);

  const refresh = async () => {
    const result = await api.listCustomProviders();
    setProfiles(result.providers);
  };

  useEffect(() => {
    void refresh().catch((error: unknown) => {
      toast.error(error instanceof Error ? error.message : t("customProviders.loadFailed"));
    });
  }, []);

  const update = (key: keyof Draft, value: string) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setTestId(null);
    setTestPreview(null);
  };

  const testProvider = async () => {
    setTesting(true);
    setTestElapsed(0);
    setTestError(null);
    setTestId(null);
    setTestPreview(null);
    try {
      const result = await api.testCustomProvider({
        base_url: draft.base_url,
        model: draft.model,
        api_key: draft.api_key,
      });
      setTestId(result.test_id);
      setTestPreview(`${result.response_preview} · ${result.latency_ms} ms`);
      toast.success(t("customProviders.testPassed"));
    } catch (error: unknown) {
      setTestError(error instanceof Error ? error.message : t("customProviders.testFailed"));
      toast.error(error instanceof Error ? error.message : t("customProviders.testFailed"));
    } finally {
      setTesting(false);
      setTestElapsed(0);
    }
  };

  useEffect(() => {
    if (!testing) return;
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setTestElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [testing]);

  const saveProvider = async () => {
    if (!testId) return;
    setSaving(true);
    try {
      await api.saveCustomProvider({ ...draft, test_id: testId });
      setDraft(emptyDraft);
      setTestId(null);
      setTestPreview(null);
      await refresh();
      toast.success(t("customProviders.saved"));
    } catch (error: unknown) {
      toast.error(error instanceof Error ? error.message : t("customProviders.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const activateProvider = async (profile: CustomProviderProfile) => {
    if (!window.confirm(t("customProviders.confirm", {label: profile.label}))) return;
    setActivating(profile.id);
    try {
      await api.activateCustomProvider(profile.id);
      onActivated?.();
      await refresh();
      toast.success(t("customProviders.activated", {label: profile.label}));
    } catch (error: unknown) {
      toast.error(error instanceof Error ? error.message : t("customProviders.activationFailed"));
    } finally {
      setActivating(null);
    }
  };

  return (
    <section className="rounded-lg border bg-card p-5 shadow-sm" dir="auto">
      <div className="mb-5 flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-primary" aria-hidden="true" />
            <h2 className="text-base font-semibold">{t("customProviders.title")}</h2>
          </div>
          <p className="max-w-3xl text-sm text-muted-foreground">
            {t("customProviders.description")}
          </p>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-1 text-xs text-muted-foreground">
          <KeyRound className="h-3 w-3" aria-hidden="true" /> {t("customProviders.keyPrivate")}
        </span>
      </div>

      {profiles.length > 0 ? (
        <div className="mb-5 space-y-2" aria-live="polite">
          {profiles.map((profile) => (
            <div key={profile.id} className="flex flex-col gap-3 rounded-md border bg-muted/20 p-3 md:flex-row md:items-center md:justify-between">
              <div className="min-w-0 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{profile.label}</span>
                  {profile.active ? <span className="rounded-full bg-success/10 px-2 py-0.5 text-xs text-success">{t("customProviders.active")}</span> : null}
                  {profile.api_key_configured ? <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">{t("customProviders.keyStored")}</span> : null}
                </div>
                <div className="break-all font-mono text-xs text-muted-foreground">{profile.model} · {profile.base_url}</div>
              </div>
              <button type="button" onClick={() => void activateProvider(profile)} disabled={profile.active || activating === profile.id} className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60">
                {activating === profile.id ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <CheckCircle2 className="h-4 w-4" aria-hidden="true" />}
                {profile.active ? t("customProviders.active") : t("customProviders.activate")}
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="mb-5 rounded-md border border-dashed bg-muted/10 px-4 py-4 text-sm text-muted-foreground">{t("customProviders.empty")}</div>
      )}

      <div className="mb-4 flex items-center gap-2 border-t pt-5">
        <Plus className="h-4 w-4 text-primary" aria-hidden="true" />
        <h3 className="text-sm font-semibold">{t("customProviders.add")}</h3>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="grid gap-2">
          <label htmlFor="custom-provider-id" className={labelClass}>{t("customProviders.id")}</label>
          <input id="custom-provider-id" disabled={testing || saving} value={draft.id} onChange={(event) => update("id", event.target.value)} className={fieldClass} placeholder="hilinkup" autoComplete="off" />
          <span className={hintClass}>{t("customProviders.idHint")}</span>
        </div>
        <div className="grid gap-2">
          <label htmlFor="custom-provider-label" className={labelClass}>{t("customProviders.label")}</label>
          <input id="custom-provider-label" disabled={testing || saving} value={draft.label} onChange={(event) => update("label", event.target.value)} className={fieldClass} placeholder={t("customProviders.labelPlaceholder")} autoComplete="off" />
        </div>
        <div className="grid gap-2 md:col-span-2">
          <label htmlFor="custom-provider-base-url" className={labelClass}><Server className="me-1 inline h-3.5 w-3.5" aria-hidden="true" />{t("customProviders.baseUrl")}</label>
          <input id="custom-provider-base-url" disabled={testing || saving} value={draft.base_url} onChange={(event) => update("base_url", event.target.value)} className={`${fieldClass} font-mono`} placeholder="https://api.example.com/v1" type="url" autoComplete="url" />
          <span className={hintClass}>{t("customProviders.urlHint")}</span>
        </div>
        <div className="grid gap-2">
          <label htmlFor="custom-provider-model" className={labelClass}>{t("customProviders.model")}</label>
          <input id="custom-provider-model" disabled={testing || saving} value={draft.model} onChange={(event) => update("model", event.target.value)} className={`${fieldClass} font-mono`} placeholder="glm-5.3-flash" autoComplete="off" />
        </div>
        <div className="grid gap-2">
          <label htmlFor="custom-provider-api-key" className={labelClass}><KeyRound className="me-1 inline h-3.5 w-3.5" aria-hidden="true" />{t("customProviders.key")}</label>
          <input id="custom-provider-api-key" disabled={testing || saving} value={draft.api_key} onChange={(event) => update("api_key", event.target.value)} className={`${fieldClass} font-mono`} placeholder={t("customProviders.keyPlaceholder")} type="password" autoComplete="new-password" />
        </div>
      </div>

      {testPreview ? <div className="mt-4 rounded-md border border-success/30 bg-success/10 px-3 py-2 text-sm text-success" role="status"><CheckCircle2 className="me-1 inline h-4 w-4" aria-hidden="true" />{t("customProviders.preview", {preview: testPreview})}</div> : null}
      {testError ? <div className="mt-4 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">{t("customProviders.error", {error: testError})}</div> : null}
      {testing && testElapsed >= 8 ? <div className="mt-4 rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground" role="status">{t("customProviders.waiting", {seconds: testElapsed})}</div> : null}
      <div className="mt-5 flex flex-wrap gap-2">
        <button type="button" onClick={() => void testProvider()} disabled={testing || saving || !draft.base_url || !draft.model || !draft.api_key} className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60">
          {testing ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <TestTube2 className="h-4 w-4" aria-hidden="true" />}
          {testing ? t("customProviders.testing") : t("customProviders.test")}
        </button>
        <button type="button" onClick={() => void saveProvider()} disabled={!testId || saving || testing || !draft.id || !draft.label} className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60">
          {saving ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Save className="h-4 w-4" aria-hidden="true" />}
          {saving ? t("customProviders.saving") : t("customProviders.save")}
        </button>
        <span className="basis-full text-xs text-muted-foreground">{t("customProviders.saveHint")}</span>
      </div>
    </section>
  );
}
