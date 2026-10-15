// View-side transport for the owner routes.
//
// The view must NOT roll its own fetch/credentials. It reuses the pinned host
// authenticated API client (`client` from @elizaos/ui, the same singleton
// DynamicViewLoader injects and uses for protected bundle fetch), which attaches the
// host's existing connection headers/cookies and owner bearer. No token ever lives in
// this bundle.

export interface CompanionTransport {
  rawRequest(
    path: string,
    init?: { method?: string; headers?: Record<string, string>; signal?: AbortSignal },
    options?: { allowNonOk?: boolean; timeoutMs?: number },
  ): Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;
}

const TRANSPORT_TIMEOUT_MS = 8000;

/** GET a companion route through the host API client and parse JSON. Throws on failure. */
export async function loadCompanionJson(
  client: CompanionTransport,
  path: string,
  signal?: AbortSignal,
): Promise<unknown> {
  const response = await client.rawRequest(
    path,
    { method: "GET", headers: { accept: "application/json" }, ...(signal ? { signal } : {}) },
    { allowNonOk: true, timeoutMs: TRANSPORT_TIMEOUT_MS },
  );
  if (!response || !response.ok) throw new Error("companion-request-failed");
  return response.json();
}
