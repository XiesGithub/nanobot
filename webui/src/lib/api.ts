import type {
  ChatSummary,
  MemoryDocument,
  ProviderSettingsUpdate,
  SettingsPayload,
  SettingsUpdate,
  SlashCommand,
  SubagentTask,
  WebSearchSettingsUpdate,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(url, {
    ...(init ?? {}),
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
    credentials: "same-origin",
  });
  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    title: s.title ?? "",
    preview: s.preview ?? "",
  }));
}

/** Signed image URL attached to a historical user message. The server
 * emits these in place of raw on-disk paths so the client can render
 * previews without learning where media lives on disk. Each URL is a
 * self-authenticating ``/api/media/...`` route (see backend
 * ``_sign_media_path``) safe to drop into an ``<img src>`` attribute. */
export interface SessionMediaUrl {
  url: string;
  name?: string;
}

export async function fetchSessionMessages(
  token: string,
  key: string,
  base: string = "",
): Promise<{
  key: string;
  created_at: string | null;
  updated_at: string | null;
  messages: Array<{
    role: string;
    content: string;
    timestamp?: string;
    tool_calls?: unknown;
    tool_call_id?: string;
    name?: string;
    /** Present on ``user`` turns that attached images. Paths have already
     * been stripped server-side; only the signed fetch URLs survive. */
    media_urls?: SessionMediaUrl[];
    usage?: {
      prompt_tokens: number;
      completion_tokens: number;
      total_tokens: number;
      cached_tokens?: number;
      estimated_cost_usd?: number | null;
      cost_source?: string;
      model?: string;
    };
  }>;
}> {
  return request(
    `${base}/api/sessions/${encodeURIComponent(key)}/messages`,
    token,
  );
}

export async function deleteSession(
  token: string,
  key: string,
  base: string = "",
): Promise<boolean> {
  const body = await request<{ deleted: boolean }>(
    `${base}/api/sessions/${encodeURIComponent(key)}/delete`,
    token,
  );
  return body.deleted;
}

export async function fetchMemoryDocuments(
  token: string,
  base: string = "",
): Promise<MemoryDocument[]> {
  const body = await request<{ documents: MemoryDocument[] }>(
    `${base}/api/memory`,
    token,
  );
  return body.documents;
}

export async function updateMemoryDocument(
  token: string,
  doc: string,
  content: string,
  base: string = "",
): Promise<MemoryDocument> {
  const query = new URLSearchParams();
  query.set("doc", doc);
  query.set("content", content);
  const body = await request<{ document: MemoryDocument }>(
    `${base}/api/memory/update?${query}`,
    token,
  );
  return body.document;
}

export async function appendMemoryDocument(
  token: string,
  doc: string,
  content: string,
  base: string = "",
): Promise<MemoryDocument> {
  const query = new URLSearchParams();
  query.set("doc", doc);
  query.set("content", content);
  const body = await request<{ document: MemoryDocument }>(
    `${base}/api/memory/append?${query}`,
    token,
  );
  return body.document;
}

export async function deleteMemoryDocument(
  token: string,
  doc: string,
  base: string = "",
): Promise<MemoryDocument> {
  const query = new URLSearchParams();
  query.set("doc", doc);
  const body = await request<{ document: MemoryDocument }>(
    `${base}/api/memory/delete?${query}`,
    token,
  );
  return body.document;
}

export async function fetchSubagentTasks(
  token: string,
  base: string = "",
): Promise<SubagentTask[]> {
  const body = await request<{ count: number; tasks: SubagentTask[] }>(
    `${base}/api/subagents`,
    token,
  );
  return body.tasks;
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(`${base}/api/settings`, token);
}

export async function listSlashCommands(
  token: string,
  base: string = "",
): Promise<SlashCommand[]> {
  type Row = {
    command: string;
    title: string;
    description: string;
    icon: string;
    arg_hint?: string;
  };
  const body = await request<{ commands: Row[] }>(`${base}/api/commands`, token);
  return body.commands
    .filter((command) => !["/stop", "/restart"].includes(command.command))
    .map((command) => ({
      command: command.command,
      title: command.title,
      description: command.description,
      icon: command.icon,
      argHint: command.arg_hint ?? "",
    }));
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.model !== undefined) query.set("model", update.model);
  if (update.provider !== undefined) query.set("provider", update.provider);
  return request<SettingsPayload>(`${base}/api/settings/update?${query}`, token);
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.apiBase !== undefined) query.set("api_base", update.apiBase);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/update?${query}`,
    token,
  );
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.baseUrl !== undefined) query.set("base_url", update.baseUrl);
  return request<SettingsPayload>(
    `${base}/api/settings/web-search/update?${query}`,
    token,
  );
}
