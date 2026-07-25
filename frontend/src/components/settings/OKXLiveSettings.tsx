import { useEffect, useId, useState, type FormEvent } from "react";
import {
  CheckCircle2,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  LockKeyhole,
  PlugZap,
  RefreshCw,
  Save,
  ShieldAlert,
  TriangleAlert,
  X,
} from "lucide-react";
import { toast } from "sonner";
import {
  api,
  type OKXConnectionCheck,
  type OKXLiveConfigStatus,
  type UpdateOKXLiveConfigRequest,
} from "@/lib/api";
import {
  OKXSecretInput,
  okxFieldClass as fieldClass,
  okxHintClass as hintClass,
  okxLabelClass as labelClass,
} from "./OKXSecretInput";

interface OKXLiveForm {
  apiKey: string;
  apiSecret: string;
  passphrase: string;
  expectedUid: string;
  proxy: string;
  clearProxy: boolean;
}

type SecretFieldName = "apiKey" | "apiSecret" | "passphrase" | "proxy";

const emptyForm: OKXLiveForm = {
  apiKey: "",
  apiSecret: "",
  passphrase: "",
  expectedUid: "",
  proxy: "",
  clearProxy: false,
};

function formFromStatus(status: OKXLiveConfigStatus): OKXLiveForm {
  return {
    ...emptyForm,
    expectedUid: status.expected_uid,
  };
}

function hasNonCredentialChanges(form: OKXLiveForm, status: OKXLiveConfigStatus): boolean {
  return form.expectedUid.trim() !== status.expected_uid
    || Boolean(form.proxy.trim())
    || form.clearProxy;
}

export function OKXLiveSettings() {
  const idPrefix = useId();
  const [config, setConfig] = useState<OKXLiveConfigStatus | null>(null);
  const [form, setForm] = useState<OKXLiveForm>(emptyForm);
  const [visible, setVisible] = useState<Record<SecretFieldName, boolean>>({
    apiKey: false,
    apiSecret: false,
    passphrase: false,
    proxy: false,
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [replacingCredentials, setReplacingCredentials] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checkResult, setCheckResult] = useState<OKXConnectionCheck | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.getOkxLiveConfig();
      setConfig(next);
      setForm(formFromStatus(next));
      setDirty(false);
      setReplacingCredentials(false);
      setCheckResult(null);
    } catch (loadError) {
      setError(
        loadError instanceof Error
          ? loadError.message
          : "Could not load the OKX live configuration",
      );
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let alive = true;
    api.getOkxLiveConfig()
      .then((next) => {
        if (!alive) return;
        setConfig(next);
        setForm(formFromStatus(next));
        setReplacingCredentials(false);
      })
      .catch((loadError) => {
        if (!alive) return;
        setError(
          loadError instanceof Error
            ? loadError.message
            : "Could not load the OKX live configuration",
        );
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const updateForm = (updates: Partial<OKXLiveForm>) => {
    setForm((current) => ({ ...current, ...updates }));
    setDirty(true);
    setCheckResult(null);
  };

  const configuredLiveCredentials = Boolean(
    config?.credentials_configured && !config.is_demo && config.profile === "live-readonly",
  );
  const credentialsLocked = configuredLiveCredentials && !replacingCredentials;
  const credentialsRequired = !configuredLiveCredentials || replacingCredentials;
  const busy = loading || saving || checking;
  const testDisabled = busy || dirty || replacingCredentials || !config?.configured;

  const beginCredentialReplacement = () => {
    setReplacingCredentials(true);
    setError(null);
    setCheckResult(null);
  };

  const cancelCredentialReplacement = () => {
    setForm((current) => ({
      ...current,
      apiKey: "",
      apiSecret: "",
      passphrase: "",
    }));
    setVisible((current) => ({
      ...current,
      apiKey: false,
      apiSecret: false,
      passphrase: false,
    }));
    setReplacingCredentials(false);
    setDirty(config ? hasNonCredentialChanges(form, config) : false);
    setError(null);
    setCheckResult(null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const credentials = [form.apiKey.trim(), form.apiSecret.trim(), form.passphrase.trim()];
    const credentialCount = credentials.filter(Boolean).length;
    if (credentialCount !== 0 && credentialCount !== 3) {
      const message = "API Key, Secret, and Passphrase must be entered together.";
      setError(message);
      toast.error(message);
      return;
    }
    if (credentialsRequired && credentialCount !== 3) {
      const message = "Enter all three OKX live credentials for initial setup.";
      setError(message);
      toast.error(message);
      return;
    }

    const payload: UpdateOKXLiveConfigRequest = {
      expected_uid: form.expectedUid.trim(),
      clear_proxy: form.clearProxy,
    };
    if (credentialCount === 3) {
      [payload.api_key, payload.api_secret, payload.passphrase] = credentials;
    }
    if (!form.clearProxy && form.proxy.trim()) {
      payload.proxy = form.proxy.trim();
    }

    setSaving(true);
    setError(null);
    try {
      const next = await api.updateOkxLiveConfig(payload);
      setConfig(next);
      setForm(formFromStatus(next));
      setDirty(false);
      setReplacingCredentials(false);
      setCheckResult(null);
      toast.success("OKX live read-only configuration saved");
    } catch (saveError) {
      const message = saveError instanceof Error
        ? saveError.message
        : "Could not save the OKX live configuration";
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  const testConnection = async () => {
    setChecking(true);
    setError(null);
    setCheckResult(null);
    try {
      const result = await api.checkOkxLiveConnection();
      setCheckResult(result);
      if (result.ok) {
        toast.success(result.message);
      } else {
        toast.error(result.message);
      }
    } catch (checkError) {
      const message = checkError instanceof Error
        ? checkError.message
        : "Could not test the OKX live connection";
      setError(message);
      toast.error(message);
    } finally {
      setChecking(false);
    }
  };

  const connectionLabel = config?.configured ? "Configured" : "Not configured";
  const connectionTone = config?.configured
    ? "bg-success/10 text-success"
    : "bg-warning/10 text-warning";

  return (
    <section className="rounded-lg border bg-card p-5 shadow-sm">
      <div className="mb-5 flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-destructive" />
            <h2 className="text-base font-semibold">OKX Live Account</h2>
          </div>
          <p className="max-w-3xl text-sm text-muted-foreground">
            Store isolated live credentials for account identity and balance checks.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${connectionTone}`}>
            {connectionLabel}
          </span>
          <button
            type="button"
            onClick={load}
            disabled={busy}
            className="inline-flex h-9 w-9 items-center justify-center rounded-md border text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
            aria-label="Refresh OKX live configuration"
            title="Refresh OKX live configuration"
          >
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          </button>
        </div>
      </div>

      <div className="mb-5 flex items-start gap-2 border-l-2 border-destructive bg-destructive/5 px-3 py-2 text-sm text-muted-foreground">
        <LockKeyhole className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
        <span>
          <span className="font-medium text-foreground">Read-only live access.</span>{" "}
          Live perpetual order writes remain disabled in this build.
        </span>
      </div>

      {error ? (
        <div className="mb-5 flex items-start gap-2 border-l-2 border-destructive bg-destructive/5 px-3 py-2 text-sm text-destructive">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      <form onSubmit={submit}>
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="grid content-start gap-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold">Live credentials</h3>
                <p className={hintClass}>Saved values are never returned to the browser.</p>
              </div>
              {configuredLiveCredentials ? (
                <button
                  type="button"
                  onClick={replacingCredentials ? cancelCredentialReplacement : beginCredentialReplacement}
                  disabled={busy}
                  className="inline-flex shrink-0 items-center justify-center gap-2 rounded-md border px-3 py-1.5 text-xs font-medium transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {replacingCredentials
                    ? <X className="h-3.5 w-3.5" />
                    : <KeyRound className="h-3.5 w-3.5" />}
                  {replacingCredentials ? "Cancel" : "Replace credentials"}
                </button>
              ) : null}
            </div>

            <OKXSecretInput
              id={`${idPrefix}-api-key`}
              label="API Key"
              ariaLabel="Live API Key"
              environment="live"
              value={form.apiKey}
              visible={visible.apiKey}
              configured={Boolean(credentialsLocked && config?.api_key_configured)}
              disabled={busy || credentialsLocked}
              required={credentialsRequired}
              onChange={(apiKey) => updateForm({ apiKey })}
              onToggleVisibility={() => setVisible((current) => ({ ...current, apiKey: !current.apiKey }))}
            />
            <OKXSecretInput
              id={`${idPrefix}-api-secret`}
              label="Secret"
              ariaLabel="Live Secret"
              environment="live"
              value={form.apiSecret}
              visible={visible.apiSecret}
              configured={Boolean(credentialsLocked && config?.api_secret_configured)}
              disabled={busy || credentialsLocked}
              required={credentialsRequired}
              onChange={(apiSecret) => updateForm({ apiSecret })}
              onToggleVisibility={() => setVisible((current) => ({ ...current, apiSecret: !current.apiSecret }))}
            />
            <OKXSecretInput
              id={`${idPrefix}-passphrase`}
              label="Passphrase"
              ariaLabel="Live Passphrase"
              environment="live"
              value={form.passphrase}
              visible={visible.passphrase}
              configured={Boolean(credentialsLocked && config?.passphrase_configured)}
              disabled={busy || credentialsLocked}
              required={credentialsRequired}
              onChange={(passphrase) => updateForm({ passphrase })}
              onToggleVisibility={() => setVisible((current) => ({ ...current, passphrase: !current.passphrase }))}
            />

            <div className="grid gap-2">
              <label className={labelClass} htmlFor={`${idPrefix}-proxy`}>
                HTTP(S) proxy <span className="font-normal text-muted-foreground">(optional)</span>
              </label>
              <div className="relative">
                <input
                  id={`${idPrefix}-proxy`}
                  type={visible.proxy ? "text" : "password"}
                  value={form.proxy}
                  onChange={(event) => updateForm({ proxy: event.target.value })}
                  className={`${fieldClass} pr-10`}
                  placeholder={config?.proxy_configured ? "Configured - leave blank to keep" : "http://127.0.0.1:7890"}
                  autoComplete="new-password"
                  disabled={busy || form.clearProxy}
                  spellCheck={false}
                />
                <button
                  type="button"
                  onClick={() => setVisible((current) => ({ ...current, proxy: !current.proxy }))}
                  disabled={busy || form.clearProxy}
                  className="absolute right-1 top-1 inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                  aria-label={`${visible.proxy ? "Hide" : "Show"} live proxy URL`}
                  title={`${visible.proxy ? "Hide" : "Show"} live proxy URL`}
                >
                  {visible.proxy ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
              {config?.proxy_configured ? (
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={form.clearProxy}
                    onChange={(event) => updateForm({ clearProxy: event.target.checked, proxy: "" })}
                    className="h-3.5 w-3.5 accent-primary"
                    disabled={busy}
                  />
                  Clear saved live proxy
                </label>
              ) : null}
            </div>
          </div>

          <div className="grid content-start gap-5 border-t pt-6 lg:border-l lg:border-t-0 lg:pl-6 lg:pt-0">
            <div>
              <h3 className="text-sm font-semibold">Account binding</h3>
              <p className={hintClass}>The live credential file is separate from OKX Demo Trading.</p>
            </div>

            <label className="grid gap-2" htmlFor={`${idPrefix}-uid`}>
              <span className={labelClass}>OKX Live UID</span>
              <input
                id={`${idPrefix}-uid`}
                aria-label="OKX Live UID"
                value={form.expectedUid}
                onChange={(event) => updateForm({ expectedUid: event.target.value })}
                className={fieldClass}
                placeholder="Live account UID"
                autoComplete="off"
                disabled={busy}
                required
              />
              <span className={hintClass}>Connection checks must match this live account identity.</span>
            </label>

            <div className="border-l-2 border-warning bg-warning/5 px-3 py-2 text-sm text-muted-foreground">
              Use a dedicated live API key with Read permission. Trade and Withdraw permissions are not required.
            </div>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-t pt-4 text-sm">
              <dt className="text-muted-foreground">SDK</dt>
              <dd className="text-right font-medium">{config?.sdk_installed ? "Installed" : "Missing"}</dd>
              <dt className="text-muted-foreground">Environment</dt>
              <dd className="text-right font-medium">Live</dd>
              <dt className="text-muted-foreground">Credential mode</dt>
              <dd className="text-right font-medium">Read only</dd>
              <dt className="text-muted-foreground">Order execution</dt>
              <dd className="text-right font-medium text-destructive">
                {config?.live_swap_writes_compiled ? "Promotion-gated" : "Build locked"}
              </dd>
            </dl>
          </div>
        </div>

        <div className="mt-6 flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs text-muted-foreground">
            {dirty ? "Unsaved live configuration changes" : "Live configuration is idle"}
          </p>
          <div className="flex flex-col gap-2 sm:flex-row">
            <button
              type="button"
              onClick={testConnection}
              disabled={testDisabled}
              className="inline-flex items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
              title={dirty || replacingCredentials ? "Save changes before testing" : "Test the saved live configuration"}
            >
              {checking ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlugZap className="h-4 w-4" />}
              {checking ? "Testing..." : "Test live connection"}
            </button>
            <button
              type="submit"
              disabled={busy}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {saving ? "Saving..." : "Save live settings"}
            </button>
          </div>
        </div>
      </form>

      {checkResult ? (
        <div className={`mt-5 flex items-start gap-3 border-l-2 px-3 py-2 text-sm ${
          checkResult.ok
            ? "border-success bg-success/5 text-success"
            : "border-destructive bg-destructive/5 text-destructive"
        }`}>
          {checkResult.ok
            ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
            : <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />}
          <div>
            <div className="font-medium">{checkResult.message}</div>
            {checkResult.ok && checkResult.total_equity ? (
              <div className="mt-1 text-xs opacity-80">Total equity: {checkResult.total_equity}</div>
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}
