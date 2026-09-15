/**
 * Typed fetch for /api/*. Same origin as the page — one process serves both, so
 * there is no base URL to configure and no CORS to get wrong.
 */

import type { LexiconResponse } from "@/lib/lexicon/types"

export interface RuntimeConfig {
  history_seconds: number
  applied_timeout_ms: number
  ws_flush_hz: number
}

/**
 * A failed /api call, with the backend's own explanation attached. The lexicon
 * 503 answers with `{detail, lexicon_type, lexicon_target_key, …}`; a bare
 * status code would leave the empty-state page guessing why it is empty.
 */
export class ApiError extends Error {
  readonly status: number
  readonly body: Record<string, unknown> | null

  constructor(status: number, detail: string, body: Record<string, unknown> | null) {
    super(detail)
    this.name = "ApiError"
    this.status = status
    this.body = body
  }
}

async function apiError(path: string, response: Response): Promise<ApiError> {
  let body: Record<string, unknown> | null = null
  try {
    body = (await response.json()) as Record<string, unknown>
  } catch {
    // A proxy or the ingress can answer with HTML; the status is still the news.
    body = null
  }
  const detail =
    typeof body?.detail === "string" ? body.detail : `${path} -> ${response.status}`
  return new ApiError(response.status, detail, body)
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" })
  if (!response.ok) throw await apiError(path, response)
  return (await response.json()) as T
}

export function fetchLexicon(): Promise<LexiconResponse> {
  return getJson<LexiconResponse>("/api/lexicon")
}

export function fetchConfig(): Promise<RuntimeConfig> {
  return getJson<RuntimeConfig>("/api/config")
}

/** Asks the backend to re-read the DCM — and, on an empty DCM, to seed it. */
export async function refreshLexicon(): Promise<{ rev: number }> {
  const response = await fetch("/api/lexicon/refresh", { method: "POST" })
  if (!response.ok) throw await apiError("/api/lexicon/refresh", response)
  return (await response.json()) as { rev: number }
}

/** The scripting/e2e entry point. The UI writes over the socket instead. */
export async function postControl(
  signals: Record<string, unknown>,
  parameters: Record<string, unknown>,
): Promise<string[]> {
  const response = await fetch("/api/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ signals, parameters }),
  })
  if (response.status === 422) {
    const body = (await response.json()) as { errors: string[] }
    return body.errors
  }
  if (!response.ok) throw new Error(`control -> ${response.status}`)
  return []
}
