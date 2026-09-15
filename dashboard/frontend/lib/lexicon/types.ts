/**
 * Lexicon shapes, mirroring the Phase 1 schema exactly.
 *
 * Nothing in this file names a signal, a parameter or a unit: the lexicon is
 * data, and the dashboard only knows its shape.
 */

export type Datatype = "bool" | "uint" | "int" | "float" | "enum"
export type Direction = "input" | "output" | null
export type Collection = "signals" | "parameters"

export interface EnumMember {
  value: number | string | boolean
  label: string
}

export interface Descriptor {
  name: string
  label: string
  description: string
  datatype: Datatype
  unit: string | null
  default: number | string | boolean
  min: number | null
  max: number | null
  enum: EnumMember[] | null
  direction: Direction
  tunable: boolean | null
}

export interface LexiconModel {
  name: string
  label: string
  description: string
  version: string
}

/**
 * The merged view. Signals and parameters are two independently versioned DCM
 * configurations (D9); the backend merges whichever it has, so either array can
 * be empty because its configuration is missing rather than because the plant
 * has none. `CollectionState.loaded` is the only thing that tells those apart.
 */
export interface LexiconDocument {
  lexicon_version: string
  model: LexiconModel
  signals: Descriptor[]
  parameters: Descriptor[]
}

/** One DCM configuration's own state, as the backend reports it. */
export interface CollectionState {
  loaded: boolean
  rev: number
  type: string
  target_key: string
  config_id: string
  count: number
  seeded_by_this_pod: boolean
  error: string | null
}

export interface LexiconResponse {
  rev: number
  sha256: string
  fetched_at: number
  document: LexiconDocument
  signals: CollectionState
  parameters: CollectionState
}

/** A descriptor plus where it came from — enough to render a picker row. */
export interface Candidate {
  collection: Collection
  descriptor: Descriptor
}
