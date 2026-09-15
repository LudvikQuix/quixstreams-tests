"use client"

/**
 * The binding picker. Every row comes from the lexicon and nowhere else.
 *
 * The candidate list is filtered by the element type's row in the §6.6 table, so
 * D1 is enforced before the user can make a mistake: a `tunable: false`
 * parameter is REMOVED from a control's list rather than shown disabled - a
 * greyed row invites a support question, an absent row does not. It stays
 * reachable through a readout, read-only.
 */

import { useMemo, useState } from "react"
import { Check } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { bindingKey, bindingOf, candidates, groupOf } from "@/lib/lexicon/resolve"
import type { Candidate, LexiconDocument } from "@/lib/lexicon/types"
import type { Binding, ElementType } from "@/lib/types/layout"

interface BindingPickerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  elementType: ElementType
  lexicon: LexiconDocument | null
  current: Binding[]
  onCommit: (bindings: Binding[]) => void
}

function rangeLabel(candidate: Candidate): string {
  const { descriptor } = candidate
  if (descriptor.datatype === "enum") {
    return (descriptor.enum ?? []).map((member) => member.label).join(" / ")
  }
  if (descriptor.min === null || descriptor.max === null) return descriptor.datatype
  return `${descriptor.min} … ${descriptor.max}`
}

export function BindingPicker({
  open,
  onOpenChange,
  elementType,
  lexicon,
  current,
  onCommit,
}: BindingPickerProps): JSX.Element {
  const multi = elementType === "chart"
  const [selected, setSelected] = useState<Binding[]>(current)

  const grouped = useMemo(() => {
    const groups = new Map<string, Candidate[]>()
    for (const candidate of candidates(lexicon, elementType)) {
      const group = groupOf(candidate)
      groups.set(group, [...(groups.get(group) ?? []), candidate])
    }
    return Array.from(groups.entries())
  }, [lexicon, elementType])

  const toggle = (candidate: Candidate): void => {
    const binding = bindingOf(candidate)
    if (!multi) {
      onCommit([binding])
      onOpenChange(false)
      return
    }
    const key = bindingKey(binding)
    const exists = selected.some((item) => bindingKey(item) === key)
    setSelected(
      exists ? selected.filter((item) => bindingKey(item) !== key) : [...selected, binding],
    )
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (next) setSelected(current)
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Bind this {elementType}</DialogTitle>
          <DialogDescription>
            {multi
              ? "A chart may overlay several series. Pick one or more."
              : "Pick the lexicon entry this element drives or displays."}
          </DialogDescription>
        </DialogHeader>

        <Command className="max-h-[60vh]">
          <CommandInput placeholder="Search by name, label or description…" />
          <CommandList>
            <CommandEmpty>
              No entry in this lexicon can be bound to a {elementType}.
            </CommandEmpty>
            {grouped.map(([group, items]) => (
              <CommandGroup key={group} heading={group}>
                {items.map((candidate) => {
                  const key = bindingKey(bindingOf(candidate))
                  const isSelected = selected.some((item) => bindingKey(item) === key)
                  return (
                    <CommandItem
                      key={key}
                      value={`${candidate.descriptor.label} ${candidate.descriptor.name} ${candidate.descriptor.description}`}
                      onSelect={() => toggle(candidate)}
                    >
                      <div className="flex w-full items-center justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            {isSelected ? <Check className="h-3 w-3" /> : null}
                            <span className="truncate">{candidate.descriptor.label}</span>
                            <code className="truncate text-[10px] text-muted-foreground">
                              {candidate.descriptor.name}
                            </code>
                          </div>
                          <div className="truncate text-[10px] text-muted-foreground">
                            {candidate.descriptor.description}
                          </div>
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                          {candidate.descriptor.unit ? (
                            <Badge variant="secondary" className="text-[10px]">
                              {candidate.descriptor.unit}
                            </Badge>
                          ) : null}
                          <Badge variant="outline" className="text-[10px]">
                            {rangeLabel(candidate)}
                          </Badge>
                        </div>
                      </div>
                    </CommandItem>
                  )
                })}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>

        {multi ? (
          <DialogFooter>
            <Button
              variant="secondary"
              onClick={() => {
                onCommit(selected)
                onOpenChange(false)
              }}
            >
              Apply {selected.length} series
            </Button>
          </DialogFooter>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}
