/**
 * Binding resolution and the picker's candidate rules.
 *
 * This is the only place that decides what may be bound to what. The rules come
 * from CLAUDE.md §4 (as amended by D7) and the spec's §6.6 table; D1 — a
 * `tunable: false` parameter can never reach a control — is enforced here and
 * nowhere else, so there is one gate to audit.
 */

import type {
  Candidate,
  Datatype,
  Descriptor,
  Direction,
  LexiconDocument,
} from "@/lib/lexicon/types"
import type { Binding, ElementType } from "@/lib/types/layout"

const NUMERIC: Datatype[] = ["uint", "int", "float"]
const ALL: Datatype[] = ["bool", "uint", "int", "float", "enum"]

export function isControl(type: ElementType): boolean {
  return type === "switch" || type === "knob" || type === "typein"
}

export function allowedDatatypes(type: ElementType): Datatype[] {
  if (type === "switch") return ["bool", "enum"]
  if (type === "knob" || type === "typein") return NUMERIC
  if (type === "chart") return NUMERIC
  return ALL
}

/** Which signal directions this element type may bind. Parameters are separate. */
export function allowedDirections(type: ElementType): Direction[] {
  return isControl(type) ? ["input"] : ["output"]
}

export function bindingKey(binding: Binding): string {
  return `${binding.collection}:${binding.name}:${binding.direction ?? ""}`
}

export function resolve(
  lexicon: LexiconDocument | null,
  binding: Binding | null,
): Descriptor | null {
  if (!lexicon || !binding) return null
  if (binding.collection === "signals") {
    return (
      lexicon.signals.find(
        (entry) => entry.name === binding.name && entry.direction === binding.direction,
      ) ?? null
    )
  }
  return lexicon.parameters.find((entry) => entry.name === binding.name) ?? null
}

export function isCompatible(type: ElementType, descriptor: Descriptor): boolean {
  return allowedDatatypes(type).includes(descriptor.datatype)
}

/**
 * A `tunable: false` parameter is removed from a control's candidate list, not
 * disabled: a greyed row invites a support question, an absent row does not. It
 * stays reachable through a readout, read-only.
 */
export function candidates(
  lexicon: LexiconDocument | null,
  type: ElementType,
): Candidate[] {
  if (!lexicon) return []
  const datatypes = allowedDatatypes(type)
  const directions = allowedDirections(type)

  const signals = lexicon.signals
    .filter((entry) => directions.includes(entry.direction))
    .filter((entry) => datatypes.includes(entry.datatype))
    .map((descriptor) => ({ collection: "signals" as const, descriptor }))

  const parameters = lexicon.parameters
    .filter((entry) => (isControl(type) ? entry.tunable === true : true))
    .filter((entry) => datatypes.includes(entry.datatype))
    .map((descriptor) => ({ collection: "parameters" as const, descriptor }))

  return [...signals, ...parameters]
}

export function groupOf(candidate: Candidate): string {
  if (candidate.collection === "signals") {
    return candidate.descriptor.direction === "input" ? "Input signals" : "Output signals"
  }
  return candidate.descriptor.tunable
    ? "Tunable parameters"
    : "Fixed parameters (read-only)"
}

export function bindingOf(candidate: Candidate): Binding {
  return {
    collection: candidate.collection,
    name: candidate.descriptor.name,
    direction: candidate.collection === "signals" ? candidate.descriptor.direction : null,
  }
}

/** Element options may narrow the descriptor range, never widen it. */
export function effectiveRange(
  descriptor: Descriptor,
  min: number | null | undefined,
  max: number | null | undefined,
): { min: number; max: number } {
  const low = descriptor.min ?? 0
  const high = descriptor.max ?? 1
  return {
    min: min === null || min === undefined ? low : Math.max(low, min),
    max: max === null || max === undefined ? high : Math.min(high, max),
  }
}

export function defaultStep(descriptor: Descriptor, range: { min: number; max: number }): number {
  if (descriptor.datatype === "float") return (range.max - range.min) / 200
  return 1
}

/** A control may write an input signal or a tunable parameter. Nothing else. */
export function isWritable(candidate: Candidate): boolean {
  if (candidate.collection === "signals") return candidate.descriptor.direction === "input"
  return candidate.descriptor.tunable === true
}
