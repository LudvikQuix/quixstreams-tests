/**
 * Layout persistence behind a storage adapter.
 *
 * M1 binds it to localStorage, M2 repoints it at /api/layouts and DCM (D5). The
 * document written is already the D5 schema in both cases, so M2 is a one-file
 * swap with no migration: the same JSON that sits in localStorage today is the
 * JSON DCM will version tomorrow.
 */

import type { DashboardLayout } from "@/lib/types/layout"

export interface LayoutSummary {
  layout_id: string
  name: string
}

export interface LayoutStorage {
  list(): Promise<LayoutSummary[]>
  load(layoutId: string): Promise<DashboardLayout | null>
  save(layout: DashboardLayout): Promise<void>
  remove(layoutId: string): Promise<void>
}

const PREFIX = "sil-dashboard:layout:"

function keyFor(layoutId: string): string {
  return `${PREFIX}${layoutId}`
}

function parse(raw: string | null): DashboardLayout | null {
  if (!raw) return null
  try {
    return JSON.parse(raw) as DashboardLayout
  } catch {
    // A hand-edited or half-written entry must not blank the dashboard.
    return null
  }
}

export const localStorageAdapter: LayoutStorage = {
  async list(): Promise<LayoutSummary[]> {
    if (typeof window === "undefined") return []
    const summaries: LayoutSummary[] = []
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i)
      if (!key?.startsWith(PREFIX)) continue
      const layout = parse(window.localStorage.getItem(key))
      if (layout) summaries.push({ layout_id: layout.layout_id, name: layout.name })
    }
    return summaries.sort((a, b) => a.name.localeCompare(b.name))
  },

  async load(layoutId: string): Promise<DashboardLayout | null> {
    if (typeof window === "undefined") return null
    return parse(window.localStorage.getItem(keyFor(layoutId)))
  },

  async save(layout: DashboardLayout): Promise<void> {
    if (typeof window === "undefined") return
    const stamped: DashboardLayout = {
      ...layout,
      created_at: layout.created_at ?? new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }
    window.localStorage.setItem(keyFor(layout.layout_id), JSON.stringify(stamped))
  },

  async remove(layoutId: string): Promise<void> {
    if (typeof window === "undefined") return
    window.localStorage.removeItem(keyFor(layoutId))
  },
}
