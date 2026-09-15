"use client"

/**
 * Knob: a slider over a numeric input signal or tunable parameter, with an
 * optional exact-entry box.
 *
 * Two details decide whether this feels right or maddening:
 *   - Trailing throttle while dragging PLUS a guaranteed emit on release.
 *     Leading-edge-only throttling drops the value the user actually let go on,
 *     which is the single most likely way this ships feeling broken.
 *   - A control the user is holding is never reconciled. Incoming `applied`
 *     values are recorded by the store but not pushed into the slider until the
 *     pointer is released, or the 5 s heartbeat yanks it out from under a finger.
 */

import { useEffect, useRef, useState } from "react"

import { Input } from "@/components/ui/input"
import { Slider } from "@/components/ui/slider"
import { useDashboard } from "@/lib/store/dashboard-context"
import { useTelemetryVersion } from "@/lib/hooks/use-telemetry"
import { defaultStep, effectiveRange } from "@/lib/lexicon/resolve"
import type { Descriptor } from "@/lib/lexicon/types"
import type { Binding, KnobOptions } from "@/lib/types/layout"
import { cn } from "@/lib/utils"

/** The plant's sample period. A write per tick is the most it can act on. */
const THROTTLE_MS = 100

interface KnobElementProps {
  binding: Binding
  descriptor: Descriptor
  options: KnobOptions
}

export function KnobElement({
  binding,
  descriptor,
  options,
}: KnobElementProps): JSX.Element {
  const { write } = useDashboard()
  const { store } = useTelemetryVersion()

  const range = effectiveRange(descriptor, options.min, options.max)
  const step = options.step ?? defaultStep(descriptor, range)

  const holding = useRef(false)
  const lastSent = useRef(0)
  const trailing = useRef<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const plantValue = store.controlValue(binding.collection, descriptor.name)
  const fallback = typeof descriptor.default === "number" ? descriptor.default : range.min
  const [local, setLocal] = useState<number>(
    typeof plantValue === "number" ? plantValue : fallback,
  )
  const [text, setText] = useState<string>(String(local))
  const [error, setError] = useState<string | null>(null)

  const controlState = store.controlState(binding.collection, descriptor.name)

  useEffect(() => {
    if (holding.current) return
    if (typeof plantValue === "number" && plantValue !== local) {
      setLocal(plantValue)
      setText(String(plantValue))
    }
    // `local` is deliberately not a dependency: this effect exists to pull the
    // plant's value in, not to fight the user's own edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plantValue])

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current)
    }
  }, [])

  const send = (value: number): void => {
    const result = write(binding, value, range)
    setError(result.ok ? null : (result.error ?? "rejected"))
  }

  const sendThrottled = (value: number): void => {
    const now = Date.now()
    const elapsed = now - lastSent.current
    if (elapsed >= THROTTLE_MS) {
      lastSent.current = now
      send(value)
      return
    }
    trailing.current = value
    if (timer.current) return
    timer.current = setTimeout(() => {
      timer.current = null
      lastSent.current = Date.now()
      if (trailing.current !== null) send(trailing.current)
      trailing.current = null
    }, THROTTLE_MS - elapsed)
  }

  const commitText = (): void => {
    const parsed = Number(text)
    if (text.trim() === "" || Number.isNaN(parsed)) {
      setError("expects a number")
      return
    }
    setLocal(parsed)
    send(parsed)
  }

  return (
    <div className="flex h-full flex-col justify-center gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <span
          className={cn(
            "text-lg font-semibold tabular-nums",
            controlState === "pending" && "animate-pulse",
            controlState === "unconfirmed" && "text-warning",
          )}
        >
          {local}
        </span>
        {descriptor.unit ? (
          <span className="text-xs text-muted-foreground">{descriptor.unit}</span>
        ) : null}
      </div>

      <Slider
        className="no-drag"
        min={range.min}
        max={range.max}
        step={step}
        value={[local]}
        onPointerDown={() => {
          holding.current = true
        }}
        onValueChange={(next) => {
          holding.current = true
          setLocal(next[0])
          setText(String(next[0]))
          sendThrottled(next[0])
        }}
        onValueCommit={(next) => {
          holding.current = false
          if (timer.current) {
            clearTimeout(timer.current)
            timer.current = null
          }
          trailing.current = null
          lastSent.current = Date.now()
          send(next[0])
        }}
      />

      {options.show_input ? (
        <Input
          className="no-drag h-7 text-xs"
          value={text}
          inputMode="decimal"
          onFocus={() => {
            holding.current = true
          }}
          onChange={(event) => setText(event.target.value)}
          onBlur={() => {
            holding.current = false
            commitText()
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") commitText()
          }}
        />
      ) : null}

      {error ? <div className="text-[10px] text-destructive">{error}</div> : null}
      {controlState === "unconfirmed" ? (
        <div className="text-[10px] text-warning">not confirmed by the plant</div>
      ) : null}
    </div>
  )
}
