import { useCallback, useEffect, useState, type CSSProperties, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { api, type OfficeSnapshot } from "@/lib/api";
import { cn } from "@/lib/utils";

export function PixelOffice() {
  const { t } = useTranslation();
  const [snapshot, setSnapshot] = useState<OfficeSnapshot | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [agentId, setAgentId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("normal");
  const [results, setResults] = useState<Record<string, string>>({});
  const refresh = useCallback(async () => {
    setPending(true);
    try {
      const value = await api.getPixelOffice();
      setSnapshot(value);
      setAgentId((current) => current || value.agents[0]?.id || "");
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setPending(false); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  async function mutate(action: "create" | "assign" | "complete" | "rank", arguments_: Record<string, unknown>) {
    setPending(true);
    try {
      setSnapshot(await api.updatePixelOffice(action, arguments_));
      setError("");
      return true;
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); return false; }
    finally { setPending(false); }
  }
  async function create(event: FormEvent) {
    event.preventDefault();
    if (await mutate("create", { task_id: crypto.randomUUID(), title, description, priority, assigned_to: agentId || null })) {
      setTitle(""); setDescription("");
    }
  }
  const input = "mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm";
  const agents = snapshot?.agents ?? [];
  const agentName = (id: string | null) => agents.find((agent) => agent.id === id)?.name ?? t("pixelOffice.unassigned");
  return (
    <main className="h-full overflow-y-auto bg-background px-4 py-5 sm:px-6">
      <div className="mx-auto max-w-7xl space-y-5">
        <header className="flex items-start justify-between gap-4">
          <div><h1 className="text-2xl font-semibold">{t("pixelOffice.title")}</h1>
            <p className="mt-2 max-w-3xl text-sm text-muted-foreground">{t("pixelOffice.description")}</p></div>
          <button type="button" disabled={pending} onClick={() => void refresh()} className="rounded-md border px-3 py-2 text-sm disabled:opacity-50">{t("pixelOffice.refresh")}</button>
        </header>
        {error && <p role="alert" className="rounded-lg border border-destructive/30 p-3 text-sm text-destructive">{error}</p>}
        {!snapshot && <p role="status">{t(pending ? "pixelOffice.loading" : "pixelOffice.unavailable")}</p>}
        {snapshot && <>
          <section className="pixel-office-floor rounded-xl border border-border p-4 sm:p-6" aria-label={t("pixelOffice.floor")}>
            <h2 className="mb-4 font-semibold">{t("pixelOffice.floor")}</h2>
            <div className="grid gap-4 md:grid-cols-3">{agents.map((agent) => (
              <button type="button" key={agent.id} onClick={() => setAgentId(agent.id)}
                className={cn("pixel-desk rounded-lg border p-4 text-start", agentId === agent.id ? "border-primary ring-2 ring-primary/20" : "border-border/70")}>
                <div className="flex items-start justify-between gap-3">
                  <div className="pixel-character" style={{ "--pixel-color": agent.color } as CSSProperties} aria-hidden="true">
                    <div className="pixel-character__shadow" /><div className="pixel-character__body">
                      <span className="pixel-character__head" /><span className="pixel-character__torso" />
                      <span className="pixel-character__arm pixel-character__arm--left" /><span className="pixel-character__arm pixel-character__arm--right" />
                      <span className="pixel-character__leg pixel-character__leg--left" /><span className="pixel-character__leg pixel-character__leg--right" />
                    </div>
                  </div>
                  <span className="rounded-full bg-background/70 px-2 py-1 text-xs">{t("pixelOffice.taskCount", { count: agent.load })}</span>
                </div>
                <p className="mt-3 font-semibold">{agent.name}</p><p className="text-xs text-muted-foreground">{t(`pixelOffice.ranks.${agent.rank}`)}</p>
              </button>
            ))}</div>
          </section>
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
            <section className="rounded-xl border border-border bg-card p-4">
              <h2 className="mb-4 font-semibold">{t("pixelOffice.board")}</h2>
              {!snapshot.tasks.length && <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">{t("pixelOffice.empty")}</p>}
              <div className="space-y-3">{snapshot.tasks.map((task) => (
                <article key={task.id} className="rounded-lg border border-border/70 p-3">
                  <div className="flex flex-wrap justify-between gap-2"><h3 className="break-words font-medium">{task.title}</h3><span className="text-xs text-muted-foreground">{t(`pixelOffice.priorities.${task.priority}`)} · {t(`pixelOffice.statuses.${task.status}`)}</span></div>
                  <p className="mt-1 whitespace-pre-wrap break-words text-sm text-muted-foreground">{task.description}</p>
                  <p className="mt-2 text-xs text-primary">{agentName(task.assigned_to)}</p>
                  {task.status === "completed" ? <p className="mt-2 whitespace-pre-wrap break-words text-sm">{task.result}</p> : (
                    <fieldset disabled={pending} className="mt-3 space-y-2 disabled:opacity-50">
                      <label className="block text-xs">{t("pixelOffice.assignee")}
                        <select className={input} value={task.assigned_to ?? ""} onChange={(event) => void mutate("assign", { task_id: task.id, agent_id: event.target.value })}>
                          <option value="" disabled>{t("pixelOffice.unassigned")}</option>{agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}
                        </select>
                      </label>
                      <form onSubmit={(event) => { event.preventDefault(); void mutate("complete", { task_id: task.id, result: results[task.id] ?? "" }); }}>
                        <label className="block text-xs">{t("pixelOffice.result")}
                          <textarea className={input} rows={2} required maxLength={2000} value={results[task.id] ?? ""} onChange={(event) => setResults((current) => ({ ...current, [task.id]: event.target.value }))} />
                        </label>
                        <button type="submit" disabled={!results[task.id]?.trim()} className="mt-2 rounded-md border px-3 py-1.5 text-xs disabled:opacity-50">{t("pixelOffice.complete")}</button>
                      </form>
                    </fieldset>
                  )}
                </article>
              ))}</div>
            </section>
            <section className="h-fit rounded-xl border border-border bg-card p-4">
              <h2 className="mb-4 font-semibold">{t("pixelOffice.create")}</h2>
              <form onSubmit={(event) => void create(event)}><fieldset disabled={pending} className="space-y-3 disabled:opacity-50">
                <label className="block text-xs">{t("pixelOffice.assignee")}<select className={input} value={agentId} onChange={(event) => setAgentId(event.target.value)}><option value="">{t("pixelOffice.unassigned")}</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
                <label className="block text-xs">{t("pixelOffice.taskTitle")}<input className={input} required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
                <label className="block text-xs">{t("pixelOffice.instructions")}<textarea className={input} rows={3} maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
                <label className="block text-xs">{t("pixelOffice.priority")}<select className={input} value={priority} onChange={(event) => setPriority(event.target.value)}>{(["low", "normal", "high"] as const).map((value) => <option key={value} value={value}>{t(`pixelOffice.priorities.${value}`)}</option>)}</select></label>
                <button type="submit" disabled={!title.trim()} className="w-full rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50">{t("pixelOffice.create")}</button>
              </fieldset></form>
            </section>
          </div>
          <section className="rounded-xl border border-border bg-card p-4"><h2 className="mb-3 font-semibold">{t("pixelOffice.ranksTitle")}</h2>
            <div className="grid gap-3 md:grid-cols-3">{agents.map((agent) => <label key={agent.id} className="block text-sm">{agent.name}
              <select className={input} disabled={pending} value={agent.rank} onChange={(event) => void mutate("rank", { agent_id: agent.id, rank: event.target.value })}>{(["Operator", "Senior", "Lead"] as const).map((rank) => <option key={rank} value={rank}>{t(`pixelOffice.ranks.${rank}`)}</option>)}</select>
            </label>)}</div>
          </section>
        </>}
      </div>
    </main>
  );
}
