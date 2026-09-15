/**
 * The WebSocket client: one socket per tab, carrying batched telemetry down and
 * control writes up.
 *
 * Every reconnect restarts at `hello` -> `snapshot`, so first connect and
 * recovery are one code path. The message objects are transport-neutral on
 * purpose: an SSE + POST fallback would swap this file and backend/hub.py and
 * nothing else.
 */

export interface SignalRef {
  name: string
  direction: "input" | "output" | null
}

export interface ServerFrame {
  t: string
  [key: string]: unknown
}

export type SocketState = "connecting" | "open" | "closed"

const BACKOFF_MIN_MS = 500
const BACKOFF_MAX_MS = 8000

export class DashboardSocket {
  private socket: WebSocket | null = null
  private attempt = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  private stopped = false
  private subscription: SignalRef[] = []
  private paused = false
  private lexiconRev = 0
  private historyS = 60

  constructor(
    private readonly onFrame: (frame: ServerFrame) => void,
    private readonly onState: (state: SocketState) => void,
  ) {}

  start(): void {
    this.stopped = false
    this.open()
  }

  stop(): void {
    this.stopped = true
    if (this.timer) clearTimeout(this.timer)
    this.socket?.close()
    this.socket = null
  }

  setContext(lexiconRev: number, historyS: number): void {
    this.lexiconRev = lexiconRev
    this.historyS = historyS
  }

  /** Replaces the subscription set; the server answers with a fresh snapshot. */
  setSubscription(signals: SignalRef[]): void {
    this.subscription = signals
    this.send({ t: "sub", signals })
  }

  write(seq: number, signals: Record<string, unknown>, parameters: Record<string, unknown>): void {
    this.send({ t: "write", seq, signals, parameters })
  }

  /** Nothing is buffered for a tab that is not looking; the gap refills from the
   * backend window on resume. */
  setPaused(paused: boolean): void {
    if (this.paused === paused) return
    this.paused = paused
    this.send({ t: paused ? "pause" : "resume" })
  }

  private open(): void {
    if (this.stopped) return
    this.onState("connecting")
    const scheme = window.location.protocol === "https:" ? "wss" : "ws"
    const socket = new WebSocket(`${scheme}://${window.location.host}/ws`)
    this.socket = socket

    socket.onopen = () => {
      this.attempt = 0
      this.onState("open")
      this.send({
        t: "hello",
        lexicon_rev: this.lexiconRev,
        signals: this.subscription,
        history_s: this.historyS,
      })
      if (this.paused) this.send({ t: "pause" })
    }

    socket.onmessage = (event) => {
      let frame: ServerFrame
      try {
        frame = JSON.parse(event.data as string) as ServerFrame
      } catch {
        return
      }
      if (frame.t === "ping") {
        this.send({ t: "pong" })
        return
      }
      this.onFrame(frame)
    }

    socket.onclose = () => {
      this.socket = null
      this.onState("closed")
      this.scheduleReconnect()
    }

    socket.onerror = () => socket.close()
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    const ceiling = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** this.attempt)
    this.attempt += 1
    // Full jitter, so a backend restart does not bring every tab back at once.
    const delay = Math.random() * ceiling
    this.timer = setTimeout(() => this.open(), delay)
  }

  private send(message: Record<string, unknown>): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message))
    }
  }
}
