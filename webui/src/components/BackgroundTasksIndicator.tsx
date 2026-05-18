import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchSubagentTasks } from "@/lib/api";
import type { SubagentTask } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

const POLL_MS = 3_000;

export function BackgroundTasksIndicator() {
  const { t } = useTranslation();
  const { token } = useClient();
  const [tasks, setTasks] = useState<SubagentTask[]>([]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const schedule = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(poll, POLL_MS);
    };

    const poll = async () => {
      try {
        const rows = await fetchSubagentTasks(token);
        if (!cancelled) setTasks(rows);
      } catch {
        if (!cancelled) setTasks([]);
      } finally {
        if (!cancelled) schedule();
      }
    };

    void poll();
    const onFocus = () => void poll();
    window.addEventListener("focus", onFocus);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [token]);

  if (tasks.length === 0) return null;

  const first = tasks[0];
  return (
    <div
      role="status"
      className="pointer-events-none fixed left-1/2 top-3 z-40 flex -translate-x-1/2 items-center gap-2 rounded-full border border-border/70 bg-popover/95 px-3 py-1.5 text-[11.5px] font-medium text-popover-foreground shadow-sm backdrop-blur"
    >
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-500/50" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
      </span>
      <span className="max-w-[min(70vw,28rem)] truncate">
        {t("backgroundTasks.running", {
          defaultValue: "{{count}} background task is analyzing...",
          count: tasks.length,
          label: first?.label ?? "",
        })}
      </span>
    </div>
  );
}
