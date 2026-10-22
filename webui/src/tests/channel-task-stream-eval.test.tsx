import { afterAll, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import { useNanobotStream } from "@/hooks/useNanobotStream";
import { NanobotClient } from "@/lib/nanobot-client";
import type { InboundEvent, UIMessage } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

class EvalSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = EvalSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event?: { code?: number }) => void) | null = null;

  constructor(readonly url: string) {}

  send(raw: string): void {
    this.sent.push(raw);
  }

  close(): void {
    this.readyState = EvalSocket.CLOSED;
    this.onclose?.();
  }

  open(): void {
    this.readyState = EvalSocket.OPEN;
    this.onopen?.();
  }

  emit(frame: InboundEvent): void {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }
}

type Observation = Record<string, unknown>;
const observations: Observation[] = [];

function makeClient() {
  let socket: EvalSocket | null = null;
  const client = new NanobotClient({
    url: "ws://channel-task-stream-eval",
    reconnect: false,
    socketFactory: (url) => {
      socket = new EvalSocket(url);
      return socket as unknown as WebSocket;
    },
  });
  client.connect();
  if (!socket) throw new Error("client_did_not_create_socket");
  socket.open();
  return { client, socket };
}

function wrap(client: NanobotClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <ClientProvider client={client} token="eval-token">
        {children}
      </ClientProvider>
    );
  };
}

function assistantBodies(messages: UIMessage[]): string[] {
  return messages.filter((message) => message.role === "assistant").map((message) => message.content);
}

function emit(socket: EvalSocket, ...frames: InboundEvent[]): void {
  act(() => {
    for (const frame of frames) socket.emit(frame);
  });
}

afterAll(() => {
  const destination = process.env.NANOBOT_CHANNEL_STREAM_EVIDENCE;
  if (!destination) return;
  mkdirSync(dirname(destination), { recursive: true });
  writeFileSync(destination, `${JSON.stringify({ schema: "channel-task-stream-ui/1.0", cases: observations }, null, 2)}\n`, "utf8");
});

describe("V066 actual WebUI stream state", () => {
  it("replays tool-bound stream frames and keeps pre-tool text as progress", () => {
    const { client, socket } = makeClient();
    const onTurnEnd = vi.fn();
    const turnId = "v066-turn-1";
    const callId = "call_00_OYnfRxLFvBQuw83la5C41326";
    const { result, unmount } = renderHook(
      () => useNanobotStream("chat-owner-A", [], false, onTurnEnd),
      { wrapper: wrap(client) },
    );

    act(() => result.current.send("read fact.txt"));
    emit(socket,
      { event: "delta", chat_id: "chat-owner-A", turn_id: turnId, stream_id: "task-A:0", text: "I'll read the file for you." },
      { event: "stream_end", chat_id: "chat-owner-A", turn_id: turnId, stream_id: "task-A:0", resuming: true },
      { event: "message", chat_id: "chat-owner-A", turn_id: turnId, text: "read fact.txt", kind: "tool_hint" },
      { event: "message", chat_id: "chat-owner-A", turn_id: turnId, text: "", kind: "progress", tool_events: [{ version: 1, phase: "start", call_id: callId, name: "read_file", execution_started: true }] },
      { event: "message", chat_id: "chat-owner-A", turn_id: turnId, text: "", kind: "progress", tool_events: [{ version: 1, phase: "end", call_id: callId, name: "read_file", execution_started: true }] },
      { event: "delta", chat_id: "chat-owner-A", turn_id: turnId, text: "fact.txt contains 73", stream_id: "task-A:1" },
      { event: "stream_end", chat_id: "chat-owner-A", turn_id: turnId, stream_id: "task-A:1" },
      { event: "turn_end", chat_id: "chat-owner-A", turn_id: turnId },
    );

    const messages = result.current.messages;
    const visibleAnswers = assistantBodies(messages);
    const traces = messages.filter((message) => message.kind === "trace");
    const observation = {
      case: "v066_wire_contract_replay",
      owner_id: "owner-A",
      chat_id: "chat-owner-A",
      runner_final: "fact.txt contains 73",
      frames: ["delta:task-A:0:pre-tool", "stream_end:task-A:0:resuming", "tool_hint:read_file", `tool_event:start:${callId}`, `tool_event:end:${callId}`, "delta:task-A:1:final", "stream_end:task-A:1", "turn_end"],
      visible_answers: visibleAnswers,
      visible_trace_rows: traces.map((message) => ({ content: message.content, traces: message.traces, trace_calls: message.traceCalls })),
      turn_end_callbacks: onTurnEnd.mock.calls.length,
      contract_pass: visibleAnswers.length === 1 && visibleAnswers[0] === "fact.txt contains 73",
      product_findings: [],
    };
    observations.push(observation);

    expect(visibleAnswers).toEqual(["fact.txt contains 73"]);
    expect(traces.flatMap((message) => message.traces ?? [])).toContain("I'll read the file for you.");
    expect(traces.flatMap((message) => message.traceCalls ?? []).map((call) => call.callId)).toEqual([callId]);
    expect(onTurnEnd).toHaveBeenCalledTimes(1);
    unmount();
    client.close();
  });

  it.skipIf(!process.env.NANOBOT_CHANNEL_STREAM_UI_INPUT)(
    "replays captured worker frames without repairing the archived wire",
    () => {
      const inputPath = process.env.NANOBOT_CHANNEL_STREAM_UI_INPUT!;
      const input = JSON.parse(readFileSync(inputPath, "utf8")) as {
        task_id: string;
        owner_id: string;
        chat_id: string;
        runner_final: string;
        wire_frames: InboundEvent[];
      };
      const { client, socket } = makeClient();
      const onTurnEnd = vi.fn();
      const { result, unmount } = renderHook(
        () => useNanobotStream(input.chat_id, [], false, onTurnEnd),
        { wrapper: wrap(client) },
      );
      emit(socket, ...input.wire_frames);

      const visibleAnswers = assistantBodies(result.current.messages);
      const hasTurnOwnership = input.wire_frames.some((frame) => "turn_id" in frame && !!frame.turn_id);
      const hasResumingBoundary = input.wire_frames.some((frame) =>
        frame.event === "stream_end" && typeof frame.resuming === "boolean",
      );
      const compatibleWire = hasTurnOwnership && hasResumingBoundary;
      const observation = {
        case: "captured_worker_wire_frames",
        task_id: input.task_id,
        owner_id: input.owner_id,
        chat_id: input.chat_id,
        runner_final: input.runner_final,
        wire_frames: input.wire_frames,
        visible_answers: visibleAnswers,
        visible_trace_rows: result.current.messages.filter((message) => message.kind === "trace")
          .map(({ role, content, traces, traceCalls }) => ({
            role, content, traces, trace_calls: traceCalls,
          })),
        turn_end_callbacks: onTurnEnd.mock.calls.length,
        contract_pass: compatibleWire && visibleAnswers.length === 1 && visibleAnswers[0] === input.runner_final,
        product_findings: compatibleWire ? [] : ["archived_wire_missing_turn_id_or_stream_end_resuming"],
      };
      observations.push(observation);

      if (compatibleWire) expect(visibleAnswers).toEqual([input.runner_final]);
      else expect(visibleAnswers).not.toEqual([input.runner_final]);
      expect(onTurnEnd).toHaveBeenCalledTimes(1);
      unmount();
      client.close();
    },
  );

  it("keeps interleaved stream IDs in separate assistant messages", () => {
    const { client, socket } = makeClient();
    const { result, unmount } = renderHook(
      () => useNanobotStream("chat-owner-A", []),
      { wrapper: wrap(client) },
    );

    emit(socket,
      { event: "delta", chat_id: "chat-owner-A", turn_id: "interleaved-turn", text: "结果为", stream_id: "stream-A" },
      { event: "delta", chat_id: "chat-owner-A", turn_id: "interleaved-turn", text: "foreign", stream_id: "stream-B" },
      { event: "delta", chat_id: "chat-owner-A", turn_id: "interleaved-turn", text: "73", stream_id: "stream-A" },
      { event: "stream_end", chat_id: "chat-owner-A", turn_id: "interleaved-turn", stream_id: "stream-A" },
      { event: "stream_end", chat_id: "chat-owner-A", turn_id: "interleaved-turn", stream_id: "stream-B" },
      { event: "turn_end", chat_id: "chat-owner-A", turn_id: "interleaved-turn" },
    );

    const visibleAnswers = assistantBodies(result.current.messages);
    const observation = {
      case: "interleaved_stream_ids_negative_control",
      owner_id: "owner-A",
      chat_id: "chat-owner-A",
      stream_ids: ["stream-A", "stream-B"],
      visible_answers: visibleAnswers,
      expected_per_stream: { "stream-A": "结果为73", "stream-B": "foreign" },
      contract_pass: visibleAnswers.length === 2 && visibleAnswers[0] === "结果为73" && visibleAnswers[1] === "foreign",
      product_findings: [],
    };
    observations.push(observation);

    expect(visibleAnswers).toEqual(["结果为73", "foreign"]);
    expect(observation.contract_pass).toBe(true);
    unmount();
    client.close();
  });

  it("makes duplicate stream and turn terminals idempotent", () => {
    const { client, socket } = makeClient();
    const onTurnEnd = vi.fn();
    const { result, unmount } = renderHook(
      () => useNanobotStream("chat-owner-A", [], false, onTurnEnd),
      { wrapper: wrap(client) },
    );

    emit(socket,
      { event: "delta", chat_id: "chat-owner-A", turn_id: "duplicate-turn", text: "结果为", stream_id: "stream-A" },
      { event: "delta", chat_id: "chat-owner-A", turn_id: "duplicate-turn", text: "73", stream_id: "stream-A" },
      { event: "stream_end", chat_id: "chat-owner-A", turn_id: "duplicate-turn", stream_id: "stream-A" },
      { event: "stream_end", chat_id: "chat-owner-A", turn_id: "duplicate-turn", stream_id: "stream-A" },
      { event: "message", chat_id: "chat-owner-A", turn_id: "duplicate-turn", message_id: "duplicate-turn:final", stream_id: "stream-A", text: "结果为73" },
      { event: "message", chat_id: "chat-owner-A", turn_id: "duplicate-turn", message_id: "duplicate-turn:final", stream_id: "stream-A", text: "结果为73" },
      { event: "turn_end", chat_id: "chat-owner-A", turn_id: "duplicate-turn" },
      { event: "turn_end", chat_id: "chat-owner-A", turn_id: "duplicate-turn" },
    );

    const visibleAnswers = assistantBodies(result.current.messages);
    const observation = {
      case: "duplicate_terminal_and_final_negative_control",
      owner_id: "owner-A",
      chat_id: "chat-owner-A",
      visible_answers: visibleAnswers,
      turn_end_callbacks: onTurnEnd.mock.calls.length,
      contract_pass: visibleAnswers.length === 1 && visibleAnswers[0] === "结果为73" && onTurnEnd.mock.calls.length === 1,
      product_findings: [],
    };
    observations.push(observation);

    expect(visibleAnswers).toEqual(["结果为73"]);
    expect(onTurnEnd).toHaveBeenCalledTimes(1);
    expect(observation.contract_pass).toBe(true);
    unmount();
    client.close();
  });

  it("routes wrong-chat frames only to the matching hook owner", () => {
    const { client, socket } = makeClient();
    const ownerA = renderHook(() => useNanobotStream("chat-owner-A", []), { wrapper: wrap(client) });
    const ownerB = renderHook(() => useNanobotStream("chat-owner-B", []), { wrapper: wrap(client) });

    emit(socket,
      { event: "message", chat_id: "chat-owner-B", text: "B-only progress", kind: "tool_hint" },
      { event: "delta", chat_id: "chat-owner-B", text: "B-only answer", stream_id: "task-B:0" },
      { event: "stream_end", chat_id: "chat-owner-B", stream_id: "task-B:0" },
      { event: "turn_end", chat_id: "chat-owner-B" },
    );

    const aMessages = ownerA.result.current.messages;
    const bMessages = ownerB.result.current.messages;
    const observation = {
      case: "wrong_chat_owner_negative_control",
      frame_owner_id: "owner-B",
      frame_chat_id: "chat-owner-B",
      listener_chat_ids: ["chat-owner-A", "chat-owner-B"],
      owner_a_messages: aMessages,
      owner_b_messages: bMessages.map(({ role, kind, content }) => ({ role, kind, content })),
      contract_pass: assistantBodies(aMessages).length === 0 && assistantBodies(bMessages).length === 1,
      product_findings: [],
    };
    observations.push(observation);

    expect(aMessages).toEqual([]);
    expect(assistantBodies(bMessages)).toEqual(["B-only answer"]);
    expect(observation.contract_pass).toBe(true);
    ownerA.unmount();
    ownerB.unmount();
    client.close();
  });
});

describe("V074 helper progress", () => {
  it("counts unique executed call IDs across tool progress and final stream", () => {
    const { client, socket } = makeClient();
    const turnId = "v074-turn-1";
    const readId = "call_00_EjIpqHA2VZnaZHsR0BDN6168";
    const editId = "call_00_bPt56NY74moXCkO7YbjP8160";
    const { result, unmount } = renderHook(
      () => useNanobotStream("chat-v074", []),
      { wrapper: wrap(client) },
    );

    emit(socket,
      { event: "delta", chat_id: "chat-v074", turn_id: turnId, stream_id: "v074:0", text: "I'll update the config." },
      { event: "stream_end", chat_id: "chat-v074", turn_id: turnId, stream_id: "v074:0", resuming: true },
      { event: "message", chat_id: "chat-v074", turn_id: turnId, text: "read config.json", kind: "tool_hint" },
      { event: "message", chat_id: "chat-v074", turn_id: turnId, text: "", kind: "progress", tool_events: [{ version: 1, phase: "start", call_id: readId, name: "read_file", execution_started: true }] },
      { event: "message", chat_id: "chat-v074", turn_id: turnId, text: "", kind: "progress", tool_events: [{ version: 1, phase: "end", call_id: readId, name: "read_file", execution_started: true }] },
      { event: "delta", chat_id: "chat-v074", turn_id: turnId, stream_id: "v074:1", text: "I'll now edit config.json." },
      { event: "stream_end", chat_id: "chat-v074", turn_id: turnId, stream_id: "v074:1", resuming: true },
      { event: "message", chat_id: "chat-v074", turn_id: turnId, text: "edit config.json", kind: "tool_hint" },
      { event: "message", chat_id: "chat-v074", turn_id: turnId, text: "", kind: "progress", tool_events: [{ version: 1, phase: "start", call_id: editId, name: "edit_file", execution_started: true }] },
      { event: "message", chat_id: "chat-v074", turn_id: turnId, text: "", kind: "progress", tool_events: [
        { version: 1, phase: "end", call_id: editId, name: "edit_file", execution_started: true },
        { version: 1, phase: "end", call_id: readId, name: "read_file", execution_started: true },
      ] },
      { event: "delta", chat_id: "chat-v074", turn_id: turnId, stream_id: "v074:2", text: "{\"timeout\":60,\"retries\":3}" },
      { event: "stream_end", chat_id: "chat-v074", turn_id: turnId, stream_id: "v074:2" },
      { event: "turn_end", chat_id: "chat-v074", turn_id: turnId },
    );

    const messages = result.current.messages;
    const trace = messages.find((message) => message.kind === "trace");
    const calls = trace?.traceCalls ?? [];
    const answers = assistantBodies(messages);
    const observation = {
      case: "v074_tool_events_bound_to_actual_call_ids",
      turn_id: turnId,
      tool_call_ids: calls.map((call) => call.callId),
      phases: calls.map((call) => call.phase),
      execution_started: calls.map((call) => call.executionStarted),
      visible_progress_lines: trace?.traces,
      visible_answers: answers,
      actual_execution_count: new Set(calls.filter((call) => call.executionStarted).map((call) => call.callId)).size,
      contract_pass: calls.length === 2 && answers.length === 1 && answers[0] === "{\"timeout\":60,\"retries\":3}",
      product_findings: [],
    };
    observations.push(observation);

    expect(trace?.traces).toHaveLength(4);
    expect(calls.map((call) => call.callId)).toEqual([readId, editId]);
    expect(new Set(calls.filter((call) => call.executionStarted).map((call) => call.callId)).size).toBe(2);
    expect(answers).toEqual(["{\"timeout\":60,\"retries\":3}"]);
    expect(observation.contract_pass).toBe(true);
    unmount();
    client.close();
  });
});
