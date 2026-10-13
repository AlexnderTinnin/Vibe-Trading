import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";

const fieldClass = "w-full rounded-md border bg-background px-3 py-2 text-sm";

/** Explicit local export import; the user still selects it in portfolio sources. */
export function ExtraEtfImport({ onImported }: { onImported: () => Promise<void> }) {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [label, setLabel] = useState("");
  const [count, setCount] = useState("");
  const [age, setAge] = useState("7");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  return <form className="space-y-3 rounded-lg border p-4" onSubmit={async (event) => {
    event.preventDefault();
    if (!file || busy) return;
    setBusy(true); setMessage("");
    try {
      if (file.size > 4 * 1024 * 1024) throw new Error(t("portfolio.imports.tooLarge"));
      await api.importExtraEtf({ csv_text: await file.text(), label, source_mtime: new Date(file.lastModified).toISOString(), expected_positions: Number(count), max_age_days: Number(age) });
      await onImported();
      setMessage(t("portfolio.imports.success"));
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }}>
    <h3 className="font-medium">{t("portfolio.imports.title")}</h3>
    <p className="text-xs text-muted-foreground">{t("portfolio.imports.hint")}</p>
    <label className="block text-sm">{t("portfolio.imports.file")}<input required disabled={busy} type="file" accept=".csv,text/csv" className={fieldClass} onChange={(event) => { setFile(event.target.files?.[0] ?? null); setMessage(""); }} /></label>
    <label className="block text-sm">{t("portfolio.editor.accountLabel")}<input required maxLength={80} disabled={busy} value={label} onChange={(event) => setLabel(event.target.value)} className={fieldClass} /></label>
    <label className="block text-sm">{t("portfolio.imports.count")}<input required type="number" min="0" max="20000" step="1" disabled={busy} value={count} onChange={(event) => setCount(event.target.value)} className={fieldClass} /></label>
    <label className="block text-sm">{t("portfolio.imports.age")}<input required type="number" min="1" max="365" step="1" disabled={busy} value={age} onChange={(event) => setAge(event.target.value)} className={fieldClass} /></label>
    <p className="text-xs text-muted-foreground">{t("portfolio.imports.overlap")}</p>
    <button type="submit" disabled={busy || !file} className="rounded-md border px-3 py-2 text-sm disabled:opacity-50">{busy ? t("portfolio.imports.importing") : t("portfolio.imports.import")}</button>
    {message && <p role="status" className="text-sm">{message}</p>}
  </form>;
}
