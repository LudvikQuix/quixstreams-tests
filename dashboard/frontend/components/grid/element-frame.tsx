"use client"

/**
 * Chrome shared by every element: title, state badge, and the edit-mode affordances.
 *
 * A binding that no longer resolves renders `broken` and keeps its binding. The
 * stored layout is never rewritten by a lexicon change, so a lexicon rollback
 * heals every broken element automatically and an afternoon of grid building
 * survives a bad seed.
 */

import type { ReactNode } from "react"
import { Link2Off, Settings2, Trash2 } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

/**
 * Staleness is deliberately NOT one of these: `stale_after_ms` is a per-element
 * option and the body is what knows whether its own value has aged, so a stale
 * readout dims itself rather than the frame badging it.
 */
export type ElementState = "ok" | "unbound" | "broken"

interface ElementFrameProps {
  title: string
  subtitle?: string | null
  state: ElementState
  message?: string | null
  editMode: boolean
  onBind: () => void
  onRemove: () => void
  children: ReactNode
}

const STATE_BADGE: Record<ElementState, string | null> = {
  ok: null,
  unbound: "unbound",
  broken: "binding lost",
}

export function ElementFrame({
  title,
  subtitle,
  state,
  message,
  editMode,
  onBind,
  onRemove,
  children,
}: ElementFrameProps): JSX.Element {
  const badge = STATE_BADGE[state]
  return (
    <Card
      className={cn(
        "flex h-full w-full flex-col overflow-hidden",
        state === "unbound" && "border-dashed",
        state === "broken" && "border-warning",
      )}
    >
      <div
        className={cn(
          "drag-handle flex items-center justify-between gap-2 border-b px-2 py-1",
          editMode && "cursor-move",
        )}
      >
        <div className="min-w-0">
          <div className="truncate text-xs font-medium sm:text-sm">{title}</div>
          {subtitle ? (
            <div className="hidden truncate text-[10px] text-muted-foreground sm:block">
              {subtitle}
            </div>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {badge ? (
            <Badge variant="outline" className="text-[10px]">
              {badge}
            </Badge>
          ) : null}
          {editMode ? (
            <>
              <Button
                size="icon"
                variant="ghost"
                className="no-drag h-6 w-6"
                aria-label="Bind"
                onClick={onBind}
              >
                <Settings2 className="h-3 w-3" />
              </Button>
              <Button
                size="icon"
                variant="ghost"
                className="no-drag h-6 w-6"
                aria-label="Remove"
                onClick={onRemove}
              >
                <Trash2 className="h-3 w-3" />
              </Button>
            </>
          ) : null}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col justify-center p-2">
        {state === "unbound" || state === "broken" ? (
          <div className="flex flex-col items-center gap-2 text-center text-xs text-muted-foreground">
            <Link2Off className="h-4 w-4" />
            <span>{message ?? "Nothing bound yet"}</span>
            <Button size="sm" variant="secondary" className="no-drag" onClick={onBind}>
              {state === "broken" ? "Re-bind" : "Bind…"}
            </Button>
          </div>
        ) : (
          children
        )}
      </div>
    </Card>
  )
}
