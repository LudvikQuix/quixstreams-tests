/**
 * Client-side telemetry state: latest values, per-signal history, the plant's
 * `applied` control state, and the pending/confirmed/rejected bookkeeping for
 * writes.
 *
 * Plain class, not a React store: charts read it imperatively from a
 * requestAnimationFrame tick, readouts read it through useSyncExternalStore.
 * Both need the same data and only one of them should cause React renders.
 */

import type { ServerFrame, SocketState } from "@/lib/transport/socket"
import { valuesMatch } from "@/lib/lexicon/validate"

export interface Series {
  ts: number[]
  v: (number | null)[]
}

export interface PendingWrite {
  value: unknown
  sentAt: number
  seq: number
}

export type ControlState = "idle" | "pending" | "unconfirmed"

export interface StatusInfo {
  telemetry_stale: boolean
  last_sample_age_ms: number | null
  ws_clients: number
}

type RejectHandler = (field: string, sent: unknown, echoed: unknown) => void

/**
 * How long a write is given before an echo is allowed to judge it.
 *
 * The round trip is coalesce (50 ms) + one plant tick (100 ms) + one flush
 * (100 ms) plus slack. Inside that window an arriving `applied` may have been
 * generated before the write reached the plant, and judging against it would
 * report a rejection for a write that was about to succeed. There is no
 * correlation id to do better with (Phase 1 §6.7 deferred the ack channel), so
 * early echoes are ignored rather than trusted.
 */
const SETTLE_MS = 400

export class TelemetryStore {
  version = 0
  connection: SocketState = "connecting"
  lexiconRev = 0
  windowS = 60
  appliedTimeoutMs = 7000
  dropped = 0
  status: StatusInfo = {
    telemetry_stale: true,
    last_sample_age_ms: null,
    ws_clients: 0,
  }
  applied: { signals: Record<string, unknown>; parameters: Record<string, unknown> } | null = null
  appliedTs: number | null = null

  private series = new Map<string, Series>()
  private latest = new Map<string, number>()
  private latestTs = new Map<string, number>()
  private pending = new Map<string, PendingWrite>()
  private listeners = new Set<() => void>()
  private rejectHandlers = new Set<RejectHandler>()

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getVersion = (): number => this.version

  onReject(handler: RejectHandler): () => void {
    this.rejectHandlers.add(handler)
    return () => this.rejectHandlers.delete(handler)
  }

  setConnection(state: SocketState): void {
    this.connection = state
    this.emit()
  }

  handleFrame(frame: ServerFrame): void {
    if (frame.t === "snapshot") {
      this.series.clear()
      this.latest.clear()
      this.latestTs.clear()
      this.windowS = (frame.history_s as number) ?? this.windowS
      this.lexiconRev = (frame.lexicon_rev as number) ?? this.lexiconRev
      this.ingest(frame.ts as number[], frame.series as Record<string, (number | null)[]>)
      this.setApplied(
        frame.applied as { signals: Record<string, unknown>; parameters: Record<string, unknown> } | null,
        Date.now() - ((frame.applied_age_ms as number) ?? 0),
      )
    } else if (frame.t === "frames") {
      this.dropped = (frame.dropped as number) ?? this.dropped
      this.ingest(frame.ts as number[], frame.series as Record<string, (number | null)[]>)
    } else if (frame.t === "applied") {
      this.setApplied(
        {
          signals: (frame.signals as Record<string, unknown>) ?? {},
          parameters: (frame.parameters as Record<string, unknown>) ?? {},
        },
        Date.now(),
      )
    } else if (frame.t === "status") {
      this.status = {
        telemetry_stale: Boolean(frame.telemetry_stale),
        last_sample_age_ms: (frame.last_sample_age_ms as number) ?? null,
        ws_clients: (frame.ws_clients as number) ?? 0,
      }
    } else if (frame.t === "lexicon") {
      this.lexiconRev = (frame.rev as number) ?? this.lexiconRev
    }
    this.emit()
  }

  private ingest(ts: number[], series: Record<string, (number | null)[]>): void {
    if (!ts || ts.length === 0) return
    for (const [name, values] of Object.entries(series ?? {})) {
      const bucket = this.series.get(name) ?? { ts: [], v: [] }
      for (let i = 0; i < ts.length; i += 1) {
        bucket.ts.push(ts[i])
        bucket.v.push(values[i] ?? null)
        const value = values[i]
        if (value !== null && value !== undefined) {
          this.latest.set(name, value)
          this.latestTs.set(name, ts[i])
        }
      }
      const cutoff = bucket.ts[bucket.ts.length - 1] - this.windowS * 1000
      let drop = 0
      while (drop < bucket.ts.length && bucket.ts[drop] < cutoff) drop += 1
      if (drop > 0) {
        bucket.ts.splice(0, drop)
        bucket.v.splice(0, drop)
      }
      this.series.set(name, bucket)
    }
  }

  /**
   * The `applied` echo is the only feedback channel the plant has: a rejected
   * write changes nothing, so the echo still carries the old value and the
   * control snaps back. That is the implicit NACK Phase 1 designed for.
   *
   * `receivedAt` is a LOCAL timestamp, not the broker's: an echo that predates a
   * write cannot judge it, and comparing a browser clock to a broker clock would
   * make that judgement depend on clock skew. A snapshot's `applied_age_ms`
   * converts to the same local scale.
   */
  private setApplied(
    applied: { signals: Record<string, unknown>; parameters: Record<string, unknown> } | null,
    receivedAt: number,
  ): void {
    if (!applied) return
    this.applied = applied
    this.appliedTs = receivedAt
    for (const [key, write] of Array.from(this.pending.entries())) {
      if (receivedAt - write.sentAt < SETTLE_MS) continue
      const [collection, name] = key.split(":")
      const bucket = collection === "signals" ? applied.signals : applied.parameters
      const echoed = bucket?.[name]
      if (echoed === undefined) continue
      this.pending.delete(key)
      if (!valuesMatch(write.value, echoed)) {
        this.rejectHandlers.forEach((handler) => handler(key, write.value, echoed))
      }
    }
  }

  markPending(collection: string, name: string, value: unknown, seq: number): void {
    this.pending.set(`${collection}:${name}`, { value, sentAt: Date.now(), seq })
    this.emit()
  }

  controlState(collection: string, name: string): ControlState {
    const write = this.pending.get(`${collection}:${name}`)
    if (!write) return "idle"
    return Date.now() - write.sentAt > this.appliedTimeoutMs ? "unconfirmed" : "pending"
  }

  /** Optimistic value while pending, the plant's echo otherwise. */
  controlValue(collection: string, name: string): unknown {
    const write = this.pending.get(`${collection}:${name}`)
    if (write) return write.value
    const bucket = collection === "signals" ? this.applied?.signals : this.applied?.parameters
    return bucket?.[name]
  }

  latestValue(name: string): number | undefined {
    return this.latest.get(name)
  }

  latestAge(name: string): number | null {
    const ts = this.latestTs.get(name)
    return ts === undefined ? null : Date.now() - ts
  }

  seriesFor(name: string): Series | undefined {
    return this.series.get(name)
  }

  private emit(): void {
    this.version += 1
    this.listeners.forEach((listener) => listener())
  }
}
