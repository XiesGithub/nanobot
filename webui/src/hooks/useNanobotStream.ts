import { useCallback, useEffect, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import { toMediaAttachment } from "@/lib/media";
import type { StreamError } from "@/lib/nanobot-client";
import type {
  InboundEvent,
  OutboundImageGeneration,
  OutboundMedia,
  ToolExecutionEvent,
  UIImage,
  UIMessage,
  UITraceCall,
} from "@/lib/types";

interface StreamBuffer {
  messageId: string;
  turnKey: string;
  streamId?: string;
  parts: string[];
}

const MAX_REMEMBERED_EVENTS = 128;
const TERMINAL_TOOL_PHASES = new Set([
  "end", "error", "cancelled", "rejected", "waiting_confirmation",
]);

function remember(set: Set<string>, key: string): void {
  set.add(key);
  if (set.size > MAX_REMEMBERED_EVENTS) {
    const oldest = set.values().next().value as string | undefined;
    if (oldest !== undefined) set.delete(oldest);
  }
}

function rememberValue<T>(map: Map<string, T>, key: string, value: T): void {
  map.set(key, value);
  if (map.size > MAX_REMEMBERED_EVENTS) {
    const oldest = map.keys().next().value as string | undefined;
    if (oldest !== undefined) map.delete(oldest);
  }
}

function validToolEvents(events: ToolExecutionEvent[] | undefined): ToolExecutionEvent[] {
  return (events ?? []).filter((event) =>
    event.version === 1 && typeof event.call_id === "string" && !!event.call_id &&
    typeof event.name === "string" && !!event.name,
  );
}

function toolCallLabel(call: UITraceCall): string {
  return `${call.name} · ${call.phase}`;
}

function appendTrace(
  messages: UIMessage[],
  text: string,
  events: ToolExecutionEvent[] | undefined,
  turnId: string | undefined,
  traceKey: string,
): UIMessage[] {
  const traceIndex = messages.findIndex((message) =>
    message.kind === "trace" && message.traceKey === traceKey,
  );
  const existingTrace = traceIndex >= 0 ? messages[traceIndex] : undefined;
  const previousCalls = existingTrace?.traceCalls ?? [];
  const calls = [...previousCalls];
  for (const event of validToolEvents(events)) {
    const index = calls.findIndex((call) => call.callId === event.call_id);
    const existing = index >= 0 ? calls[index] : undefined;
    if (existing && TERMINAL_TOOL_PHASES.has(existing.phase)) continue;
    const next: UITraceCall = {
      callId: event.call_id,
      name: event.name,
      phase: event.phase,
      executionStarted: !!event.execution_started,
      ...(event.error_kind ? { errorKind: event.error_kind } :
        existing?.errorKind ? { errorKind: existing.errorKind } : {}),
    };
    if (index >= 0) calls[index] = next;
    else calls.push(next);
  }

  const traces = [...(existingTrace?.traces ?? [])];
  const line = text.trim();
  if (line && !traces.includes(line)) traces.push(line);
  const visibleLines = [...traces, ...calls.map(toolCallLabel)];
  const trace: UIMessage = {
    ...(existingTrace ?? {}),
    id: existingTrace?.id ?? crypto.randomUUID(),
    role: "tool",
    kind: "trace",
    content: visibleLines.at(-1) ?? "",
    traces,
    ...(calls.length > 0 ? { traceCalls: calls } : {}),
    ...(turnId ? { turnId } : {}),
    traceKey,
    createdAt: existingTrace?.createdAt ?? Date.now(),
  };
  return existingTrace
    ? [...messages.slice(0, traceIndex), trace, ...messages.slice(traceIndex + 1)]
    : [...messages, trace];
}

/**
 * Subscribe to a chat by ID. Returns the in-memory message list for the chat,
 * a streaming flag, and a ``send`` function. Initial history must be seeded
 * separately (e.g. via ``fetchSessionMessages``) since the server only replays
 * live events.
 */
/** Payload passed to ``send`` when the user attaches one or more images.
 *
 * ``media`` is handed to the wire client verbatim; ``preview`` powers the
 * optimistic user bubble (blob URLs so the preview appears before the server
 * acks the frame). Keeping the two separate lets the bubble re-use the local
 * blob URL even after the server persists the file under a different name. */
export interface SendImage {
  media: OutboundMedia;
  preview: UIImage;
}

export interface SendOptions {
  imageGeneration?: OutboundImageGeneration;
}

export function useNanobotStream(
  chatId: string | null,
  initialMessages: UIMessage[] = [],
  hasPendingToolCalls = false,
  onTurnEnd?: () => void,
): {
  messages: UIMessage[];
  isStreaming: boolean;
  send: (content: string, images?: SendImage[], options?: SendOptions) => void;
  stop: () => void;
  setMessages: React.Dispatch<React.SetStateAction<UIMessage[]>>;
  hydrateHistory: (messages: UIMessage[], pending: boolean) => void;
  streamError: StreamError | null;
  dismissStreamError: () => void;
} {
  const { client } = useClient();
  const [messages, setMessages] = useState<UIMessage[]>(initialMessages);
  const initialStreaming = initialMessages.length > 0
    ? initialMessages[initialMessages.length - 1].kind === "trace"
    : false;
  const [isStreaming, setIsStreaming] = useState(initialStreaming || hasPendingToolCalls);
  const [streamError, setStreamError] = useState<StreamError | null>(null);
  const buffersRef = useRef(new Map<string, StreamBuffer>());
  const endedStreamsRef = useRef(new Set<string>());
  const endedTurnsRef = useRef(new Set<string>());
  const completedStreamMessagesRef = useRef(new Map<string, { messageId: string; content: string }>());
  const assistantMessageIdsRef = useRef(new Map<string, string>());
  const suppressedTurnsRef = useRef(new Set<string>());
  const activeTurnsRef = useRef(new Set<string>());
  const stoppedTurnsRef = useRef(new Set<string>());
  const currentTurnRef = useRef<string | null>(null);
  const legacyTurnNumberRef = useRef(0);
  const legacyTurnEndedRef = useRef(false);

  useEffect(() => client.onError(setStreamError), [client]);

  const dismissStreamError = useCallback(() => setStreamError(null), []);

  const hydrateHistory = useCallback((history: UIMessage[], pending: boolean) => {
    // History can complete a turn while this chat was not subscribed. A live
    // turn still owns its state until its own terminal event arrives.
    if (activeTurnsRef.current.size > 0) return;
    setMessages(history);
    setIsStreaming(pending);
  }, []);

  useEffect(() => {
    setMessages(initialMessages);
    setIsStreaming(
      (initialMessages.length > 0
        ? initialMessages[initialMessages.length - 1].kind === "trace"
        : false) || hasPendingToolCalls,
    );
    setStreamError(null);
    buffersRef.current.clear();
    endedStreamsRef.current.clear();
    endedTurnsRef.current.clear();
    completedStreamMessagesRef.current.clear();
    assistantMessageIdsRef.current.clear();
    suppressedTurnsRef.current.clear();
    activeTurnsRef.current.clear();
    stoppedTurnsRef.current.clear();
    currentTurnRef.current = null;
    legacyTurnNumberRef.current = 0;
    legacyTurnEndedRef.current = false;
    // Initial history can change independently of the chat ID.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId]);

  useEffect(() => {
    if (hasPendingToolCalls) setIsStreaming(true);
  }, [hasPendingToolCalls]);

  useEffect(() => {
    if (!chatId) return;

    const handle = (ev: InboundEvent) => {
      const turnId = ev.event === "delta" || ev.event === "stream_end" ||
        ev.event === "turn_end" || ev.event === "message" ? ev.turn_id : undefined;
      const isStartingContent = ev.event === "delta" ||
        (ev.event === "message" && ev.kind !== "tool_hint" && ev.kind !== "progress");
      if (isStartingContent && !turnId && legacyTurnEndedRef.current) {
        legacyTurnNumberRef.current += 1;
        legacyTurnEndedRef.current = false;
        currentTurnRef.current = null;
      }
      const turnKey = turnId
        ? `turn:${turnId}`
        : currentTurnRef.current ?? `legacy:${legacyTurnNumberRef.current}`;
      if (ev.event !== "turn_end") currentTurnRef.current = turnKey;
      if (stoppedTurnsRef.current.has(turnKey) &&
          (ev.event === "delta" || ev.event === "stream_end")) return;

      if (ev.event === "delta") {
        if (endedTurnsRef.current.has(turnKey) || suppressedTurnsRef.current.has(turnKey)) return;
        activeTurnsRef.current.add(turnKey);
        const streamKey = `${turnKey}\u0000${ev.stream_id ?? "legacy-stream"}`;
        if (endedStreamsRef.current.has(streamKey)) return;
        let buffer = buffersRef.current.get(streamKey);
        if (!buffer) {
          buffer = {
            messageId: crypto.randomUUID(),
            turnKey,
            ...(ev.stream_id ? { streamId: ev.stream_id } : {}),
            parts: [],
          };
          buffersRef.current.set(streamKey, buffer);
          setMessages((prev) => [
            ...prev,
            {
              id: buffer!.messageId,
              role: "assistant",
              content: "",
              isStreaming: true,
              ...(ev.turn_id ? { turnId: ev.turn_id } : {}),
              createdAt: Date.now(),
            },
          ]);
        }
        buffer.parts.push(ev.text);
        const combined = buffer.parts.join("");
        const messageId = buffer.messageId;
        setMessages((prev) => prev.map((message) =>
          message.id === messageId ? { ...message, content: combined } : message,
        ));
        setIsStreaming(true);
        return;
      }

      if (ev.event === "stream_end") {
        const streamKey = `${turnKey}\u0000${ev.stream_id ?? "legacy-stream"}`;
        if (endedStreamsRef.current.has(streamKey)) return;
        remember(endedStreamsRef.current, streamKey);
        const buffer = buffersRef.current.get(streamKey);
        buffersRef.current.delete(streamKey);
        if (!buffer) return;
        const completedText = buffer.parts.join("");
        if (!ev.resuming) {
          rememberValue(completedStreamMessagesRef.current, streamKey, {
            messageId: buffer.messageId,
            content: completedText,
          });
          return;
        }
        // The provider's pre-tool narration is useful progress, but it is not
        // a separate assistant answer. Move the completed segment into trace.
          setMessages((prev) => appendTrace(
          prev.filter((message) => message.id !== buffer.messageId),
          completedText,
          undefined,
          ev.turn_id,
          turnKey,
        ));
        return;
      }

      if (ev.event === "turn_end") {
        if (endedTurnsRef.current.has(turnKey)) return;
        remember(endedTurnsRef.current, turnKey);
        if (!ev.turn_id) legacyTurnEndedRef.current = true;
        const finalizedIds = new Set<string>();
        for (const [streamKey, buffer] of buffersRef.current) {
          if (buffer.turnKey === turnKey) {
            finalizedIds.add(buffer.messageId);
            buffersRef.current.delete(streamKey);
          }
        }
        suppressedTurnsRef.current.delete(turnKey);
        activeTurnsRef.current.delete(turnKey);
        setIsStreaming(activeTurnsRef.current.size > 0);
        setMessages((prev) => prev.map((message) =>
          message.isStreaming && (finalizedIds.has(message.id) ||
            (ev.turn_id ? message.turnId === ev.turn_id : true))
            ? { ...message, isStreaming: false }
            : message,
        ));
        onTurnEnd?.();
        return;
      }

      if (ev.event === "session_updated") {
        onTurnEnd?.();
        return;
      }

      if (ev.event !== "message") return;
      if (ev.kind === "tool_hint" || ev.kind === "progress") {
        const stopped = stoppedTurnsRef.current.has(turnKey);
        if (!stopped && (endedTurnsRef.current.has(turnKey) || suppressedTurnsRef.current.has(turnKey))) return;
        const events = validToolEvents(ev.tool_events);
        if (stopped) {
          const terminals = events.filter((event) => TERMINAL_TOOL_PHASES.has(event.phase));
          if (terminals.length) {
            setMessages((prev) => appendTrace(prev, "", terminals, turnId, turnKey));
          }
          return;
        }
        if (!ev.text.trim() && events.length === 0) return;
        activeTurnsRef.current.add(turnKey);
        setIsStreaming(true);
        setMessages((prev) => appendTrace(prev, ev.text, events, ev.turn_id, turnKey));
        return;
      }

      if (endedTurnsRef.current.has(turnKey) || stoppedTurnsRef.current.has(turnKey)) return;
      if (suppressedTurnsRef.current.has(turnKey) && !ev.message_id && !ev.stream_id) return;
      const media = ev.media_urls?.length
        ? ev.media_urls.map((item) => toMediaAttachment(item))
        : ev.media?.map((url) => toMediaAttachment({ url }));
      const hasMedia = !!media && media.length > 0;
      const content = ev.buttons?.length ? (ev.button_prompt ?? ev.text) : ev.text;
      const stableMessageKey = ev.message_id ? `${turnKey}\u0000${ev.message_id}` : undefined;
      const streamKey = ev.stream_id ? `${turnKey}\u0000${ev.stream_id}` : undefined;
      const existingMessageId = (stableMessageKey
        ? assistantMessageIdsRef.current.get(stableMessageKey)
        : undefined) ?? (streamKey
        ? completedStreamMessagesRef.current.get(streamKey)?.messageId ??
          buffersRef.current.get(streamKey)?.messageId
        : undefined);
      activeTurnsRef.current.add(turnKey);
      if (stableMessageKey && existingMessageId) {
        rememberValue(assistantMessageIdsRef.current, stableMessageKey, existingMessageId);
      }

      // Correlate a full message with its stream only by explicit stream ID.
      // Unrelated streams in the same turn must remain independent.
      const activeBuffer = streamKey ? buffersRef.current.get(streamKey) : undefined;
      if (streamKey) buffersRef.current.delete(streamKey);
      const newMessageId = existingMessageId ?? activeBuffer?.messageId ?? crypto.randomUUID();
      setMessages((prev) => {
        if (existingMessageId) {
          let found = false;
          const updated = prev.map((message) => {
            if (message.id !== existingMessageId) return message;
            found = true;
            return {
              ...message,
              content,
              isStreaming: false,
              ...(ev.turn_id ? { turnId: ev.turn_id } : {}),
              ...(ev.citations ? { citations: ev.citations } : {}),
              ...(ev.buttons && ev.buttons.length > 0 ? { buttons: ev.buttons } : {}),
              ...(hasMedia ? { media } : {}),
            };
          });
          return found ? updated : [...updated, {
            id: existingMessageId,
            role: "assistant",
            content,
            ...(ev.turn_id ? { turnId: ev.turn_id } : {}),
            citations: ev.citations,
            mailChatId: chatId,
            createdAt: Date.now(),
            ...(ev.buttons && ev.buttons.length > 0 ? { buttons: ev.buttons } : {}),
            ...(hasMedia ? { media } : {}),
          }];
        }
        const existingIndex = prev.findIndex((message) => message.id === newMessageId);
        const finalMessage: UIMessage = {
          ...(existingIndex >= 0 ? prev[existingIndex] : {}),
          id: newMessageId,
          role: "assistant",
          content,
          isStreaming: false,
          ...(ev.turn_id ? { turnId: ev.turn_id } : {}),
          citations: ev.citations,
          mailChatId: chatId,
          createdAt: existingIndex >= 0 ? prev[existingIndex].createdAt : Date.now(),
          ...(ev.buttons && ev.buttons.length > 0 ? { buttons: ev.buttons } : {}),
          ...(hasMedia ? { media } : {}),
        };
        return existingIndex >= 0
          ? prev.map((message, index) => index === existingIndex ? finalMessage : message)
          : [...prev, finalMessage];
      });
      if (stableMessageKey) rememberValue(assistantMessageIdsRef.current, stableMessageKey, newMessageId);
      if (streamKey) completedStreamMessagesRef.current.delete(streamKey);
      if (hasMedia) suppressedTurnsRef.current.add(turnKey);
    };

    const unsubscribe = client.onChat(chatId, handle);
    return () => {
      unsubscribe();
      buffersRef.current.clear();
    };
  }, [chatId, client, onTurnEnd]);

  const send = useCallback(
    (content: string, images?: SendImage[], options?: SendOptions) => {
      if (!chatId) return;
      const hasImages = !!images && images.length > 0;
      if (!hasImages && !content.trim()) return;

      const previews = hasImages ? images!.map((image) => image.preview) : undefined;
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "user",
          content,
          createdAt: Date.now(),
          ...(previews ? { images: previews } : {}),
        },
      ]);
      setIsStreaming(true);
      const wireMedia = hasImages ? images!.map((image) => image.media) : undefined;
      if (options) client.sendMessage(chatId, content, wireMedia, options);
      else client.sendMessage(chatId, content, wireMedia);
    },
    [chatId, client],
  );

  const stop = useCallback(() => {
    if (!chatId) return;
    for (const turnKey of activeTurnsRef.current) remember(stoppedTurnsRef.current, turnKey);
    activeTurnsRef.current.clear();
    buffersRef.current.clear();
    setIsStreaming(false);
    setMessages((prev) => prev.map((message) =>
      message.isStreaming ? { ...message, isStreaming: false } : message,
    ));
    client.sendMessage(chatId, "/stop");
  }, [chatId, client]);

  return {
    messages,
    isStreaming,
    send,
    stop,
    setMessages,
    hydrateHistory,
    streamError,
    dismissStreamError,
  };
}
