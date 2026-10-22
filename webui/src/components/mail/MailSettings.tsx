import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useClient } from "@/providers/ClientProvider";
import { mailGet, type MailSettings as Settings } from "@/lib/mail-api";
import { MailAction } from "./MailAction";

type InventoryKind = "documents" | "interactions" | "deliveries" | "actions" | "removed";
interface Item { id?: string; old_mail_id?: string; filename?: string; subject?: string;
  state?: string; status?: string; action?: string; error_code?: string; local_input?: number;
  cleanup_pending?: number; removed_at?: number; mailbox_server_name?: string }

export function MailSettings() {
  const { client, token } = useClient();
  const { t } = useTranslation();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [archive, setArchive] = useState(false);
  const [trigger, setTrigger] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [kind, setKind] = useState<InventoryKind>("documents");
  const [items, setItems] = useState<Item[]>([]);
  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [next, setNext] = useState<string | null>(null);
  const [cursor, setCursor] = useState("");
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => { setCursor(""); setRevision((value) => value + 1); }, []);

  useEffect(() => {
    let cancelled = false;
    setSettings(null); setError("");
    client.mailRequest<Settings>("get_settings").then((value) => {
      if (!cancelled) { setSettings(value); setArchive(Boolean(value.mode.archive_enabled)); setTrigger(Boolean(value.mode.trigger_agent)); }
    }).catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [client, token, revision]);

  useEffect(() => {
    const controller = new AbortController();
    setItems([]); setNext(null); setInventoryLoading(true);
    mailGet<{ items?: Item[]; sources?: Item[]; next_cursor: string | null }>(token,
      kind === "removed" ? "removed" : "inventory", { kind, cursor }, controller.signal)
      .then((value) => { if (!controller.signal.aborted) { setItems(value.items ?? value.sources ?? []); setNext(value.next_cursor); } })
      .catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setInventoryLoading(false); });
    return () => controller.abort();
  }, [token, kind, cursor, revision]);

  async function save() {
    if (!settings) return;
    setBusy(true); setError("");
    try {
      await client.mailRequest("set_mode", { archive_enabled: archive, trigger_agent: trigger, expected_revision: settings.mode.mode_revision });
      refresh();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const canManage = settings?.scopes.includes("manage") === true;
  return <div className="space-y-6">
    <p className="text-sm leading-6 text-muted-foreground">{t("mail.privacy")}</p>
    {error && <p role="alert" className="rounded-lg border border-destructive/30 p-3 text-sm text-destructive">{error}</p>}
    <Button variant="outline" size="sm" onClick={refresh} disabled={busy}>{t("mail.refresh")}</Button>
    {!settings && !error && <p role="status">{t("mail.loading")}</p>}
    {settings && <>
      <section className="space-y-4 rounded-2xl border bg-card p-5" aria-label={t("mail.mode")}>
        <h2 className="font-semibold">{t("mail.mode")}</h2>
        <label className="flex items-start gap-3"><input type="checkbox" className="mt-1 h-4 w-4 accent-primary" checked={archive} disabled={busy || !canManage || !settings.enabled} onChange={(e) => setArchive(e.target.checked)} /><span>{t("mail.archive")}<span className="block text-xs text-muted-foreground">{t("mail.archiveHint")}</span></span></label>
        <label className="flex items-start gap-3"><input type="checkbox" className="mt-1 h-4 w-4 accent-primary" checked={trigger} disabled={busy || !canManage} onChange={(e) => setTrigger(e.target.checked)} /><span>{t("mail.trigger")}<span className="block text-xs text-muted-foreground">{t("mail.triggerHint")}</span></span></label>
        <p className="text-sm text-muted-foreground">{t(`mail.modes.${Number(archive)}${Number(trigger)}`)}</p>
        <Button onClick={() => void save()} disabled={busy || !canManage || (archive === Boolean(settings.mode.archive_enabled) && trigger === Boolean(settings.mode.trigger_agent))}>{t("mail.save")}</Button>
      </section>
      <section className="space-y-3 rounded-2xl border bg-card p-5" aria-label={t("mail.health")}>
        <h2 className="font-semibold">{t("mail.health")}</h2>
        <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
          <div><dt className="text-muted-foreground">{t("mail.worker")}</dt><dd>{t(settings.status.worker_online ? "mail.online" : "mail.offline")}</dd></div>
          <div><dt className="text-muted-foreground">{t("mail.semantic")}</dt><dd>{settings.status.semantic_status}</dd></div>
          <div><dt className="text-muted-foreground">{t("mail.pending")}</dt><dd>{(settings.status.pending_bytes / 1048576).toFixed(1)} / {(settings.status.max_pending_bytes / 1048576).toFixed(1)} MiB</dd></div>
          <div><dt className="text-muted-foreground">{t("mail.admissionWaiting")}</dt><dd>{settings.status.admission_waiting_count ?? 0}</dd></div>
          <div><dt className="text-muted-foreground">{t("mail.indexPending")}</dt><dd>{settings.status.index_pending_documents ?? 0}</dd></div>
          <div><dt className="text-muted-foreground">{t("mail.types")}</dt><dd>{settings.attachment_types.join(", ")} · {settings.max_attachments} · {(settings.max_attachment_bytes / 1048576).toFixed(0)} MiB</dd></div>
        </dl>
        <p className="text-xs leading-5 text-muted-foreground">{t("mail.admissionWaitingHint")}</p>
        {Object.entries(settings.status.admission_waiting_reasons ?? {}).map(([reason, count]) => <p className="text-xs break-words" key={reason}>{reason}: {count}</p>)}
        <p className="text-xs text-muted-foreground">{t("mail.durableJobs")}</p>
        <div className="flex flex-wrap gap-2">{Object.entries(settings.status.ingest_counts ?? {}).map(([stage, count]) => <span className="rounded-md bg-muted px-2 py-1 text-xs" key={stage}>{stage}: {count}</span>)}</div>
        <div className="flex flex-wrap gap-2">{Object.entries(settings.status.counts).map(([name, count]) => <span className="rounded-md bg-muted px-2 py-1 text-xs" key={name}>{name}: {count}</span>)}</div>
        {settings.status.errors.map((item) => <p className="text-sm break-words" key={`${item.code}:${item.scope_id}`}>{item.code} · {item.scope_id} · {item.count}</p>)}
      </section>
    </>}
    <section className="space-y-4">
      <div className="flex flex-wrap gap-2" aria-label={t("mail.inventory")}>
        {(["documents", "interactions", "deliveries", "removed", "actions"] as const).map((value) => <Button key={value} size="sm" variant={kind === value ? "default" : "outline"} aria-pressed={kind === value} onClick={() => { setKind(value); setCursor(""); }}>{t(`mail.inventoryKinds.${value}`)}</Button>)}
      </div>
      <p className="text-xs leading-5 text-muted-foreground">{t("mail.inventoryHint")}</p>
      {kind === "deliveries" && <p className="text-sm text-muted-foreground">{t("mail.deliveryHint")}</p>}
      {inventoryLoading ? <p role="status" className="text-sm text-muted-foreground">{t("mail.loading")}</p> : items.length === 0 && !error && <p className="text-sm text-muted-foreground">{t("mail.empty")}</p>}
      {items.map((item) => <article className="space-y-2 rounded-xl border p-4" key={item.id ?? item.old_mail_id}>
        <h3 className="text-sm font-medium break-words">{item.subject || item.filename || item.mailbox_server_name || item.action}</h3>
        <p className="text-xs text-muted-foreground break-words">{item.id ?? item.old_mail_id} · {item.error_code || item.status || item.state}</p>
        {item.removed_at && <p className="text-xs">{new Date(item.removed_at * 1000).toLocaleString()} · {item.cleanup_pending ? t("mail.cleanupPending") : t("mail.removed")}</p>}
        {canManage && <div className="flex flex-wrap gap-2">
          {kind === "documents" && <MailAction action={item.local_input ? "retry" : "prepare_refetch"} target={item.id!} onApplied={refresh} />}
          {kind === "interactions" && <><MailAction action="dismiss_interaction" target={item.id!} onApplied={refresh} /><MailAction action="prepare_interaction_retry" target={item.id!} onApplied={refresh} /></>}
          {kind === "removed" && <MailAction action="request_restore" target={item.old_mail_id!} onApplied={refresh} />}
        </div>}
      </article>)}
      {next && <Button variant="outline" onClick={() => setCursor(next)}>{t("mail.next")}</Button>}
    </section>
  </div>;
}
