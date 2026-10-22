import { createContext, useContext, useRef, useState, type ReactNode } from "react";

import type { NanobotClient } from "@/lib/nanobot-client";

interface ClientContextValue {
  client: NanobotClient;
  token: string;
  modelName: string | null;
  mailOwner: boolean;
  privateAccessDenied: boolean;
  invalidatePrivateAccess: (requestToken: string) => void;
}

const ClientContext = createContext<ClientContextValue | null>(null);

export function ClientProvider({
  client,
  token,
  modelName = null,
  mailOwner = false,
  children,
}: {
  client: NanobotClient;
  token: string;
  modelName?: string | null;
  mailOwner?: boolean;
  children: ReactNode;
}) {
  const activeToken = useRef(token);
  activeToken.current = token;
  const [denied, setDenied] = useState<{ token: string; value: boolean }>({ token, value: false });
  const invalidatePrivateAccess = (requestToken: string) => {
    if (activeToken.current === requestToken) setDenied({ token: requestToken, value: true });
  };
  return (
    <ClientContext.Provider value={{ client, token, modelName, mailOwner,
      privateAccessDenied: denied.token === token && denied.value, invalidatePrivateAccess }}>
      {children}
    </ClientContext.Provider>
  );
}

export function useOptionalClient(): ClientContextValue | null {
  return useContext(ClientContext);
}

export function useClient(): ClientContextValue {
  const ctx = useContext(ClientContext);
  if (!ctx) {
    throw new Error("useClient must be used within a ClientProvider");
  }
  return ctx;
}
