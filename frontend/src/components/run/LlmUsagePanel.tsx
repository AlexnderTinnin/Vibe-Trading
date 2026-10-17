import { useTranslation } from "react-i18next";
import { Coins, Hash } from "lucide-react";
import type { LlmUsage } from "@/lib/api";
import { KeyValueTable, RunCardPanel } from "@/components/run/RunCard";

// A missing cache key means the provider never reported a cache event for
// that scope — never "0 tokens". Totals rows render only when their key is
// present; the per-iteration table only gains a cache column when at least
// one iteration reported it, and unreported cells show this marker.
const NOT_REPORTED = "—";

function cacheCell(value: number | undefined): string {
  return value === undefined ? NOT_REPORTED : String(value);
}

export function LlmUsagePanel({ usage }: { usage: LlmUsage }) {
  const { t } = useTranslation();
  const { totals } = usage;

  const summary: Record<string, unknown> = {
    [t("runDetail.llmUsage.provider")]: usage.provider,
    [t("runDetail.llmUsage.model")]: usage.model,
    [t("runDetail.llmUsage.inputTokens")]: totals.input_tokens,
    [t("runDetail.llmUsage.outputTokens")]: totals.output_tokens,
    [t("runDetail.llmUsage.totalTokens")]: totals.total_tokens,
    [t("runDetail.llmUsage.calls")]: totals.calls,
  };
  if (totals.cache_read_tokens !== undefined) {
    summary[t("runDetail.llmUsage.cacheRead")] = totals.cache_read_tokens;
  }
  if (totals.cache_creation_tokens !== undefined) {
    summary[t("runDetail.llmUsage.cacheCreation")] = totals.cache_creation_tokens;
  }

  const calls = usage.per_iteration;
  const hasCacheRead = calls.some((call) => call.cache_read_tokens !== undefined);
  const hasCacheCreation = calls.some((call) => call.cache_creation_tokens !== undefined);

  return (
    <div className="p-4 space-y-4">
      <div data-testid="llm-usage-summary">
        <RunCardPanel title={t("runDetail.llmUsage.tab")} icon={Coins}>
          <KeyValueTable data={summary} empty={t("runDetail.noneRecorded")} />
        </RunCardPanel>
      </div>
      {calls.length > 0 && (
        <div data-testid="llm-usage-iterations">
          <RunCardPanel title={t("runDetail.llmUsage.perIteration")} icon={Hash}>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-[11px] uppercase tracking-wide text-muted-foreground [&_th]:font-medium">
                    <th className="py-2 ps-4 pr-4">{t("runDetail.llmUsage.iteration")}</th>
                    <th className="py-2 pr-4 text-end">{t("runDetail.llmUsage.inputTokens")}</th>
                    <th className="py-2 pr-4 text-end">{t("runDetail.llmUsage.outputTokens")}</th>
                    <th className="py-2 pr-4 text-end">{t("runDetail.llmUsage.totalTokens")}</th>
                    {hasCacheRead && <th className="py-2 pr-4 text-end">{t("runDetail.llmUsage.cacheRead")}</th>}
                    {hasCacheCreation && <th className="py-2 text-end">{t("runDetail.llmUsage.cacheCreation")}</th>}
                  </tr>
                </thead>
                <tbody>
                  {calls.map((call, index) => (
                    <tr key={`${index}-${call.iter}`} className="border-b last:border-0 hover:bg-muted/40">
                      <td className="py-2 ps-4 pr-4 font-mono tabular-nums">{call.iter}</td>
                      <td className="py-2 pr-4 text-end tabular-nums">{call.input_tokens}</td>
                      <td className="py-2 pr-4 text-end tabular-nums">{call.output_tokens}</td>
                      <td className="py-2 pr-4 text-end tabular-nums">{call.total_tokens}</td>
                      {hasCacheRead && <td className="py-2 pr-4 text-end tabular-nums">{cacheCell(call.cache_read_tokens)}</td>}
                      {hasCacheCreation && <td className="py-2 text-end tabular-nums">{cacheCell(call.cache_creation_tokens)}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </RunCardPanel>
        </div>
      )}
    </div>
  );
}
