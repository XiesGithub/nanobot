import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ClientProvider, useClient } from "@/providers/ClientProvider";
import { MailAction } from "@/components/mail/MailAction";
import { MailSources } from "@/components/mail/MailSources";
import { mailDownload } from "@/lib/mail-api";
import { ApiError } from "@/lib/api";
import { MessageBubble } from "@/components/MessageBubble";
import type { NanobotClient } from "@/lib/nanobot-client";
import type { UIMessage } from "@/lib/types";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Mail owner UI", () => {
  it("requires a separate explicit confirmation and reports queued work honestly", async () => {
    const request = vi.fn().mockResolvedValueOnce({ status: "needs_confirmation", confirmation_token: "fake-token",
      target_ids: ["mail_123"], preview: [{ id: "mail_123", subject: "Fictional source" }] })
      .mockResolvedValueOnce({ status: "accepted_waiting_service", target_ids: ["mail_123"] });
    render(<ClientProvider client={{ mailRequest: request } as unknown as NanobotClient} token="fake" mailOwner>
      <MailAction action="prepare_remove" target="mail_123" />
    </ClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "Remove source" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Fictional source")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm this action" }));
    await waitFor(() => expect(request).toHaveBeenLastCalledWith("confirm_remove", { token: "fake-token", chat_id: "management" }));
    expect(await screen.findByRole("status")).toHaveTextContent("waiting for Gateway");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders hostile source text as text, with coverage and quote warnings", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ next_cursor: null, sources: [{
      evidence_id: "ev_123", mail_id: "mail_123", document_id: "doc_123", subject: "Fictional mail",
      filename: "", sender_address: "test@example.invalid", sender_name: "Test", received_at: null,
      content_role: "quoted", locator: { paragraph: 1 }, excerpt: '<img src=x onerror="alert(1)">',
      coverage: { complete: false, truncated: true, warnings: ["needs_ocr"] },
    }] }) }));
    const { container } = render(<ClientProvider client={{} as NanobotClient} token="fake" mailOwner>
      <MailSources citations={[{ evidence_id: "ev_123", subject: "Fictional mail" }]} chatId="owner-chat" />
    </ClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "[1] Fictional mail" }));
    expect(await screen.findByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("Message body · Quoted history")).toBeInTheDocument();
    expect(screen.getByText(/needs_ocr/)).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("chat_id=owner-chat"), expect.objectContaining({
      headers: { Authorization: "Bearer fake" }, cache: "no-store",
    }));
  });

  it("discards a download when a later range is revoked", async () => {
    const request = vi.fn().mockResolvedValueOnce({ status: 206, headers: new Headers({ "Content-Range": "bytes 0-1/4" }),
      arrayBuffer: async () => new Uint8Array([1, 2]).buffer }).mockResolvedValueOnce({ status: 403 });
    vi.stubGlobal("fetch", request);
    await expect(mailDownload("fake", "doc_123", "owner-chat")).rejects.toThrow("HTTP 403");
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[1][1].headers).toEqual({ Authorization: "Bearer fake", Range: "bytes=2-65537" });
  });

  it("removes all owner private message content after a source 403 and keeps it hidden on remount", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new ApiError(403, "HTTP 403")));
    const messages: UIMessage[] = [
      { id: "a1", role: "assistant", content: "PRIVATE-ORIGINAL-ONE", citations: [{ evidence_id: "ev_1", subject: "PRIVATE-SUBJECT" }], mailChatId: "owner-chat", createdAt: 1 },
      { id: "a2", role: "assistant", content: "PRIVATE-ORIGINAL-TWO", createdAt: 2 },
      { id: "t1", role: "tool", kind: "trace", content: "PRIVATE-TRACE", createdAt: 3 },
    ];
    const view = (items = messages) => <ClientProvider client={{} as NanobotClient} token="owner-token" mailOwner>
      {items.map((message) => <MessageBubble key={message.id} message={message} />)}
    </ClientProvider>;
    const { rerender } = render(view());
    fireEvent.click(screen.getByRole("button", { name: "[1] PRIVATE-SUBJECT" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 403");
    await waitFor(() => {
      expect(screen.queryByText("PRIVATE-ORIGINAL-ONE")).not.toBeInTheDocument();
      expect(screen.queryByText("PRIVATE-ORIGINAL-TWO")).not.toBeInTheDocument();
      expect(screen.queryByText("PRIVATE-TRACE")).not.toBeInTheDocument();
      expect(screen.queryByText("PRIVATE-SUBJECT")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Copy reply" })).not.toBeInTheDocument();
    });
    rerender(view([messages[1]]));
    expect(screen.queryByText("PRIVATE-ORIGINAL-TWO")).not.toBeInTheDocument();
  });

  it("does not invalidate access for a non-permission failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<ClientProvider client={{} as NanobotClient} token="owner-token" mailOwner>
      <MessageBubble message={{ id: "a1", role: "assistant", content: "PRIVATE-ORIGINAL", createdAt: 1 }} />
      <MailSources citations={[{ evidence_id: "ev_1", subject: "Source" }]} chatId="owner-chat" />
    </ClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "[1] Source" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("network down");
    expect(screen.getByText("PRIVATE-ORIGINAL")).toBeInTheDocument();
  });

  it("aborts another source request when owner access is revoked", async () => {
    let pendingSignal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("ev_pending")) {
        pendingSignal = init?.signal as AbortSignal;
        return new Promise<Response>(() => {});
      }
      return Promise.resolve({ ok: false, status: 403, json: async () => ({}) } as Response);
    }));
    const { container } = render(<ClientProvider client={{} as NanobotClient} token="owner-token" mailOwner>
      <MailSources citations={[{ evidence_id: "ev_pending", subject: "Pending source" }]} chatId="owner-chat" />
      <MailSources citations={[{ evidence_id: "ev_denied", subject: "Denied source" }]} chatId="owner-chat" />
    </ClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "[1] Pending source" }));
    await waitFor(() => expect(pendingSignal).toBeDefined());
    fireEvent.click(container.querySelectorAll("button")[1]);
    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 403");
    await waitFor(() => expect(pendingSignal?.aborted).toBe(true));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("resets denial on token change and ignores stale token invalidation", () => {
    let oldInvalidate: ((token: string) => void) | undefined;
    function StateProbe() {
      const client = useClient();
      oldInvalidate = client.invalidatePrivateAccess;
      return <span>{client.privateAccessDenied ? "denied" : "allowed"}</span>;
    }
    const { rerender } = render(<ClientProvider client={{} as NanobotClient} token="old-token" mailOwner>
      <StateProbe />
    </ClientProvider>);
    act(() => oldInvalidate!("old-token"));
    expect(screen.getByText("denied")).toBeInTheDocument();
    const staleInvalidate = oldInvalidate!;
    rerender(<ClientProvider client={{} as NanobotClient} token="new-token" mailOwner>
      <StateProbe />
    </ClientProvider>);
    expect(screen.getByText("allowed")).toBeInTheDocument();
    act(() => staleInvalidate("old-token"));
    expect(screen.getByText("allowed")).toBeInTheDocument();
  });
});
