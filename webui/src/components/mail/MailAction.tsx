import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { useClient } from "@/providers/ClientProvider";
import type { MailReceipt } from "@/lib/mail-api";

const confirmations: Record<string, string> = {
  prepare_remove: "confirm_remove", request_restore: "confirm_restore",
  prepare_refetch: "confirm_refetch", prepare_interaction_retry: "confirm_interaction_retry",
};

export function MailAction({ action, target, chatId = "management", onApplied }: {
  action: string; target: string; chatId?: string; onApplied?: () => void;
}) {
  const { client } = useClient();
  const { t } = useTranslation();
  const [receipt, setReceipt] = useState<MailReceipt | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  async function execute(confirm = false) {
    setBusy(true); setError("");
    try {
      const next = await client.mailRequest<MailReceipt>(confirm ? confirmations[action] : action,
        confirm ? { token: receipt?.confirmation_token, chat_id: chatId } : { target_ids: [target], chat_id: chatId });
      setReceipt(next);
      setConfirmOpen(next.status === "needs_confirmation");
      if (next.status !== "needs_confirmation") onApplied?.();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="space-y-1">
    <Button size="sm" variant="outline" disabled={busy} onClick={() => void execute()}>{t(`mail.actions.${action}`)}</Button>
    {receipt && receipt.status !== "needs_confirmation" && <p role="status" className="text-xs text-muted-foreground">{t(`mail.receipts.${receipt.status}`, { defaultValue: receipt.status })}</p>}
    {!confirmOpen && error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    <Dialog open={confirmOpen} onOpenChange={(open) => { if (!busy) { setConfirmOpen(open); if (!open) setReceipt(null); } }}>
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogTitle>{t(`mail.actions.${action}`)}</DialogTitle>
        <DialogDescription>{t(`mail.confirm.${action}`)} {t("mail.confirmExpiry")}</DialogDescription>
        <ul className="space-y-2 text-sm">
          {receipt?.preview?.map((item, i) => <li className="rounded-lg border p-3 break-words" key={i}>
            <p>{String(item.subject ?? item.filename ?? item.mailbox ?? item.id)}</p>
            <p className="text-xs text-muted-foreground">{String(item.id)}</p>
            {item.removed_at ? <p>{new Date(Number(item.removed_at) * 1000).toLocaleString()}</p> : null}
          </li>)}
        </ul>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="outline" disabled={busy} onClick={() => { setConfirmOpen(false); setReceipt(null); }}>{t("mail.cancel")}</Button>
          <Button disabled={busy || !receipt?.confirmation_token} onClick={() => void execute(true)}>{t("mail.confirmAction")}</Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>;
}
