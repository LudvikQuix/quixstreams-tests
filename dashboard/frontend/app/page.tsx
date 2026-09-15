"use client"

/**
 * The single route. Layout selection is a query parameter (`/?layout=<id>`),
 * never a dynamic segment: a static export has to know every route at build
 * time and cannot know a layout id then.
 *
 * Markup here is functional, not designed — structure, states and affordances
 * only. Visual polish is a separate pass.
 */

import dynamic from "next/dynamic"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { useTelemetryVersion } from "@/lib/hooks/use-telemetry"
import { DashboardProvider, useDashboard } from "@/lib/store/dashboard-context"

const GridCanvas = dynamic(
  () => import("@/components/grid/grid-canvas").then((module) => module.GridCanvas),
  { ssr: false, loading: () => <div className="p-6 text-sm">Loading grid…</div> },
)

export default function Page(): JSX.Element {
  return (
    <DashboardProvider>
      <Shell />
    </DashboardProvider>
  )
}

function Shell(): JSX.Element {
  const {
    lexicon,
    bootError,
    layout,
    dirty,
    editMode,
    setEditMode,
    addElement,
    saveLayout,
  } = useDashboard()
  const { store } = useTelemetryVersion()

  if (bootError) {
    return (
      <main className="p-6">
        <h1 className="text-lg font-semibold">Dashboard unavailable</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          The backend could not serve a lexicon: {bootError}. The service reports its
          own state on <code>/healthz</code>.
        </p>
      </main>
    )
  }

  const connectionTone =
    store.connection === "open" ? "success" : store.connection === "connecting" ? "warning" : "destructive"

  return (
    <main className="flex min-h-screen flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <div className="mr-auto min-w-0">
          <div className="truncate text-sm font-semibold sm:text-base">
            {lexicon?.document.model.label ?? "SIL Dashboard"}
          </div>
          <div className="truncate text-[10px] text-muted-foreground">
            {layout ? `${layout.name} · ` : ""}lexicon rev {lexicon?.rev ?? 0}
          </div>
        </div>

        <Badge variant={connectionTone}>{store.connection}</Badge>
        {store.status.telemetry_stale ? <Badge variant="warning">no telemetry</Badge> : null}
        {store.dropped > 0 ? <Badge variant="outline">lagging ({store.dropped})</Badge> : null}

        <Separator orientation="vertical" className="hidden h-6 sm:block" />

        {editMode ? (
          <>
            <Button size="sm" variant="secondary" onClick={() => addElement("readout")}>
              + Readout
            </Button>
            <Button size="sm" variant="secondary" onClick={() => addElement("chart")}>
              + Chart
            </Button>
            <Button size="sm" variant="secondary" onClick={() => addElement("knob")}>
              + Knob
            </Button>
            <Button size="sm" variant={dirty ? "default" : "outline"} onClick={saveLayout}>
              {dirty ? "Save layout*" : "Save layout"}
            </Button>
          </>
        ) : null}

        <Button size="sm" variant={editMode ? "default" : "outline"} onClick={() => setEditMode(!editMode)}>
          {editMode ? "Done" : "Edit"}
        </Button>
      </header>

      <div className="flex-1 overflow-auto p-2">
        <GridCanvas />
      </div>
    </main>
  )
}
