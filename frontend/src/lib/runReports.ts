import type { RunData } from "@/lib/api";

// trade_log is capped at the first 500 rows server-side, which drops the most
// recent trades of a long run; the full trades.csv rows come alongside it.
export function runTrades(
  run: Pick<RunData, "artifacts_trades_csv" | "trade_log">,
): Array<Record<string, string>> {
  return run.artifacts_trades_csv?.length ? run.artifacts_trades_csv : run.trade_log || [];
}

