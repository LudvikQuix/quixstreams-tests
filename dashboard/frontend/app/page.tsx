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
import { useTheme } from "next-themes"
import { Moon, Sun } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { useTelemetryVersion } from "@/lib/hooks/use-telemetry"
import type { CollectionState } from "@/lib/lexicon/types"
import {
  DashboardProvider,
  useDashboard,
  type LexiconUnavailable,
} from "@/lib/store/dashboard-context"

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
    lexiconError,
    retryLexicon,
    layout,
    dirty,
    editMode,
    setEditMode,
    addElement,
    saveLayout,
  } = useDashboard()
  const { store } = useTelemetryVersion()
  const { resolvedTheme, setTheme } = useTheme()
  const isDark = resolvedTheme === "dark"

  // No lexicon is an empty state, not a dead page: the service is running, it
  // retries by itself, and one seed or one DCM write fills this in without a
  // reload. Everything below here needs a lexicon to render honestly.
  if (!lexicon) {
    return <NoLexicon error={lexiconError} onRetry={retryLexicon} />
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
        {/* Half a lexicon is a usable dashboard, but only if it admits which
            half it is missing — otherwise an absent parameter configuration
            reads as a plant with no tunables and the picker just looks empty. */}
        {!lexicon.parameters.loaded ? (
          <Badge variant="warning" title={lexicon.parameters.error ?? undefined}>
            no parameters · read-only
          </Badge>
        ) : null}
        {!lexicon.signals.loaded ? (
          <Badge variant="warning" title={lexicon.signals.error ?? undefined}>
            no signals · no telemetry bindings
          </Badge>
        ) : null}

        <Separator orientation="vertical" className="hidden h-6 sm:block" />

        <Button
          size="icon"
          variant="ghost"
          aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
          onClick={() => setTheme(isDark ? "light" : "dark")}
        >
          {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </Button>

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

/**
 * The "no lexicon" empty state. Structure and affordances only — the visual
 * pass is separate.
 *
 * It says which configurations are missing, because the fix is a DCM write and
 * the type/target_key pair is what the operator needs to make it. Since D9
 * there are two of them and they fail independently, so each gets its own row:
 * "signals present, parameters 403" and "both unreachable" want different
 * actions. It never claims the dashboard is down: the dashboard is what is
 * rendering this.
 */
function NoLexicon({
  error,
  onRetry,
}: {
  error: LexiconUnavailable | null
  onRetry: () => void
}): JSX.Element {
  if (!error) {
    return <main className="p-6 text-sm">Loading lexicon…</main>
  }

  const state = error.state ?? {}
  const facts: Array<[string, string]> = [
    ["target key", String(state.lexicon_target_key ?? "—")],
    ["signals", configFact(state.signals)],
    ["parameters", configFact(state.parameters)],
    ["seeding", state.lexicon_seed_enabled === false ? "disabled" : "enabled"],
    ["DCM token", state.dcm_token_present === false ? "missing" : "present"],
  ]

  return (
    <main className="p-6">
      <h1 className="text-lg font-semibold">
        {error.missing ? "No lexicon yet" : "Lexicon unavailable"}
      </h1>
      <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
        The dashboard is running and has nothing to draw: it learns every signal,
        parameter, unit and range from a lexicon held in the Dynamic Configuration
        Manager, and no usable one has been read yet.
      </p>
      <p className="mt-2 max-w-2xl text-sm">{error.detail}</p>

      {error.state ? (
        <dl className="mt-4 grid max-w-md grid-cols-[10rem_1fr] gap-x-4 gap-y-1 text-xs">
          {facts.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-muted-foreground">{label}</dt>
              <dd>
                <code>{value}</code>
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      <div className="mt-4 flex items-center gap-2">
        <Button size="sm" onClick={onRetry}>
          Retry now
        </Button>
        <span className="text-xs text-muted-foreground">
          Retrying automatically every 10 s. The service reports its own state on{" "}
          <code>/api/healthz</code>.
        </span>
      </div>
    </main>
  )
}

/**
 * One fact-list row for one DCM configuration. The 503 body is untyped — it is
 * whatever the backend sent — so every field is narrowed before it is read, and
 * a shape this page does not recognise degrades to a dash rather than throwing
 * inside the very component that exists to explain a failure.
 */
function configFact(raw: unknown): string {
  if (typeof raw !== "object" || raw === null) return "—"
  const state = raw as Partial<CollectionState>
  const label = `${state.type ?? "?"} → ${state.config_id ?? "?"}`
  if (state.loaded) return `${label} · rev ${state.rev ?? 0} · ${state.count ?? 0} entries`
  return `${label} · ${state.error ?? "not loaded"}`
}
