import type {
  ApiResponse,
  SessionInfo,
  ConfigPayload,
  OverviewData,
  EnvData,
  ModelsResponse,
  LogsData,
  NetworkData,
  TokenDetail,
  TokenVerifyResult,
  RotationInfo,
  ApiKeysData,
  ApiKeyItem,
  RequestsData,
  RequestStats,
  ChatTestResult,
  GeoInfo,
  GeoRefreshResult,
} from "@/types"

const API_BASE = "/admin/api"

class ApiClientError extends Error {
  status: number
  data: { detail?: string }

  constructor(status: number, data: { detail?: string }) {
    super(data.detail || "Request failed")
    this.status = status
    this.data = data
  }
}

export { ApiClientError }

async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const headers: Record<string, string> = {
    ...(options?.headers as Record<string, string>),
  }

  if (options?.body && typeof options.body === "string") {
    headers["Content-Type"] = "application/json"
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
    credentials: "same-origin",
  })

  const json = await res.json()

  if (!res.ok) {
    throw new ApiClientError(res.status, json)
  }

  const wrapped = json as ApiResponse<T>
  return wrapped.data
}

export const api = {
  // Auth
  session: () => request<SessionInfo>("/session"),

  login: (key: string) =>
    request<ConfigPayload>("/login", {
      method: "POST",
      body: JSON.stringify({ key }),
    }),

  logout: () =>
    request<unknown>("/logout", { method: "POST" }),

  // Overview & Config
  overview: () => request<OverviewData>("/overview"),

  config: () => request<ConfigPayload>("/config"),

  env: () => request<EnvData>("/env"),

  // Geo / device fingerprint
  geo: () => request<GeoInfo>("/geo"),

  refreshGeo: () =>
    request<GeoRefreshResult>("/geo/refresh", { method: "POST" }),

  // Models
  models: () => request<ModelsResponse>("/models"),

  // Runtime Logs
  logs: (params?: { since_id?: number; limit?: number; level?: string }) => {
    const sp = new URLSearchParams()
    if (params?.since_id) sp.set("since_id", String(params.since_id))
    if (params?.limit) sp.set("limit", String(params.limit))
    if (params?.level) sp.set("level", params.level)
    const qs = sp.toString()
    return request<LogsData>(`/logs${qs ? `?${qs}` : ""}`)
  },

  // Network
  network: () => request<NetworkData>("/network"),

  // Freebuff Tokens
  getToken: (index: number) =>
    request<TokenDetail>(`/freebuff-tokens/${index}`),

  addToken: (token: string) =>
    request<ConfigPayload>("/freebuff-tokens", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  updateToken: (index: number, token: string) =>
    request<ConfigPayload>(`/freebuff-tokens/${index}`, {
      method: "PUT",
      body: JSON.stringify({ token }),
    }),

  deleteToken: (index: number) =>
    request<ConfigPayload>(`/freebuff-tokens/${index}`, {
      method: "DELETE",
    }),

  saveTokens: (tokens: string[]) =>
    request<ConfigPayload>("/freebuff-tokens", {
      method: "PUT",
      body: JSON.stringify({ tokens }),
    }),

  verifyToken: (token: string) =>
    request<TokenVerifyResult>("/freebuff-tokens/verify", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  // Account rotation / health
  rotateTokens: () =>
    request<RotationInfo>("/tokens/rotate", { method: "POST" }),

  activateToken: (index: number) =>
    request<RotationInfo>(`/tokens/activate/${index}`, { method: "POST" }),

  validateTokens: () =>
    request<RotationInfo>("/tokens/validate", { method: "POST" }),

  // API Keys
  getKeys: () => request<ApiKeysData>("/api-keys"),

  createKey: (name: string, key: string, allowedModels: string[] = ["*"]) =>
    request<ApiKeyItem>("/api-keys", {
      method: "POST",
      body: JSON.stringify({ name, key, allowed_models: allowedModels }),
    }),

  updateKey: (name: string, fields: { key?: string; allowed_models?: string[]; enabled?: boolean }) =>
    request<ApiKeyItem>(`/api-keys/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify(fields),
    }),

  deleteKey: (name: string) =>
    request<unknown>(`/api-keys/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  toggleKey: (name: string) =>
    request<ApiKeyItem>(`/api-keys/${encodeURIComponent(name)}/toggle`, {
      method: "PUT",
    }),

  // Request Records
  requests: (params?: {
    since_id?: number
    limit?: number
    model?: string
    status?: string
    api_key_name?: string
  }) => {
    const sp = new URLSearchParams()
    if (params?.since_id) sp.set("since_id", String(params.since_id))
    if (params?.limit) sp.set("limit", String(params.limit))
    if (params?.model) sp.set("model", params.model)
    if (params?.status) sp.set("status", params.status)
    if (params?.api_key_name) sp.set("api_key_name", params.api_key_name)
    const qs = sp.toString()
    return request<RequestsData>(`/requests${qs ? `?${qs}` : ""}`)
  },

  requestStats: () => request<RequestStats>("/requests/stats"),

  clearRequests: () =>
    request<unknown>("/requests", { method: "DELETE" }),

  // Security
  updateSecurity: (adminKey: string) =>
    request<ConfigPayload>("/security", {
      method: "PUT",
      body: JSON.stringify({ admin_key: adminKey }),
    }),

  // Proxy
  saveProxy: (payload: { proxy_enabled: boolean; proxy_type: string; proxy_host: string; proxy_port: number; proxy_username?: string; proxy_password?: string }) =>
    request<ConfigPayload>("/proxy", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  testProxy: (payload: { proxy_type: string; proxy_host: string; proxy_port: number; proxy_username?: string; proxy_password?: string }) =>
    request<{ ok: boolean; ip?: string; country?: string; city?: string; org?: string; latency_ms?: number; error?: string }>("/proxy/test", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // Model Test
  chatTest: (model: string, prompt: string) =>
    request<ChatTestResult>("/chat-test", {
      method: "POST",
      body: JSON.stringify({ model, prompt }),
    }),
}
