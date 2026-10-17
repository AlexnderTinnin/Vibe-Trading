import { useTranslation } from "react-i18next";
import { abbreviateNum } from "@/lib/formatters";

interface LlmUsageCounterProps {
  input: number;
  output: number;
}

/**
 * Live token counter for the in-flight attempt, fed by `llm_usage` SSE
 * events. Renders nothing before the first usage delta arrives — a chat
 * stream that never measured a call shows no fabricated zero.
 */
export function LlmUsageCounter({ input, output }: LlmUsageCounterProps) {
  const { t } = useTranslation();
  if (input === 0 && output === 0) return null;
  return (
    <p className="px-3 text-xs text-muted-foreground">
      {"\u2191"}{abbreviateNum(input)} {"\u2193"}{abbreviateNum(output)} {t("agent.tokenUsage.live")}
    </p>
  );
}
