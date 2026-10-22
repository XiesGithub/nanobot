import { ApiError } from "./api";
import type { MailCitation } from "./types";

export interface MailEvidence extends MailCitation {
  mail_id: string;
  document_id: string;
  sender_address: string;
  sender_name: string;
  received_at: string | null;
  time_basis: string;
  excerpt: string;
  locator: Record<string, unknown>;
  content_role: string;
  coverage: { complete: boolean; truncated: boolean; warnings: string[] };
}
export interface SourcePage { sources: MailEvidence[]; next_cursor: string | null }
export interface MailReceipt {
  status: string;
  action_id: string;
  error_code?: string;
  confirmation_token?: string;
  target_ids: string[];
  preview: Array<Record<string, unknown>>;
}
export interface MailSettings {
  enabled: boolean;
  mode: { archive_enabled: number; trigger_agent: number; mode_revision: number };
  attachment_types: string[];
  max_attachment_bytes: number;
  max_attachments: number;
  scopes: string[];
  status: {
    counts: Record<string, number>;
    delivery_counts?: Record<string, number>;
    admission_waiting_count?: number;
    admission_waiting_reasons?: Record<string, number>;
    ingest_counts?: Record<string, number>;
    index_pending_documents?: number;
    pending_bytes: number;
    max_pending_bytes: number;
    semantic_status: string;
    worker_online: boolean;
    errors: Array<{ code: string; scope_id: string; count: number }>;
  };
}
export async function mailGet<T>(token: string, path: string, params: Record<string, string> = {}, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api/mail-knowledge/${path}?${new URLSearchParams(params)}`, {
    headers: { Authorization: `Bearer ${token}` }, cache: "no-store", credentials: "same-origin", signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, typeof body?.error === "string" ? body.error : `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

/** Each bounded range is re-authorized by the server; no credentials in URLs. */
export async function mailDownload(token: string, documentId: string, chatId: string, signal?: AbortSignal): Promise<Blob> {
  const parts: ArrayBuffer[] = [];
  let offset = 0;
  let total: number | undefined;
  while (total === undefined || offset < total) {
    const response = await fetch(`/api/mail-knowledge/attachments/${encodeURIComponent(documentId)}?chat_id=${encodeURIComponent(chatId)}`, {
      headers: { Authorization: `Bearer ${token}`, Range: `bytes=${offset}-${offset + 65535}` },
      cache: "no-store", credentials: "same-origin", signal,
    });
    if (response.status !== 206) throw new ApiError(response.status, `HTTP ${response.status}`);
    const range = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get("Content-Range") ?? "");
    if (!range || Number(range[1]) !== offset || (total !== undefined && Number(range[3]) !== total)) throw new Error("invalid_range");
    total = Number(range[3]);
    if (!Number.isSafeInteger(total) || total > 128 * 1024 * 1024) throw new Error("download_too_large");
    const body = await response.arrayBuffer();
    if (!body.byteLength || body.byteLength !== Number(range[2]) - offset + 1) throw new Error("invalid_range");
    parts.push(body);
    offset += body.byteLength;
  }
  return new Blob(parts, { type: "application/octet-stream" });
}
