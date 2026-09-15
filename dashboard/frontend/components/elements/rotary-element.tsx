"use client"

/**
 * Rotary detent knob for bool and enum descriptors.
 *
 * One detent per allowed value. The current position is unambiguous at a
 * glance; each stop is labelled from the lexicon's enum[].label. Accepts
 * drag, keyboard (arrows to step, Home/End to extremes) and direct tap of
 * any detent button. No signal or parameter name is hardcoded here.
 */

import { useEffect, useRef, useState, type CSSProperties } from "react"

import { useDashboard } from "@/lib/store/dashboard-context"
import { useTelemetryVersion } from "@/lib/hooks/use-telemetry"
import type { Descriptor, EnumMember } from "@/lib/lexicon/types"
import type { Binding } from "@/lib/types/layout"
import { cn } from "@/lib/utils"

interface RotaryElementProps {
  binding: Binding
  descriptor: Descriptor
}

/**
 * Degrees from 12 o'clock, clockwise. Total travel is 270°: first detent at
 * −135° (7:30 position), last detent at +135° (4:30 position).
 */
function detentAngle(index: number, count: number): number {
  if (count <= 1) return 0
  return -135 + index * (270 / (count - 1))
}

/** Convert clockwise-from-top degrees to SVG (x, y) centred at (cx, cy) at radius r. */
function polar(cx: number, cy: number, r: number, deg: number): [number, number] {
  const rad = (deg * Math.PI) / 180
  return [cx + r * Math.sin(rad), cy - r * Math.cos(rad)]
}

/** SVG path for a clockwise arc from fromDeg to toDeg at radius r around (cx, cy). */
function arcPath(cx: number, cy: number, r: number, fromDeg: number, toDeg: number): string {
  const span = toDeg - fromDeg
  if (Math.abs(span) < 0.5) return ""
  const [sx, sy] = polar(cx, cy, r, fromDeg)
  const [ex, ey] = polar(cx, cy, r, toDeg)
  const largeArc = Math.abs(span) > 180 ? 1 : 0
  const sweep = span > 0 ? 1 : 0
  return `M ${sx.toFixed(2)} ${sy.toFixed(2)} A ${r} ${r} 0 ${largeArc} ${sweep} ${ex.toFixed(2)} ${ey.toFixed(2)}`
}

/** Clamp a pointer angle to the knob's travel range. */
function clampToTravel(deg: number): number {
  return Math.max(-135, Math.min(135, deg))
}

/** Index of the detent closest to the given clockwise-from-top angle. */
function nearestDetent(deg: number, count: number): number {
  let best = 0
  let bestDist = Infinity
  for (let i = 0; i < count; i += 1) {
    const dist = Math.abs(deg - detentAngle(i, count))
    if (dist < bestDist) {
      bestDist = dist
      best = i
    }
  }
  return best
}

/** Build the detent list from the descriptor. Bool gets two synthetic members. */
function detentsFor(descriptor: Descriptor): EnumMember[] {
  if (descriptor.datatype === "enum") return descriptor.enum ?? []
  return [
    { value: false as boolean, label: "Off" },
    { value: true as boolean, label: "On" },
  ]
}

export function RotaryElement({ binding, descriptor }: RotaryElementProps): JSX.Element {
  const { write } = useDashboard()
  const { store } = useTelemetryVersion()

  const detents = detentsFor(descriptor)
  const count = detents.length

  const plantValue = store.controlValue(binding.collection, descriptor.name)
  const controlState = store.controlState(binding.collection, descriptor.name)

  const indexOfValue = (v: unknown): number => {
    if (v === undefined || v === null) return 0
    const idx = detents.findIndex((d) => d.value === v)
    return idx >= 0 ? idx : 0
  }

  const [currentIndex, setCurrentIndex] = useState(() => indexOfValue(plantValue))
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setCurrentIndex(indexOfValue(plantValue))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plantValue])

  const send = (index: number): void => {
    const detent = detents[index]
    if (!detent) return
    const result = write(binding, detent.value)
    setError(result.ok ? null : (result.error ?? "rejected"))
  }

  const step = (delta: number): void => {
    const next = Math.max(0, Math.min(count - 1, currentIndex + delta))
    if (next === currentIndex) return
    setCurrentIndex(next)
    send(next)
  }

  const dragging = useRef(false)

  // SVG geometry constants (100×100 viewBox, centre at 50,50)
  const CX = 50
  const CY = 50
  const R_TRACK = 38
  const R_KNOB = 22
  const R_IND = 14
  const R_TICK_IN = 35
  const R_TICK_OUT = 42

  const currentAngle = detentAngle(currentIndex, count)
  const [indX, indY] = polar(CX, CY, R_IND, currentAngle)

  const angleFromPointer = (e: { currentTarget: Element; clientX: number; clientY: number }): number => {
    const rect = e.currentTarget.getBoundingClientRect()
    const pcx = rect.left + rect.width / 2
    const pcy = rect.top + rect.height / 2
    return (Math.atan2(e.clientX - pcx, -(e.clientY - pcy)) * 180) / Math.PI
  }

  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 select-none">
      {/* Rotating dial */}
      <svg
        viewBox="0 0 100 100"
        className={cn(
          "w-full max-w-[96px] cursor-grab rounded-full",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
          controlState === "pending" && "animate-pulse opacity-75",
        )}
        role="spinbutton"
        aria-valuemin={0}
        aria-valuemax={count - 1}
        aria-valuenow={currentIndex}
        aria-valuetext={detents[currentIndex]?.label ?? String(currentIndex)}
        aria-label={descriptor.label}
        tabIndex={0}
        onPointerDown={(e) => {
          dragging.current = true
          e.currentTarget.setPointerCapture(e.pointerId)
        }}
        onPointerMove={(e) => {
          if (!dragging.current) return
          const idx = nearestDetent(clampToTravel(angleFromPointer(e)), count)
          if (idx !== currentIndex) setCurrentIndex(idx)
        }}
        onPointerUp={(e) => {
          if (!dragging.current) return
          dragging.current = false
          const idx = nearestDetent(clampToTravel(angleFromPointer(e)), count)
          setCurrentIndex(idx)
          send(idx)
        }}
        onKeyDown={(e) => {
          switch (e.key) {
            case "ArrowRight":
            case "ArrowUp":
              e.preventDefault()
              step(1)
              break
            case "ArrowLeft":
            case "ArrowDown":
              e.preventDefault()
              step(-1)
              break
            case "Home":
              e.preventDefault()
              setCurrentIndex(0)
              send(0)
              break
            case "End": {
              e.preventDefault()
              const last = count - 1
              setCurrentIndex(last)
              send(last)
              break
            }
            default:
              break
          }
        }}
      >
        {/* Background track arc: full 270° travel */}
        <path
          d={arcPath(CX, CY, R_TRACK, -135, 135)}
          fill="none"
          style={{ stroke: "hsl(var(--border))", strokeWidth: "4", strokeLinecap: "round" } as CSSProperties}
        />

        {/* Active arc: from start (−135°) to the current detent */}
        {currentIndex > 0 ? (
          <path
            d={arcPath(CX, CY, R_TRACK, -135, currentAngle)}
            fill="none"
            style={{ stroke: "hsl(var(--primary))", strokeWidth: "4", strokeLinecap: "round" } as CSSProperties}
          />
        ) : null}

        {/* Knob body */}
        <circle
          cx={CX}
          cy={CY}
          r={R_KNOB}
          style={{
            fill: "hsl(var(--accent))",
            stroke: "hsl(var(--border))",
            strokeWidth: "1.5",
          } as CSSProperties}
        />

        {/* Tick marks at each detent */}
        {Array.from({ length: count }).map((_, i) => {
          const angle = detentAngle(i, count)
          const [ix, iy] = polar(CX, CY, R_TICK_IN, angle)
          const [ox, oy] = polar(CX, CY, R_TICK_OUT, angle)
          const active = i === currentIndex
          return (
            <line
              key={i}
              x1={ix.toFixed(2)}
              y1={iy.toFixed(2)}
              x2={ox.toFixed(2)}
              y2={oy.toFixed(2)}
              style={{
                stroke: active ? "hsl(var(--primary))" : "hsl(var(--muted-foreground))",
                strokeWidth: active ? "2.5" : "1.5",
                strokeLinecap: "round",
              } as CSSProperties}
            />
          )
        })}

        {/* Indicator dot: shows current position on the knob face */}
        <circle
          cx={indX.toFixed(2)}
          cy={indY.toFixed(2)}
          r="4"
          style={{ fill: "hsl(var(--primary))" } as CSSProperties}
        />
      </svg>

      {/* Current value label */}
      <div
        className={cn(
          "text-sm font-semibold",
          controlState === "unconfirmed" && "text-warning",
        )}
      >
        {detents[currentIndex]?.label ?? "—"}
      </div>

      {/* Detent buttons — direct selection without dragging */}
      <div className="no-drag flex flex-wrap justify-center gap-1">
        {detents.map((detent, i) => (
          <button
            key={i}
            type="button"
            className={cn(
              "min-h-[28px] rounded border px-2 py-0.5 text-xs transition-colors",
              i === currentIndex
                ? "border-primary bg-primary text-primary-foreground"
                : "border-border text-muted-foreground hover:border-primary hover:text-foreground",
            )}
            onClick={() => {
              setCurrentIndex(i)
              send(i)
            }}
          >
            {detent.label}
          </button>
        ))}
      </div>

      {error ? <div className="text-[10px] text-destructive">{error}</div> : null}
      {controlState === "unconfirmed" ? (
        <div className="text-[10px] text-warning">not confirmed by the plant</div>
      ) : null}
    </div>
  )
}
