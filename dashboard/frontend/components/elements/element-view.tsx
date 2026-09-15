"use client"

/**
 * Resolves an element's bindings against the current lexicon, decides which of
 * the render states applies, and dispatches to the body for its type.
 *
 * Resolution happens on every render rather than at load time, so a lexicon rev
 * bump re-resolves every element with no layout rewrite: the stored document is
 * only ever changed by an explicit user save.
 */

import { useMemo, useState } from "react"

import { BindingPicker } from "@/components/binding/binding-picker"
import { ChartElement } from "@/components/elements/chart-element"
import { KnobElement } from "@/components/elements/knob-element"
import { ReadoutElement } from "@/components/elements/readout-element"
import { RotaryElement } from "@/components/elements/rotary-element"
import { ElementFrame, type ElementState } from "@/components/grid/element-frame"
import { isCompatible, resolve } from "@/lib/lexicon/resolve"
import type { Descriptor } from "@/lib/lexicon/types"
import { useDashboard } from "@/lib/store/dashboard-context"
import {
  bindingsOf,
  withBindings,
  type Binding,
  type ChartOptions,
  type KnobOptions,
  type LayoutElement,
  type ReadoutOptions,
} from "@/lib/types/layout"

interface ResolvedBinding {
  binding: Binding
  descriptor: Descriptor | null
}

export function ElementView({ element }: { element: LayoutElement }): JSX.Element {
  const { lexicon, editMode, removeElement, updateElement } = useDashboard()
  const [pickerOpen, setPickerOpen] = useState(false)

  const bindings = bindingsOf(element)
  const resolved: ResolvedBinding[] = useMemo(
    () =>
      bindings.map((binding) => ({
        binding,
        descriptor: resolve(lexicon?.document ?? null, binding),
      })),
    // bindings is derived from `element`, which is the real dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [element, lexicon],
  )

  const broken = resolved.filter((item) => item.descriptor === null)
  const incompatible = resolved.filter(
    (item) => item.descriptor !== null && !isCompatible(element.type, item.descriptor),
  )

  let state: ElementState = "ok"
  let message: string | null = null
  if (bindings.length === 0) {
    state = "unbound"
  } else if (broken.length > 0) {
    state = "broken"
    message = `${broken.map((item) => item.binding.name).join(", ")} is not in lexicon rev ${
      lexicon?.rev ?? 0
    }`
  } else if (incompatible.length > 0) {
    state = "broken"
    message = `${incompatible[0].binding.name} is now ${incompatible[0].descriptor?.datatype}; a ${element.type} cannot use it`
  }

  const first = resolved[0]?.descriptor ?? null
  const title = element.title ?? first?.label ?? `New ${element.type}`
  const subtitle = first ? first.name : null

  return (
    <>
      <ElementFrame
        title={title}
        subtitle={subtitle}
        state={state}
        message={message}
        editMode={editMode}
        onBind={() => setPickerOpen(true)}
        onRemove={() => removeElement(element.id)}
      >
        {state === "ok" && first ? (
          <Body element={element} resolved={resolved} descriptor={first} />
        ) : null}
      </ElementFrame>

      <BindingPicker
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        elementType={element.type}
        lexicon={lexicon?.document ?? null}
        current={bindings}
        onCommit={(next) => {
          const updated = withBindings(element, next)
          updateElement(element.id, {
            binding: updated.binding,
            bindings: updated.bindings,
          })
        }}
      />
    </>
  )
}

function Body({
  element,
  resolved,
  descriptor,
}: {
  element: LayoutElement
  resolved: ResolvedBinding[]
  descriptor: Descriptor
}): JSX.Element | null {
  if (element.type === "chart") {
    const series = resolved
      .filter((item) => item.descriptor !== null)
      .map((item) => ({ name: item.binding.name, descriptor: item.descriptor as Descriptor }))
    return <ChartElement series={series} options={element.options as ChartOptions} />
  }
  if (element.type === "knob") {
    if (descriptor.datatype === "bool" || descriptor.datatype === "enum") {
      return (
        <RotaryElement
          binding={resolved[0].binding}
          descriptor={descriptor}
        />
      )
    }
    return (
      <KnobElement
        binding={resolved[0].binding}
        descriptor={descriptor}
        options={element.options as KnobOptions}
      />
    )
  }
  if (element.type === "readout") {
    return (
      <ReadoutElement
        binding={resolved[0].binding}
        descriptor={descriptor}
        options={element.options as ReadoutOptions}
      />
    )
  }
  // switch and typein land in M2; an element of that type renders empty rather
  // than throwing, so an M2 layout opened on an M1 build still loads.
  return null
}
