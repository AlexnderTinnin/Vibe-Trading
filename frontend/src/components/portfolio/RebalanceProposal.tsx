import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type PortfolioSnapshot, type RebalanceProposal as Proposal } from "@/lib/api";

/** Value-only drift analysis against the exact snapshot shown to the user. */
export function RebalanceProposal({ snapshot }: { snapshot: PortfolioSnapshot }) {
  const { t, i18n } = useTranslation();
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { setTargets({}); setProposal(null); setError(""); }, [snapshot.snapshot_id]);
  const ids = [...(snapshot.combined_holdings ?? []).flatMap((row) => row.canonical_id ? [row.canonical_id] : []), "CASH"];
  const money = (value: number) => new Intl.NumberFormat(i18n.language, { style: "currency", currency: proposal?.currency ?? (snapshot.display_currency ?? "USD"), maximumFractionDigits: 2 }).format(value);
  return <details className="rounded-xl border bg-card p-5">
    <summary className="cursor-pointer font-semibold">{t("portfolio.rebalance.title")}</summary>
    <p className="my-3 text-sm text-muted-foreground">{t("portfolio.rebalance.hint")}</p>
    <form onSubmit={async (event) => {
      event.preventDefault(); setBusy(true); setError(""); setProposal(null);
      try {
        const response = await api.proposePortfolioRebalance(snapshot.snapshot_id, Object.fromEntries(ids.map((id) => [id, Number(targets[id] || 0) / 100])));
        setProposal(response.proposal);
      } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
      finally { setBusy(false); }
    }}>
      <div className="grid max-h-80 gap-2 overflow-y-auto sm:grid-cols-2 lg:grid-cols-3">{ids.map((id) => <label key={id} className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"><span className="break-all" dir="ltr">{id}</span><input aria-label={t("portfolio.rebalance.target", { id })} type="number" min="0" max="100" step="any" value={targets[id] ?? ""} disabled={busy} onChange={(event) => { setTargets({ ...targets, [id]: event.target.value }); setProposal(null); }} className="w-24 rounded border bg-background p-2" placeholder="0 %" /></label>)}</div>
      <button disabled={busy || !snapshot.complete || !snapshot.combined_holdings} className="my-3 rounded-md border px-4 py-2 text-sm disabled:opacity-50">{t("portfolio.rebalance.generate")}</button>
    </form>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    {proposal && <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr><th className="text-start">{t("portfolio.holdings.colSymbol")}</th><th>{t("portfolio.rebalance.current")}</th><th>{t("portfolio.rebalance.targetValue")}</th><th>{t("portfolio.rebalance.change")}</th></tr></thead><tbody>{proposal.rows.map((row) => <tr key={row.canonical_id}><td className="py-2" dir="ltr">{row.canonical_id}</td><td className="text-center">{money(row.current_value)}</td><td className="text-center">{money(row.target_value)}</td><td className="text-center">{money(row.change_value)}</td></tr>)}</tbody></table><p className="mt-2 text-xs text-muted-foreground">{t("portfolio.rebalance.hint")}</p></div>}
  </details>;
}
