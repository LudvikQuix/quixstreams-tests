"use client"

/**
 * The grid itself. react-grid-layout's Responsive + WidthProvider does the
 * reflow, so no breakpoint is hand-written anywhere.
 *
 * Positions are stored once, in the 12-column `lg` space. Narrower breakpoints
 * are derived at render time and never persisted - storing five variants would
 * triple the document and immediately drift - so a layout change is only written
 * back while the viewport is actually at `lg`.
 */

import { useRef } from "react"
import { Responsive, WidthProvider, type Layout } from "react-grid-layout"

import { ElementView } from "@/components/elements/element-view"
import { useDashboard } from "@/lib/store/dashboard-context"

const ResponsiveGridLayout = WidthProvider(Responsive)

export function GridCanvas(): JSX.Element {
  const { layout, editMode, applyPositions } = useDashboard()
  const breakpoint = useRef<string>("lg")

  if (!layout) {
    return (
      <div className="p-6 text-sm text-muted-foreground">Loading layout…</div>
    )
  }

  if (layout.elements.length === 0) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Empty grid. Switch to Edit and add a readout, a chart or a knob.
      </div>
    )
  }

  const positions: Layout[] = layout.elements.map((element) => ({
    i: element.id,
    x: element.position.x,
    y: element.position.y,
    w: element.position.w,
    h: element.position.h,
    minW: element.position.min_w,
    minH: element.position.min_h,
  }))

  return (
    <ResponsiveGridLayout
      className="layout"
      layouts={{ lg: positions }}
      breakpoints={{ lg: 1200, md: 996, sm: 768, xs: 480, xxs: 0 }}
      cols={{ lg: 12, md: 10, sm: 6, xs: 4, xxs: 2 }}
      rowHeight={layout.grid.row_height}
      margin={layout.grid.margin}
      compactType={layout.grid.compact_type}
      isDraggable={editMode}
      isResizable={editMode}
      draggableHandle=".drag-handle"
      draggableCancel=".no-drag"
      onBreakpointChange={(next) => {
        breakpoint.current = next
      }}
      onLayoutChange={(current) => {
        if (!editMode || breakpoint.current !== "lg") return
        const next: Record<string, { x: number; y: number; w: number; h: number }> = {}
        for (const item of current) {
          next[item.i] = { x: item.x, y: item.y, w: item.w, h: item.h }
        }
        applyPositions(next)
      }}
    >
      {layout.elements.map((element) => (
        <div key={element.id} className="min-h-0">
          <ElementView element={element} />
        </div>
      ))}
    </ResponsiveGridLayout>
  )
}
