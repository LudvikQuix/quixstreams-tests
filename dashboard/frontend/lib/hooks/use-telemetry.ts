"use client"

/**
 * React binding for the telemetry store.
 *
 * The store bumps one version counter per flush tick; components that need to
 * re-render on new data subscribe to it. Charts deliberately do NOT use this -
 * they read the store from the shared rAF tick so a 10 Hz stream never turns
 * into a 10 Hz React render of a canvas.
 */

import { useSyncExternalStore } from "react"

import { useDashboard } from "@/lib/store/dashboard-context"
import type { TelemetryStore } from "@/lib/store/telemetry"

export function useTelemetryVersion(): { store: TelemetryStore; version: number } {
  const { store } = useDashboard()
  const version = useSyncExternalStore(
    store.subscribe,
    store.getVersion,
    () => 0,
  )
  return { store, version }
}
