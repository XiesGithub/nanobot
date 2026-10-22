import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { useNanobotStream } from "@/hooks/useNanobotStream";
import type { InboundEvent } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const EMPTY_MESSAGES: import("@/lib/types").UIMessage[] = [];

function fakeClient() {
  const handlers = new Map<string, Set<(ev: InboundEvent) => void>>();
  return {
    client: {
      status: "open" as const,
      defaultChatId: null as string | null,
      onStatus: () => () => {},
      onError: () => () => {},
      onChat(chatId: string, h: (ev: InboundEvent) => void) {
        let set = handlers.get(chatId);
        if (!set) {
          set = new Set();
          handlers.set(chatId, set);
        }
        set.add(h);
        return () => set!.delete(h);
      },
      sendMessage: vi.fn(),
      newChat: vi.fn(),
      attach: vi.fn(),
      connect: vi.fn(),
      close: vi.fn(),
      updateUrl: vi.fn(),
    },
    emit(chatId: string, ev: InboundEvent) {
      const set = handlers.get(chatId);
      set?.forEach((h) => h(ev));
    },
  };
}

function wrap(client: ReturnType<typeof fakeClient>["client"]) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <ClientProvider
        client={client as unknown as import("@/lib/nanobot-client").NanobotClient}
        token="tok"
      >
        {children}
      </ClientProvider>
    );
  };
}

describe("useNanobotStream", () => {
  it("starts in streaming mode when history shows pending tool calls", () => {
    const fake = fakeClient();
    const initialMessages = [{
      id: "m1",
      role: "assistant" as const,
      content: "Using tools",
      createdAt: Date.now(),
    }];
    const { result } = renderHook(
      () => useNanobotStream("chat-p", initialMessages, true),
      {
        wrapper: wrap(fake.client),
      },
    );

    expect(result.current.isStreaming).toBe(true);
  });

  it("collapses consecutive tool_hint frames into one trace row", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-t", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-t", {
        event: "message",
        chat_id: "chat-t",
        text: 'weather("get")',
        kind: "tool_hint",
      });
      fake.emit("chat-t", {
        event: "message",
        chat_id: "chat-t",
        text: 'search "hk weather"',
        kind: "tool_hint",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].kind).toBe("trace");
    expect(result.current.messages[0].role).toBe("tool");
    expect(result.current.messages[0].traces).toEqual([
      'weather("get")',
      'search "hk weather"',
    ]);

    act(() => {
      fake.emit("chat-t", {
        event: "message",
        chat_id: "chat-t",
        text: "## Summary",
      });
    });

    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[1].role).toBe("assistant");
    expect(result.current.messages[1].kind).toBeUndefined();
  });

  it("attaches assistant media_urls to complete messages", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-m", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-m", {
        event: "message",
        chat_id: "chat-m",
        text: "video ready",
        media_urls: [{ url: "/api/media/sig/payload", name: "demo.mp4" }],
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].media).toEqual([
      { kind: "video", url: "/api/media/sig/payload", name: "demo.mp4" },
    ]);
  });

  it("suppresses redundant stream confirmation after assistant media", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-img-result", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-img-result", {
        event: "message",
        chat_id: "chat-img-result",
        text: "image ready",
        media_urls: [{ url: "/api/media/sig/image", name: "generated.png" }],
      });
      fake.emit("chat-img-result", {
        event: "message",
        chat_id: "chat-img-result",
        text: "message()",
        kind: "tool_hint",
      });
      fake.emit("chat-img-result", {
        event: "delta",
        chat_id: "chat-img-result",
        text: "发送成功",
      });
      fake.emit("chat-img-result", {
        event: "stream_end",
        chat_id: "chat-img-result",
      });
      fake.emit("chat-img-result", {
        event: "turn_end",
        chat_id: "chat-img-result",
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("image ready");
    expect(result.current.messages[0].media).toHaveLength(1);
  });

  it("passes image generation options to the websocket client", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-img", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      result.current.send(
        "draw a square icon",
        undefined,
        { imageGeneration: { enabled: true, aspect_ratio: "1:1" } },
      );
    });

    expect(fake.client.sendMessage).toHaveBeenCalledWith(
      "chat-img",
      "draw a square icon",
      undefined,
      { imageGeneration: { enabled: true, aspect_ratio: "1:1" } },
    );
  });

  it("stops the active turn without adding a user slash command bubble", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-stop", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      result.current.send("long task");
    });
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.isStreaming).toBe(true);

    act(() => {
      result.current.stop();
    });

    expect(fake.client.sendMessage).toHaveBeenLastCalledWith("chat-stop", "/stop");
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("long task");
  });

  it.each([true, false])("finishes a continued turn after cancellation (executed=%s)", (executionStarted) => {
    const fake = fakeClient();
    const chatId = "chat-stop-resume";
    const { result } = renderHook(() => useNanobotStream(chatId, EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });
    act(() => {
      result.current.send("read then wait");
      fake.emit(chatId, { event: "message", chat_id: chatId, turn_id: "stopped-turn",
        kind: "progress", text: "gate waiting", tool_events: [
          { version: 1, call_id: "gate-1", name: "gate", phase: "start", execution_started: true },
        ] });
    });
    act(() => result.current.stop());
    act(() => fake.emit(chatId, { event: "message", chat_id: chatId, turn_id: "stopped-turn",
      kind: "progress", text: "", tool_events: [
        { version: 1, call_id: "gate-1", name: "gate", phase: "cancelled", execution_started: executionStarted },
      ] }));
    expect(result.current.isStreaming).toBe(false);
    act(() => {
      result.current.send("continue");
      fake.emit(chatId, { event: "delta", chat_id: chatId, turn_id: "continued-turn",
        stream_id: "continued-stream", text: "result=73" });
      fake.emit(chatId, { event: "stream_end", chat_id: chatId, turn_id: "continued-turn",
        stream_id: "continued-stream", resuming: false });
      fake.emit(chatId, { event: "turn_end", chat_id: chatId, turn_id: "continued-turn" });
      fake.emit(chatId, { event: "delta", chat_id: chatId, turn_id: "stopped-turn",
        stream_id: "late-old-stream", text: "late old text" });
    });
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.messages.filter((message) => message.role === "assistant")
      .map((message) => message.content)).toEqual(["result=73"]);
    expect(result.current.messages.find((message) => message.kind === "trace")?.traceCalls?.[0].phase)
      .toBe("cancelled");
    expect(result.current.messages.find((message) => message.kind === "trace")?.traceCalls?.[0].executionStarted)
      .toBe(executionStarted);
  });

  it("keeps assistant buttons on complete messages", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-q", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-q", {
        event: "message",
        chat_id: "chat-q",
        text: "How should I continue?\n\n1. Short answer\n2. Detailed answer",
        button_prompt: "How should I continue?",
        buttons: [["Short answer", "Detailed answer"]],
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("How should I continue?");
    expect(result.current.messages[0].buttons).toEqual([
      ["Short answer", "Detailed answer"],
    ]);
  });

  it("keeps streaming alive across stream_end and completes on turn_end", () => {
    const fake = fakeClient();
    const onTurnEnd = vi.fn();
    const { result } = renderHook(() => useNanobotStream("chat-s", EMPTY_MESSAGES, false, onTurnEnd), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-s", {
        event: "delta",
        chat_id: "chat-s",
        text: "Hello",
      });
    });

    expect(result.current.isStreaming).toBe(true);
    expect(result.current.messages[0]).toMatchObject({
      role: "assistant",
      content: "Hello",
      isStreaming: true,
    });

    act(() => {
      fake.emit("chat-s", {
        event: "stream_end",
        chat_id: "chat-s",
      });
    });

    expect(result.current.isStreaming).toBe(true);
    expect(result.current.messages[0].isStreaming).toBe(true);

    act(() => {
      fake.emit("chat-s", {
        event: "message",
        chat_id: "chat-s",
        text: "Hello world",
      });
    });

    expect(result.current.isStreaming).toBe(true);
    expect(result.current.messages.at(-1)).toMatchObject({
      role: "assistant",
      content: "Hello world",
    });

    act(() => {
      fake.emit("chat-s", {
        event: "turn_end",
        chat_id: "chat-s",
      });
    });

    expect(result.current.isStreaming).toBe(false);
    expect(result.current.messages.every((message) => !message.isStreaming)).toBe(true);
    expect(onTurnEnd).toHaveBeenCalledTimes(1);
  });

  it("replays the V066 wire order and keeps pre-tool text out of assistant answers", () => {
    const fake = fakeClient();
    const turnId = "turn-v066-1";
    const callId = "call-v066-read-1";
    const { result } = renderHook(() => useNanobotStream("chat-v066", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-v066", {
        event: "delta", chat_id: "chat-v066", turn_id: turnId,
        stream_id: "stream-v066-0", text: "I'll read the file for you.",
      });
      fake.emit("chat-v066", {
        event: "stream_end", chat_id: "chat-v066", turn_id: turnId,
        stream_id: "stream-v066-0", resuming: true,
      });
      fake.emit("chat-v066", {
        event: "message", chat_id: "chat-v066", turn_id: turnId,
        text: "read fact.txt", kind: "tool_hint",
      });
      fake.emit("chat-v066", {
        event: "message", chat_id: "chat-v066", turn_id: turnId, text: "",
        kind: "progress",
        tool_events: [{
          version: 1, phase: "start", call_id: callId, name: "read_file",
          execution_started: true,
        }],
      });
      fake.emit("chat-v066", {
        event: "message", chat_id: "chat-v066", turn_id: turnId, text: "",
        kind: "progress",
        tool_events: [{
          version: 1, phase: "end", call_id: callId, name: "read_file",
          execution_started: true,
        }],
      });
      fake.emit("chat-v066", {
        event: "delta", chat_id: "chat-v066", turn_id: turnId,
        stream_id: "stream-v066-1", text: "fact.txt contains 73.",
      });
      fake.emit("chat-v066", {
        event: "stream_end", chat_id: "chat-v066", turn_id: turnId,
        stream_id: "stream-v066-1",
      });
      fake.emit("chat-v066", { event: "turn_end", chat_id: "chat-v066", turn_id: turnId });
    });

    expect(result.current.messages.filter((message) => message.role === "assistant")
      .map((message) => message.content)).toEqual(["fact.txt contains 73."]);
    const trace = result.current.messages.find((message) => message.kind === "trace");
    expect(trace?.traces).toContain("I'll read the file for you.");
    expect(trace?.traces).toContain("read fact.txt");
    expect(trace?.traceCalls).toEqual([{
      callId,
      name: "read_file",
      phase: "end",
      executionStarted: true,
    }]);
  });

  it("isolates interleaved stream IDs and ignores duplicate stream and turn terminals", () => {
    const fake = fakeClient();
    const onTurnEnd = vi.fn();
    const turnId = "turn-interleaved";
    const { result } = renderHook(
      () => useNanobotStream("chat-interleaved", EMPTY_MESSAGES, false, onTurnEnd),
      { wrapper: wrap(fake.client) },
    );

    act(() => {
      fake.emit("chat-interleaved", {
        event: "delta", chat_id: "chat-interleaved", turn_id: turnId,
        stream_id: "stream-A", text: "answer ",
      });
      fake.emit("chat-interleaved", {
        event: "delta", chat_id: "chat-interleaved", turn_id: turnId,
        stream_id: "stream-B", text: "foreign",
      });
      fake.emit("chat-interleaved", {
        event: "delta", chat_id: "chat-interleaved", turn_id: turnId,
        stream_id: "stream-A", text: "73",
      });
      fake.emit("chat-interleaved", {
        event: "stream_end", chat_id: "chat-interleaved", turn_id: turnId,
        stream_id: "stream-A",
      });
      fake.emit("chat-interleaved", {
        event: "stream_end", chat_id: "chat-interleaved", turn_id: turnId,
        stream_id: "stream-A",
      });
      fake.emit("chat-interleaved", {
        event: "stream_end", chat_id: "chat-interleaved", turn_id: turnId,
        stream_id: "stream-B",
      });
      fake.emit("chat-interleaved", {
        event: "turn_end", chat_id: "chat-interleaved", turn_id: turnId,
      });
      fake.emit("chat-interleaved", {
        event: "turn_end", chat_id: "chat-interleaved", turn_id: turnId,
      });
    });

    expect(result.current.messages.filter((message) => message.role === "assistant")
      .map((message) => message.content)).toEqual(["answer 73", "foreign"]);
    expect(onTurnEnd).toHaveBeenCalledTimes(1);
  });

  it("counts V074 tool progress by executed call ID, not visible trace rows", () => {
    const fake = fakeClient();
    const turnId = "turn-v074-1";
    const readId = "call-v074-read-1";
    const editId = "call-v074-edit-1";
    const { result } = renderHook(() => useNanobotStream("chat-v074", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-v074", {
        event: "delta", chat_id: "chat-v074", turn_id: turnId,
        stream_id: "stream-v074-0", text: "I'll update config.json.",
      });
      fake.emit("chat-v074", {
        event: "stream_end", chat_id: "chat-v074", turn_id: turnId,
        stream_id: "stream-v074-0", resuming: true,
      });
      fake.emit("chat-v074", {
        event: "message", chat_id: "chat-v074", turn_id: turnId,
        text: "read config.json", kind: "tool_hint",
      });
      fake.emit("chat-v074", {
        event: "message", chat_id: "chat-v074", turn_id: turnId, text: "",
        kind: "progress",
        tool_events: [
          { version: 1, phase: "start", call_id: readId, name: "read_file", execution_started: true },
          { version: 1, phase: "end", call_id: readId, name: "read_file", execution_started: true },
        ],
      });
      fake.emit("chat-v074", {
        event: "message", chat_id: "chat-v074", turn_id: turnId,
        text: "edit_file config.json", kind: "tool_hint",
      });
      fake.emit("chat-v074", {
        event: "message", chat_id: "chat-v074", turn_id: turnId, text: "",
        kind: "progress",
        tool_events: [
          { version: 1, phase: "start", call_id: editId, name: "edit_file", execution_started: true },
          { version: 1, phase: "end", call_id: editId, name: "edit_file", execution_started: true },
          { version: 1, phase: "end", call_id: readId, name: "read_file", execution_started: true },
        ],
      });
      fake.emit("chat-v074", {
        event: "delta", chat_id: "chat-v074", turn_id: turnId,
        stream_id: "stream-v074-1", text: "timeout=60, retries=3",
      });
      fake.emit("chat-v074", {
        event: "stream_end", chat_id: "chat-v074", turn_id: turnId,
        stream_id: "stream-v074-1",
      });
      fake.emit("chat-v074", { event: "turn_end", chat_id: "chat-v074", turn_id: turnId });
    });

    const trace = result.current.messages.find((message) => message.kind === "trace");
    expect(trace?.traces).toHaveLength(3);
    expect(trace?.traceCalls?.map((call) => call.callId)).toEqual([readId, editId]);
    expect(trace?.traceCalls?.filter((call) => call.executionStarted)).toHaveLength(2);
    expect(result.current.messages.filter((message) => message.role === "assistant")
      .map((message) => message.content)).toEqual(["timeout=60, retries=3"]);
  });

  it("lets a terminal cancellation retract a pre-execution notification", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-cancel", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });
    const toolEvent = (phase: "start" | "cancelled", execution_started: boolean) => ({
      version: 1 as const,
      phase,
      call_id: "call-cancel-1",
      name: "edit_file",
      execution_started,
    });

    act(() => {
      fake.emit("chat-cancel", {
        event: "message", chat_id: "chat-cancel", turn_id: "cancel-turn", text: "",
        kind: "progress", tool_events: [toolEvent("start", true)],
      });
      fake.emit("chat-cancel", {
        event: "message", chat_id: "chat-cancel", turn_id: "cancel-turn", text: "",
        kind: "progress", tool_events: [toolEvent("cancelled", false)],
      });
      fake.emit("chat-cancel", {
        event: "message", chat_id: "chat-cancel", turn_id: "cancel-turn", text: "",
        kind: "progress", tool_events: [toolEvent("start", true)],
      });
    });

    expect(result.current.messages.find((message) => message.kind === "trace")?.traceCalls)
      .toMatchObject([{ phase: "cancelled", executionStarted: false }]);
  });

  it("keeps a completed tool terminal stable against a conflicting late terminal", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-terminal", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });
    const event = (phase: "start" | "end" | "cancelled", execution_started: boolean) => ({
      version: 1 as const,
      phase,
      call_id: "call-terminal-1",
      name: "read_file",
      execution_started,
    });

    act(() => {
      fake.emit("chat-terminal", {
        event: "message", chat_id: "chat-terminal", turn_id: "terminal-turn", text: "",
        kind: "progress", tool_events: [event("start", true)],
      });
      fake.emit("chat-terminal", {
        event: "message", chat_id: "chat-terminal", turn_id: "terminal-turn", text: "",
        kind: "progress", tool_events: [event("end", true)],
      });
      fake.emit("chat-terminal", {
        event: "message", chat_id: "chat-terminal", turn_id: "terminal-turn", text: "",
        kind: "progress", tool_events: [event("cancelled", false)],
      });
    });

    expect(result.current.messages.find((message) => message.kind === "trace")?.traceCalls)
      .toMatchObject([{ phase: "end", executionStarted: true }]);
  });

  it("merges only explicitly identified finals and preserves citation and media metadata", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-identities", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });
    const citation = { evidence_id: "source-1", filename: "report.txt" };
    const finalMessage = {
      event: "message" as const,
      chat_id: "chat-identities",
      turn_id: "identity-turn",
      message_id: "identity-turn:final",
      stream_id: "identity-stream",
      text: "same final text",
      citations: [citation],
      media_urls: [{ url: "/api/media/signed/item", name: "clip.mp4" }],
    };

    act(() => {
      fake.emit("chat-identities", {
        event: "message", chat_id: "chat-identities", text: "same final text",
      });
      fake.emit("chat-identities", {
        event: "message", chat_id: "chat-identities", text: "same final text",
      });
      fake.emit("chat-identities", {
        event: "delta", chat_id: "chat-identities", turn_id: "identity-turn",
        stream_id: "identity-stream", text: "same final text",
      });
      fake.emit("chat-identities", {
        event: "stream_end", chat_id: "chat-identities", turn_id: "identity-turn",
        stream_id: "identity-stream",
      });
      fake.emit("chat-identities", finalMessage);
      fake.emit("chat-identities", finalMessage);
    });

    const answers = result.current.messages.filter((message) => message.role === "assistant");
    expect(answers).toHaveLength(3);
    expect(answers.map((message) => message.content)).toEqual([
      "same final text", "same final text", "same final text",
    ]);
    expect(answers[2].citations).toEqual([citation]);
    expect(answers[2].media).toEqual([
      { kind: "video", url: "/api/media/signed/item", name: "clip.mp4" },
    ]);
  });

  it("keeps interleaved turns and trace rows isolated until each turn ends", () => {
    const fake = fakeClient();
    const { result } = renderHook(() => useNanobotStream("chat-turns", EMPTY_MESSAGES), {
      wrapper: wrap(fake.client),
    });
    const event = (callId: string, phase: "start" | "end") => ({
      version: 1 as const,
      phase,
      call_id: callId,
      name: "read_file",
      execution_started: true,
    });

    act(() => {
      fake.emit("chat-turns", {
        event: "delta", chat_id: "chat-turns", turn_id: "turn-A",
        stream_id: "turn-A:0", text: "A preface",
      });
      fake.emit("chat-turns", {
        event: "stream_end", chat_id: "chat-turns", turn_id: "turn-A",
        stream_id: "turn-A:0", resuming: true,
      });
      fake.emit("chat-turns", {
        event: "message", chat_id: "chat-turns", turn_id: "turn-A", text: "",
        kind: "progress", tool_events: [event("call-A", "start")],
      });
      fake.emit("chat-turns", {
        event: "delta", chat_id: "chat-turns", turn_id: "turn-B",
        stream_id: "turn-B:0", text: "B answer",
      });
      fake.emit("chat-turns", {
        event: "message", chat_id: "chat-turns", turn_id: "turn-B",
        text: "B hint", kind: "tool_hint",
      });
      fake.emit("chat-turns", {
        event: "message", chat_id: "chat-turns", turn_id: "turn-A", text: "",
        kind: "progress", tool_events: [event("call-A", "end")],
      });
      fake.emit("chat-turns", {
        event: "delta", chat_id: "chat-turns", turn_id: "turn-A",
        stream_id: "turn-A:1", text: "A answer",
      });
      fake.emit("chat-turns", {
        event: "stream_end", chat_id: "chat-turns", turn_id: "turn-A",
        stream_id: "turn-A:1",
      });
      fake.emit("chat-turns", { event: "turn_end", chat_id: "chat-turns", turn_id: "turn-A" });
    });

    expect(result.current.isStreaming).toBe(true);
    const traces = result.current.messages.filter((message) => message.kind === "trace");
    expect(traces).toHaveLength(2);
    expect(traces.find((message) => message.turnId === "turn-A")?.traces).toContain("A preface");
    expect(traces.find((message) => message.turnId === "turn-B")?.traces).toContain("B hint");
    expect(traces.find((message) => message.turnId === "turn-A")?.traceCalls)
      .toMatchObject([{ callId: "call-A", phase: "end", executionStarted: true }]);

    act(() => {
      fake.emit("chat-turns", { event: "turn_end", chat_id: "chat-turns", turn_id: "turn-B" });
    });
    expect(result.current.isStreaming).toBe(false);
  });

  it("refreshes session metadata when the server reports a session update", () => {
    const fake = fakeClient();
    const onTurnEnd = vi.fn();
    renderHook(() => useNanobotStream("chat-title", EMPTY_MESSAGES, false, onTurnEnd), {
      wrapper: wrap(fake.client),
    });

    act(() => {
      fake.emit("chat-title", {
        event: "session_updated",
        chat_id: "chat-title",
      });
    });

    expect(onTurnEnd).toHaveBeenCalledTimes(1);
  });
});
