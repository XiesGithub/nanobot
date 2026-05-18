import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, FileText, Plus, RefreshCcw, Save, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  appendMemoryDocument,
  deleteMemoryDocument,
  fetchMemoryDocuments,
  updateMemoryDocument,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import type { MemoryDocument } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

interface MemoryViewProps {
  onBackToChat: () => void;
}

export function MemoryView({ onBackToChat }: MemoryViewProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [documents, setDocuments] = useState<MemoryDocument[]>([]);
  const [selectedId, setSelectedId] = useState<string>("memory");
  const [draft, setDraft] = useState("");
  const [newEntry, setNewEntry] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const selected = useMemo(
    () => documents.find((doc) => doc.id === selectedId) ?? documents[0] ?? null,
    [documents, selectedId],
  );
  const dirty = !!selected && draft !== selected.content;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const docs = await fetchMemoryDocuments(token);
      setDocuments(docs);
      const nextSelected = docs.some((doc) => doc.id === selectedId)
        ? selectedId
        : docs[0]?.id ?? "memory";
      setSelectedId(nextSelected);
      setDraft(docs.find((doc) => doc.id === nextSelected)?.content ?? "");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [selectedId, token]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selected) return;
    setDraft(selected.content);
  }, [selected?.id]);

  const updateDoc = useCallback((doc: MemoryDocument) => {
    setDocuments((prev) => prev.map((item) => (item.id === doc.id ? doc : item)));
    setSelectedId(doc.id);
    setDraft(doc.content);
  }, []);

  const save = useCallback(async () => {
    if (!selected) return;
    setSaving(true);
    setError(null);
    try {
      const doc = await updateMemoryDocument(token, selected.id, draft);
      updateDoc(doc);
      setStatus(t("memory.status.saved", { defaultValue: "Saved." }));
      window.setTimeout(() => setStatus(null), 2_000);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [draft, selected, t, token, updateDoc]);

  const append = useCallback(async () => {
    if (!selected || !newEntry.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const doc = await appendMemoryDocument(token, selected.id, newEntry.trim());
      updateDoc(doc);
      setNewEntry("");
      setStatus(t("memory.status.added", { defaultValue: "Added." }));
      window.setTimeout(() => setStatus(null), 2_000);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [newEntry, selected, t, token, updateDoc]);

  const clear = useCallback(async () => {
    if (!selected) return;
    setSaving(true);
    setError(null);
    try {
      const doc = await deleteMemoryDocument(token, selected.id);
      updateDoc(doc);
      setStatus(t("memory.status.cleared", { defaultValue: "Cleared." }));
      window.setTimeout(() => setStatus(null), 2_000);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [selected, t, token, updateDoc]);

  return (
    <section className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <header className="flex items-center justify-between gap-3 border-b border-border/45 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={onBackToChat}
            aria-label={t("memory.backToChat", { defaultValue: "Back to chat" })}
            className="h-8 w-8 rounded-md text-muted-foreground"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden />
          </Button>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-medium">
              {t("memory.title", { defaultValue: "Knowledge / Memory" })}
            </h1>
            <p className="truncate text-[11px] text-muted-foreground">
              {t("memory.subtitle", {
                defaultValue: "Review and edit durable profile notes used by the agent.",
              })}
            </p>
          </div>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => void load()}
          disabled={loading}
          aria-label={t("memory.actions.refresh", { defaultValue: "Refresh" })}
          className="h-8 w-8 rounded-md text-muted-foreground"
        >
          <RefreshCcw className={cn("h-4 w-4", loading && "animate-spin")} aria-hidden />
        </Button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 overflow-hidden lg:grid-cols-[260px_minmax(0,1fr)]">
        <aside className="border-b border-border/45 p-3 lg:border-b-0 lg:border-r">
          <div className="flex gap-2 overflow-x-auto lg:flex-col lg:overflow-visible">
            {documents.map((doc) => (
              <button
                key={doc.id}
                type="button"
                onClick={() => setSelectedId(doc.id)}
                className={cn(
                  "flex min-w-[13rem] items-start gap-2 rounded-md px-3 py-2 text-left transition-colors lg:min-w-0",
                  selected?.id === doc.id
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/45 hover:text-foreground",
                )}
              >
                <FileText className="mt-0.5 h-4 w-4 flex-none" aria-hidden />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">
                    {doc.id === "memory"
                      ? t("memory.docs.memory", { defaultValue: "Long-term Memory" })
                      : t("memory.docs.profile", { defaultValue: "User Profile" })}
                  </span>
                  <span className="block truncate text-[11px] opacity-70">{doc.path}</span>
                </span>
              </button>
            ))}
          </div>
        </aside>

        <main className="min-h-0 overflow-y-auto px-4 py-4 sm:px-6">
          {loading && documents.length === 0 ? (
            <div className="text-sm text-muted-foreground">
              {t("memory.status.loading", { defaultValue: "Loading memory..." })}
            </div>
          ) : selected ? (
            <div className="mx-auto flex max-w-4xl flex-col gap-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium">
                    {selected.id === "memory"
                      ? t("memory.docs.memory", { defaultValue: "Long-term Memory" })
                      : t("memory.docs.profile", { defaultValue: "User Profile" })}
                  </p>
                  <p className="text-xs text-muted-foreground">{selected.description}</p>
                </div>
                <div className="flex items-center gap-2">
                  {status ? (
                    <span className="text-xs text-muted-foreground">{status}</span>
                  ) : null}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={clear}
                    disabled={saving || !selected.content.trim()}
                    className="gap-1.5 text-muted-foreground"
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden />
                    {t("memory.actions.clear", { defaultValue: "Clear" })}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    onClick={save}
                    disabled={saving || !dirty}
                    className="gap-1.5"
                  >
                    <Save className="h-3.5 w-3.5" aria-hidden />
                    {saving
                      ? t("memory.actions.saving", { defaultValue: "Saving" })
                      : t("memory.actions.save", { defaultValue: "Save" })}
                  </Button>
                </div>
              </div>

              {error ? (
                <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                  {error}
                </div>
              ) : null}

              <Textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                spellCheck={false}
                className="min-h-[46vh] resize-y font-mono text-[13px] leading-relaxed"
              />

              <div className="flex flex-col gap-2 rounded-md border border-border/60 bg-muted/25 p-3">
                <label className="text-xs font-medium text-muted-foreground">
                  {t("memory.newEntry.label", { defaultValue: "Add a new note" })}
                </label>
                <Textarea
                  value={newEntry}
                  onChange={(event) => setNewEntry(event.target.value)}
                  placeholder={t("memory.newEntry.placeholder", {
                    defaultValue: "Write a preference, profile detail, or durable fact...",
                  })}
                  className="min-h-20 resize-y text-sm"
                />
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={append}
                  disabled={saving || !newEntry.trim()}
                  className="w-fit gap-1.5"
                >
                  <Plus className="h-3.5 w-3.5" aria-hidden />
                  {t("memory.actions.add", { defaultValue: "Add note" })}
                </Button>
              </div>
            </div>
          ) : (
            <div className="text-sm text-muted-foreground">
              {t("memory.status.empty", { defaultValue: "No memory documents available." })}
            </div>
          )}
        </main>
      </div>
    </section>
  );
}
