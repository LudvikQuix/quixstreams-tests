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

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" })
  if (!response.ok) throw new Error(`${path} -> ${response.status}`)
  return (await response.json()) as T
}

export function fetchLexicon(): Promise<LexiconResponse> {
  return getJson<LexiconResponse>("/api/lexicon")
}

export function fetchConfig(): Promise<RuntimeConfig> {
  return getJson<RuntimeConfig>("/api/config")
}

export function refreshLexicon(): Promise<{ rev: number }> {
  return fetch("/api/lexicon/refresh", { method: "POST" }).then((response) => {
    if (!response.ok) throw new Error(`refresh -> ${response.status}`)
    return response.json()
  })
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
