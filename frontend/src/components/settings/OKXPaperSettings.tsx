import { useEffect, useId, useState, type FormEvent } from "react";
import {
  CheckCircle2,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  PlugZap,
  RefreshCw,
  Save,
  ShieldCheck,
  TriangleAlert,
  X,
} from "lucide-react";
import { toast } from "sonner";
import {
  api,
  type OKXConnectionCheck,
  type OKXPaperConfigStatus,
  type OKXSwapInstrument,
  type UpdateOKXPaperConfigRequest,
} from "@/lib/api";
import {
  OKXSecretInput,
  okxFieldClass as fieldClass,
  okxHintClass as hintClass,
  okxLabelClass as labelClass,
} from "./OKXSecretInput";

interface OKXPaperForm {
  apiKey: string;
  apiSecret: string;
  passphrase: string;
  expectedUid: string;
  instruments: OKXSwapInstrument[];
  maxLeverage: number;
  proxy: string;
  clearProxy: boolean;
}

type SecretFieldName = "apiKey" | "apiSecret" | "passphrase" | "proxy";

const DEFAULT_INSTRUMENTS: OKXSwapInstrument[] = [
  "BTC-USDT-SWAP",
  "ETH-USDT-SWAP",
];
function formFromStatus(status: OKXPaperConfigStatus): OKXPaperForm {
  return {
    apiKey: "",
    apiSecret: "",
    passphrase: "",
    expectedUid: status.expected_uid,
    instruments: status.allowed_swap_instruments.length
      ? status.allowed_swap_instruments
      : DEFAULT_INSTRUMENTS,
    maxLeverage: status.max_swap_leverage,
    proxy: "",
    clearProxy: false,
  };
}

function hasNonCredentialChanges(form: OKXPaperForm, status: OKXPaperConfigStatus): boolean {
  const currentInstruments = [...form.instruments].sort().join(",");
  const savedInstruments = [...status.allowed_swap_instruments].sort().join(",");
  return form.expectedUid.trim() !== status.expected_uid
    || currentInstruments !== savedInstruments
    || form.maxLeverage !== status.max_swap_leverage
    || Boolean(form.proxy.trim())
    || form.clearProxy;
}

export function OKXPaperSettings() {
  const idPrefix = useId();
  const [config, setConfig] = useState<OKXPaperConfigStatus | null>(null);
  const [form, setForm] = useState<OKXPaperForm>({
    apiKey: "",
    apiSecret: "",
    passphrase: "",
    expectedUid: "",
    instruments: DEFAULT_INSTRUMENTS,
    maxLeverage: 10,
    proxy: "",
    clearProxy: false,
  });
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
      const next = await api.getOkxPaperConfig();
      setConfig(next);
      setForm(formFromStatus(next));
      setDirty(false);
      setReplacingCredentials(false);
      setCheckResult(null);
    } catch (loadError) {
      setError(
        loadError instanceof Error
          ? loadError.message
          : "Could not load the OKX Demo configuration",
      );
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let alive = true;
    api.getOkxPaperConfig()
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
            : "Could not load the OKX Demo configuration",
        );
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const updateForm = (updates: Partial<OKXPaperForm>) => {
    setForm((current) => ({ ...current, ...updates }));
    setDirty(true);
    setCheckResult(null);
  };

  const toggleInstrument = (instrument: OKXSwapInstrument, checked: boolean) => {
    const instruments = checked
      ? [...new Set([...form.instruments, instrument])]
      : form.instruments.filter((item) => item !== instrument);
    updateForm({ instruments });
  };

  const configuredDemoCredentials = Boolean(config?.credentials_configured && config.is_demo);
  const credentialsLocked = configuredDemoCredentials && !replacingCredentials;
  const credentialsRequired = !configuredDemoCredentials || replacingCredentials;
  const busy = loading || saving || checking;
  const saveDisabled = busy || config?.runner_running === true;
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
      const message = "Enter all three OKX Demo credentials for initial setup.";
      setError(message);
      toast.error(message);
      return;
    }
    if (!form.instruments.length) {
      const message = "Select at least one swap instrument.";
      setError(message);
      toast.error(message);
      return;
    }

    const payload: UpdateOKXPaperConfigRequest = {
      expected_uid: form.expectedUid.trim(),
      allowed_swap_instruments: form.instruments,
      max_swap_leverage: form.maxLeverage,
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
      const next = await api.updateOkxPaperConfig(payload);
      setConfig(next);
      setForm(formFromStatus(next));
      setDirty(false);
      setReplacingCredentials(false);
      setCheckResult(null);
      toast.success("OKX Demo configuration saved");
    } catch (saveError) {
      const message = saveError instanceof Error
        ? saveError.message
        : "Could not save the OKX Demo configuration";
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
      const result = await api.checkOkxPaperConnection();
      setCheckResult(result);
      if (result.ok) {
        toast.success(result.message);
      } else {
        toast.error(result.message);
      }
    } catch (checkError) {
      const message = checkError instanceof Error
        ? checkError.message
        : "Could not test the OKX Demo connection";
      setError(message);
      toast.error(message);
    } finally {
      setChecking(false);
    }
  };

  const connectionLabel = config?.configured
    ? "Configured"
    : config?.environment === "live"
      ? "Live profile detected"
      : "Not configured";
  const connectionTone = config?.configured
    ? "bg-success/10 text-success"
    : config?.environment === "live"
      ? "bg-destructive/10 text-destructive"
      : "bg-warning/10 text-warning";

  return (
    <section className="rounded-lg border bg-card p-5 shadow-sm">
      <div className="mb-5 flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-primary" />
            <h2 className="text-base font-semibold">OKX Demo Trading</h2>
          </div>
          <p className="max-w-3xl text-sm text-muted-foreground">
            Configure the dedicated API credentials used by the BTC/ETH perpetual quant runner.
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
            aria-label="Refresh OKX configuration"
            title="Refresh OKX configuration"
          >
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          </button>
        </div>
      </div>

      <div className="mb-5 border-l-2 border-warning bg-warning/5 px-3 py-2 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">Demo only.</span>{" "}
        Use a separate OKX Demo Trading API key with Read and Trade permissions. Withdraw permission is not needed.
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
                <h3 className="text-sm font-semibold">Credentials</h3>
                <p className={hintClass}>Saved values are never returned to the browser.</p>
              </div>
              {configuredDemoCredentials ? (
                <button
                  type="button"
                  onClick={replacingCredentials ? cancelCredentialReplacement : beginCredentialReplacement}
                  disabled={busy || config?.runner_running === true}
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
              environment="Demo"
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
              environment="Demo"
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
              environment="Demo"
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
                  aria-label={`${visible.proxy ? "Hide" : "Show"} proxy URL`}
                  title={`${visible.proxy ? "Hide" : "Show"} proxy URL`}
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
                  Clear saved proxy
                </label>
              ) : null}
            </div>
          </div>

          <div className="grid content-start gap-5 border-t pt-6 lg:border-l lg:border-t-0 lg:pl-6 lg:pt-0">
            <div>
              <h3 className="text-sm font-semibold">Account and risk scope</h3>
              <p className={hintClass}>The server always forces the OKX simulated-trading environment.</p>
            </div>

            <label className="grid gap-2" htmlFor={`${idPrefix}-uid`}>
              <span className={labelClass}>OKX Demo UID</span>
              <input
                id={`${idPrefix}-uid`}
                aria-label="OKX Demo UID"
                value={form.expectedUid}
                onChange={(event) => updateForm({ expectedUid: event.target.value })}
                className={fieldClass}
                placeholder="Demo account UID"
                autoComplete="off"
                disabled={busy}
                required
              />
              <span className={hintClass}>Used to pin connection checks and perpetual mandates to this Demo account.</span>
            </label>

            <fieldset className="grid gap-2" disabled={busy}>
              <legend className={labelClass}>Allowed perpetual swaps</legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {DEFAULT_INSTRUMENTS.map((instrument) => (
                  <label key={instrument} className="flex items-center gap-2 border-l-2 border-border bg-muted/20 px-3 py-2 text-sm">
                    <input
                      type="checkbox"
                      checked={form.instruments.includes(instrument)}
                      onChange={(event) => toggleInstrument(instrument, event.target.checked)}
                      className="h-4 w-4 accent-primary"
                    />
                    <span className="font-mono text-xs">{instrument}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <label className="grid gap-2" htmlFor={`${idPrefix}-leverage`}>
              <span className="flex items-center justify-between gap-3 text-sm font-medium">
                <span>Maximum leverage</span>
                <span className="font-mono text-primary">{form.maxLeverage}x</span>
              </span>
              <input
                id={`${idPrefix}-leverage`}
                aria-label="Maximum leverage"
                type="range"
                min={1}
                max={10}
                step={1}
                value={form.maxLeverage}
                onChange={(event) => updateForm({ maxLeverage: Number(event.target.value) })}
                className="h-2 w-full cursor-pointer accent-primary disabled:cursor-not-allowed disabled:opacity-60"
                disabled={busy}
              />
              <div className="flex justify-between text-xs text-muted-foreground">
                <span>1x</span>
                <span>10x ceiling</span>
              </div>
            </label>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-t pt-4 text-sm">
              <dt className="text-muted-foreground">SDK</dt>
              <dd className="text-right font-medium">{config?.sdk_installed ? "Installed" : "Missing"}</dd>
              <dt className="text-muted-foreground">Quant runner</dt>
              <dd className="text-right font-medium">{config?.runner_running ? "Running" : "Stopped"}</dd>
              <dt className="text-muted-foreground">Environment</dt>
              <dd className="text-right font-medium">{config?.is_demo ? "Demo" : "Not Demo"}</dd>
            </dl>
          </div>
        </div>

        <div className="mt-6 flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs text-muted-foreground">
            {config?.runner_running
              ? "Stop the quant runner before changing this configuration."
              : dirty
                ? "Unsaved changes"
                : "Configuration is idle"}
          </p>
          <div className="flex flex-col gap-2 sm:flex-row">
            <button
              type="button"
              onClick={testConnection}
              disabled={testDisabled}
              className="inline-flex items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
              title={dirty ? "Save changes before testing" : "Test the saved configuration"}
            >
              {checking ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlugZap className="h-4 w-4" />}
              {checking ? "Testing..." : "Test connection"}
            </button>
            <button
              type="submit"
              disabled={saveDisabled}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {saving ? "Saving..." : "Save OKX Demo settings"}
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
