"use client"

/**
 * Numeric readout. Binds an output signal, or any parameter read-only.
 *
 * Parameters update from the `applied` block - roughly two messages a minute
 * plus every accepted write - so a parameter readout carries an "as of" hint
 * rather than pretending to be live.
 */

import type { Descriptor } from "@/lib/lexicon/types"
import { formatValue } from "@/lib/lexicon/validate"
import { useTelemetryVersion } from "@/lib/hooks/use-telemetry"
import type { Binding, ReadoutOptions } from "@/lib/types/layout"
import { cn } from "@/lib/utils"

interface ReadoutElementProps {
  binding: Binding
  descriptor: Descriptor
  options: ReadoutOptions
}

export function ReadoutElement({
  binding,
  descriptor,
  options,
}: ReadoutElementProps): JSX.Element {
  const { store } = useTelemetryVersion()

  const isSignal = binding.collection === "signals"
  const raw = isSignal
    ? store.latestValue(descriptor.name)
    : (store.controlValue("parameters", descriptor.name) as number | undefined)
  const age = isSignal
    ? store.latestAge(descriptor.name)
    : store.appliedTs === null
      ? null
      : Date.now() - store.appliedTs
  const stale = isSignal && age !== null && age > options.stale_after_ms

  const numeric = typeof raw === "number" ? raw : null
  const member =
    descriptor.datatype === "enum"
      ? descriptor.enum?.find((entry) => entry.value === raw)
      : undefined

  return (
    <div className="flex h-full flex-col items-center justify-center gap-1">
      <div
        className={cn(
          "text-2xl font-semibold tabular-nums sm:text-3xl",
          stale && "opacity-50",
        )}
      >
        {member ? member.label : formatValue(numeric, options.decimals)}
        {options.show_unit && descriptor.unit && !member ? (
          <span className="ml-1 text-sm font-normal text-muted-foreground">
            {descriptor.unit}
          </span>
        ) : null}
      </div>
      {!isSignal && age !== null ? (
        <div className="text-[10px] text-muted-foreground">
          as of {Math.round(age / 1000)}s ago
        </div>
      ) : null}
      {stale ? <div className="text-[10px] text-muted-foreground">no fresh sample</div> : null}
    </div>
  )
}
