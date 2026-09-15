/**
 * The pre-publish gate every control runs. The identical rules run again in the
 * backend (backend/writer.py), because /api/control is a public route.
 *
 * Out of range is blocked, never clamped: a clamped write looks accepted and
 * leaves the control and the plant permanently disagreeing (D1). A blocked value
 * is shown under the control and never reaches the wire, so the user is never
 * left guessing which field the plant refused — the plant never sees it.
 */

import type { Descriptor } from "@/lib/lexicon/types"
import type { Binding } from "@/lib/types/layout"

export interface ValidationResult {
  ok: boolean
  value?: number | boolean | string
  error?: string
}

export function isWritableBinding(binding: Binding, descriptor: Descriptor): boolean {
  if (binding.collection === "signals") return descriptor.direction === "input"
  return descriptor.tunable === true
}

export function validateValue(
  descriptor: Descriptor,
  raw: unknown,
  range?: { min: number; max: number },
): ValidationResult {
  if (descriptor.datatype === "bool") {
    return typeof raw === "boolean"
      ? { ok: true, value: raw }
      : { ok: false, error: "expects true or false" }
  }
  if (typeof raw === "boolean") {
    return { ok: false, error: `expects ${descriptor.datatype}, got a boolean` }
  }
  if (descriptor.datatype === "enum") {
    const member = (descriptor.enum ?? []).find((entry) => entry.value === raw)
    return member
      ? { ok: true, value: member.value }
      : { ok: false, error: "not one of the allowed values" }
  }

  // Never send a string for a numeric field: the sim dropped string coercion
  // deliberately, so "-8000" from a type-in is a silent no-op on the plant.
  if (typeof raw !== "number" || !Number.isFinite(raw)) {
    return { ok: false, error: "expects a number" }
  }
  if (descriptor.datatype !== "float" && !Number.isInteger(raw)) {
    return { ok: false, error: `${descriptor.datatype} expects a whole number` }
  }

  const min = range?.min ?? descriptor.min ?? Number.NEGATIVE_INFINITY
  const max = range?.max ?? descriptor.max ?? Number.POSITIVE_INFINITY
  if (raw < min || raw > max) {
    return { ok: false, error: `must be between ${min} and ${max}` }
  }
  return { ok: true, value: raw }
}

/**
 * Round-tripped floats are not bit-identical, so an exact === would report a
 * rejection on a successful write roughly whenever the step is not a binary
 * fraction.
 */
export function valuesMatch(sent: unknown, echoed: unknown): boolean {
  if (typeof sent === "number" && typeof echoed === "number") {
    return Math.abs(sent - echoed) <= 1e-9 + 1e-6 * Math.abs(echoed)
  }
  return sent === echoed
}

export function formatValue(value: number | null | undefined, decimals: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—"
  if (decimals === null) return String(value)
  return value.toFixed(decimals)
}
