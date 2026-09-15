/**
 * The D5 layout document.
 *
 * Normative schema: dev-planning/dashboard-service/layout-schema.json, at 1.1.
 *
 * A chart carries `bindings: []` because CLAUDE.md §4 (as amended by D7) says a
 * chart takes 1..N bindings. `binding` is kept as the singular alias and always
 * mirrors `bindings[0]`, so a consumer that only knows schema 1.0 still sees a
 * valid single-series chart. Schema 1.1 is additive over 1.0, so a stored 1.0
 * document loads here unchanged; documents written by this build declare "1.1".
 */

import type { Collection, Direction } from "@/lib/lexicon/types"

export type ElementType = "switch" | "knob" | "typein" | "readout" | "chart"

export interface Binding {
  collection: Collection
  name: string
  direction: Direction
}

export interface ElementPosition {
  x: number
  y: number
  w: number
  h: number
  min_w: number
  min_h: number
}

export interface KnobOptions {
  min: number | null
  max: number | null
  step: number | null
  scale: "linear" | "log"
  show_input: boolean
}

export interface ReadoutOptions {
  decimals: number | null
  show_unit: boolean
  thresholds: { op: "lt" | "lte" | "gt" | "gte"; value: number; tone: string }[] | null
  stale_after_ms: number
}

export interface ChartOptions {
  window_s: number
  y_min: number | null
  y_max: number | null
  y_autoscale: boolean
  stroke: string | null
  fill: boolean
  points: boolean
  stale_after_ms: number
}

export type ElementOptions = KnobOptions | ReadoutOptions | ChartOptions | Record<string, unknown>

export interface LayoutElement {
  id: string
  type: ElementType
  title: string | null
  binding: Binding | null
  bindings?: Binding[]
  position: ElementPosition
  options: ElementOptions
}

export interface DashboardLayout {
  layout_version: string
  layout_id: string
  name: string
  description: string
  model: { name: string; lexicon_version: string }
  grid: {
    cols: 12
    row_height: number
    margin: [number, number]
    compact_type: "vertical" | "horizontal" | null
  }
  elements: LayoutElement[]
  created_at: string | null
  updated_at: string | null
  updated_by: string | null
}

export const LAYOUT_VERSION = "1.1"

/** Every element carries a full option object; absent keys are not a thing. */
export function defaultOptions(type: ElementType): ElementOptions {
  if (type === "knob") {
    return { min: null, max: null, step: null, scale: "linear", show_input: true }
  }
  if (type === "chart") {
    return {
      window_s: 60,
      y_min: null,
      y_max: null,
      y_autoscale: true,
      stroke: null,
      fill: false,
      points: false,
      stale_after_ms: 2000,
    }
  }
  return { decimals: 2, show_unit: true, thresholds: null, stale_after_ms: 2000 }
}

export function defaultPosition(type: ElementType, y: number): ElementPosition {
  if (type === "chart") return { x: 0, y, w: 6, h: 4, min_w: 3, min_h: 3 }
  if (type === "knob") return { x: 0, y, w: 4, h: 3, min_w: 3, min_h: 2 }
  return { x: 0, y, w: 3, h: 2, min_w: 2, min_h: 2 }
}

/** Charts may hold several; every other element type holds exactly one. */
export function bindingsOf(element: LayoutElement): Binding[] {
  if (element.bindings && element.bindings.length > 0) return element.bindings
  return element.binding ? [element.binding] : []
}

export function withBindings(element: LayoutElement, bindings: Binding[]): LayoutElement {
  if (element.type === "chart") {
    return { ...element, bindings, binding: bindings[0] ?? null }
  }
  return { ...element, bindings: undefined, binding: bindings[0] ?? null }
}

export function emptyLayout(layoutId: string, modelName: string, lexiconVersion: string): DashboardLayout {
  return {
    layout_version: LAYOUT_VERSION,
    layout_id: layoutId,
    name: layoutId,
    description: "",
    model: { name: modelName, lexicon_version: lexiconVersion },
    grid: { cols: 12, row_height: 60, margin: [12, 12], compact_type: "vertical" },
    elements: [],
    created_at: null,
    updated_at: null,
    updated_by: null,
  }
}
