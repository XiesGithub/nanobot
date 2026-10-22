import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { useClient } from "@/providers/ClientProvider";
import { mailDownload, mailGet, type SourcePage } from "@/lib/mail-api";
import { ApiError } from "@/lib/api";
import type { MailCitation } from "@/lib/types";
import { MailAction } from "./MailAction";

function locationText(locator: Record<string, unknown>, label: (key: string) => string): string {
  const spans = Array.isArray(locator.spans) ? locator.spans as Array<{ locator?: Record<string, unknown> }> : [{ locator }];
  return [...new Set(spans.flatMap((span) => Object.entries(span.locator ?? {})
    .filter(([key, value]) => ["page", "paragraph", "line", "table", "row", "column", "sheet", "slide"].includes(key)
      && ["string", "number"].includes(typeof value))
    .map(([key, value]) => `${label(key)} ${String(value)}`)))].join(" · ");
}

export function MailSources({ citations, chatId }: { citations: MailCitation[]; chatId: string }) {
  const { token, mailOwner, privateAccessDenied, invalidatePrivateAccess } = useClient();
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState<SourcePage | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => { request.current?.abort(); setPage(null); setSelected(null); }, [token, chatId]);
  useEffect(() => {
    if (privateAccessDenied) {
      request.current?.abort();
      setPage(null);
      setBusy(false);
    }
  }, [privateAccessDenied]);
  async function load(id: string, cursor?: string) {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setSelected(id); setPage(null); setError(""); setBusy(true);
    try { const next = await mailGet<SourcePage>(token, `sources/${encodeURIComponent(id)}`, { chat_id: chatId, ...(cursor ? { cursor } : {}) }, controller.signal);
      if (!controller.signal.aborted) setPage(next);
    } catch (e) { if (!controller.signal.aborted) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) invalidatePrivateAccess(token);
      setError((e as Error).message);
    } }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  async function download(documentId: string, filename: string) {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError("");
    try {
      const blob = await mailDownload(token, documentId, chatId, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename;
      anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { if (!controller.signal.aborted) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) invalidatePrivateAccess(token);
      setPage(null); setError((e as Error).message);
    } }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  if (!mailOwner) return null;
  return <div className="mt-3 flex flex-wrap gap-2" aria-label={t("mail.sources")}>
    {!privateAccessDenied && citations.map((citation, i) => <Button variant="outline" size="sm" key={citation.evidence_id}
      onClick={() => void load(citation.evidence_id)} className="max-w-full justify-start">
      <span className="truncate">[{i + 1}] {citation.filename || citation.subject || t("mail.sources")}</span>
    </Button>)}
    <Dialog open={selected !== null} onOpenChange={(open) => { if (!open) { request.current?.abort(); setSelected(null); setPage(null); } }}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        <DialogTitle>{t("mail.sources")}</DialogTitle>
        <DialogDescription>{t("mail.sourceNotice")}</DialogDescription>
        {busy && <p role="status">{t("mail.loading")}</p>}
        {error && <p role="alert" className="text-destructive">{error}</p>}
        {!privateAccessDenied && page?.sources.map((source) => <article key={source.evidence_id} className="space-y-3 rounded-xl border p-4">
          <h3 className="font-semibold break-words">{source.subject}</h3>
          <p className="text-xs text-muted-foreground break-words">{source.sender_name} &lt;{source.sender_address}&gt; · {source.received_at ? new Date(source.received_at).toLocaleString() : t("mail.unknownTime")}</p>
          <p className="text-sm">{source.filename || t("mail.body")} · {t(`mail.roles.${source.content_role}`, { defaultValue: source.content_role })}</p>
          <p className="text-xs text-muted-foreground break-words">{locationText(source.locator, (key) => t(`mail.locations.${key}`, { defaultValue: key }))}</p>
          {(!source.coverage.complete || source.coverage.truncated || source.coverage.warnings.length > 0) && <p className="text-sm text-amber-700 dark:text-amber-300">{t("mail.partial")} {source.coverage.warnings.join(" · ")}</p>}
          <p className="whitespace-pre-wrap break-words text-sm leading-7">{source.excerpt}</p>
          <div className="flex flex-wrap gap-2">
            {source.filename && <Button variant="outline" size="sm" disabled={busy} onClick={() => void download(source.document_id, source.filename!)}>{t("mail.download")}</Button>}
            <MailAction action="prepare_remove" target={source.mail_id} chatId={chatId} onApplied={() => { setPage(null); setSelected(null); }} />
            {source.filename && <MailAction action="prepare_refetch" target={source.document_id} chatId={chatId} />}
          </div>
        </article>)}
        {selected && page?.next_cursor && <Button variant="outline" disabled={busy} onClick={() => void load(selected, page.next_cursor!)}>{t("mail.next")}</Button>}
      </DialogContent>
    </Dialog>
  </div>;
}
