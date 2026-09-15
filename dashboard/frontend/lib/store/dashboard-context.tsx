"use client"

/**
 * Wires the three runtime inputs together: the lexicon (what exists), the socket
 * (what is happening) and the layout (what the user built).
 *
 * Everything the UI knows it learns here at runtime. No component imports a
 * signal name, a unit or a range from source.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react"

import { fetchConfig, fetchLexicon } from "@/lib/api/client"
import { resolve } from "@/lib/lexicon/resolve"
import type { LexiconResponse } from "@/lib/lexicon/types"
import { validateValue, type ValidationResult } from "@/lib/lexicon/validate"
import { localStorageAdapter } from "@/lib/store/layout"
import { TelemetryStore } from "@/lib/store/telemetry"
import { DashboardSocket, type SignalRef } from "@/lib/transport/socket"
import {
  bindingsOf,
  defaultOptions,
  defaultPosition,
  emptyLayout,
  type Binding,
  type DashboardLayout,
  type ElementType,
  type LayoutElement,
} from "@/lib/types/layout"
import { useToast } from "@/lib/hooks/use-toast"

interface DashboardContextValue {
  store: TelemetryStore
  lexicon: LexiconResponse | null
  bootError: string | null
  layout: DashboardLayout | null
  dirty: boolean
  editMode: boolean
  setEditMode: (value: boolean) => void
  addElement: (type: ElementType) => void
  removeElement: (id: string) => void
  updateElement: (id: string, patch: Partial<LayoutElement>) => void
  applyPositions: (positions: Record<string, { x: number; y: number; w: number; h: number }>) => void
  saveLayout: () => Promise<void>
  write: (
    binding: Binding,
    value: unknown,
    range?: { min: number; max: number },
  ) => ValidationResult
}

const DashboardContext = createContext<DashboardContextValue | null>(null)

export function useDashboard(): DashboardContextValue {
  const context = useContext(DashboardContext)
  if (!context) throw new Error("useDashboard must be used inside DashboardProvider")
  return context
}

function subscriptionOf(layout: DashboardLayout | null): SignalRef[] {
  if (!layout) return []
  const seen = new Map<string, SignalRef>()
  for (const element of layout.elements) {
    for (const binding of bindingsOf(element)) {
      if (binding.collection !== "signals" || binding.direction !== "output") continue
      seen.set(binding.name, { name: binding.name, direction: "output" })
    }
  }
  return Array.from(seen.values())
}

export function DashboardProvider({ children }: { children: ReactNode }): JSX.Element {
  const storeRef = useRef<TelemetryStore | null>(null)
  if (storeRef.current === null) storeRef.current = new TelemetryStore()
  const store = storeRef.current

  const socketRef = useRef<DashboardSocket | null>(null)
  const seqRef = useRef(0)
  const { toast } = useToast()

  const [lexicon, setLexicon] = useState<LexiconResponse | null>(null)
  const [bootError, setBootError] = useState<string | null>(null)
  const [layout, setLayout] = useState<DashboardLayout | null>(null)
  const [dirty, setDirty] = useState(false)
  const [editMode, setEditMode] = useState(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([fetchLexicon(), fetchConfig()])
      .then(([lexiconResponse, config]) => {
        if (cancelled) return
        store.windowS = config.history_seconds
        store.appliedTimeoutMs = config.applied_timeout_ms
        setLexicon(lexiconResponse)
      })
      .catch((error: Error) => {
        if (!cancelled) setBootError(error.message)
      })
    return () => {
      cancelled = true
    }
  }, [store])

  useEffect(() => {
    const socket = new DashboardSocket(
      (frame) => {
        if (frame.t === "lexicon") {
          // A rev bump means re-resolve every binding, so refetch the document.
          fetchLexicon()
            .then(setLexicon)
            .catch(() => undefined)
        }
        store.handleFrame(frame)
      },
      (state) => store.setConnection(state),
    )
    socketRef.current = socket
    socket.start()

    const onVisibility = (): void => socket.setPaused(document.hidden)
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      document.removeEventListener("visibilitychange", onVisibility)
      socket.stop()
      socketRef.current = null
    }
  }, [store])

  useEffect(() => {
    return store.onReject((field, sent, echoed) => {
      const [, name] = field.split(":")
      toast({
        variant: "destructive",
        title: "The plant did not accept the write",
        description: `${name}: sent ${String(sent)}, plant reports ${String(
          echoed,
        )}. The reason is in the deployment log.`,
      })
    })
  }, [store, toast])

  useEffect(() => {
    if (!lexicon || layout) return
    const params = new URLSearchParams(window.location.search)
    const layoutId = params.get("layout") ?? "default"
    localStorageAdapter
      .load(layoutId)
      .then((found) =>
        setLayout(
          found ??
            emptyLayout(
              layoutId,
              lexicon.document.model.name,
              lexicon.document.lexicon_version,
            ),
        ),
      )
      .catch(() => undefined)
  }, [lexicon, layout])

  const subscription = useMemo(() => subscriptionOf(layout), [layout])
  const subscriptionKey = subscription.map((item) => item.name).join(",")

  useEffect(() => {
    const socket = socketRef.current
    if (!socket) return
    socket.setContext(lexicon?.rev ?? 0, store.windowS)
    socket.setSubscription(subscription)
    // subscription is rebuilt on every layout change; the key is what actually
    // changed, so the socket is not re-subscribed on unrelated edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscriptionKey, lexicon?.rev, store])

  const mutate = useCallback(
    (fn: (current: DashboardLayout) => DashboardLayout) => {
      setLayout((current) => (current ? fn(current) : current))
      setDirty(true)
    },
    [],
  )

  const addElement = useCallback(
    (type: ElementType) => {
      mutate((current) => {
        const nextY = current.elements.reduce(
          (max, element) => Math.max(max, element.position.y + element.position.h),
          0,
        )
        const element: LayoutElement = {
          id: `el-${Date.now().toString(36)}`,
          type,
          title: null,
          binding: null,
          position: defaultPosition(type, nextY),
          options: defaultOptions(type),
        }
        return { ...current, elements: [...current.elements, element] }
      })
    },
    [mutate],
  )

  const removeElement = useCallback(
    (id: string) => {
      mutate((current) => ({
        ...current,
        elements: current.elements.filter((element) => element.id !== id),
      }))
    },
    [mutate],
  )

  const updateElement = useCallback(
    (id: string, patch: Partial<LayoutElement>) => {
      mutate((current) => ({
        ...current,
        elements: current.elements.map((element) =>
          element.id === id ? { ...element, ...patch } : element,
        ),
      }))
    },
    [mutate],
  )

  const applyPositions = useCallback(
    (positions: Record<string, { x: number; y: number; w: number; h: number }>) => {
      if (!layout) return
      let changed = false
      const elements = layout.elements.map((element) => {
        const next = positions[element.id]
        if (!next) return element
        const same =
          next.x === element.position.x &&
          next.y === element.position.y &&
          next.w === element.position.w &&
          next.h === element.position.h
        if (same) return element
        changed = true
        return { ...element, position: { ...element.position, ...next } }
      })
      // react-grid-layout re-emits the layout it was handed, so without this
      // equality check the grid would mark itself dirty on every render.
      if (!changed) return
      setLayout({ ...layout, elements })
      setDirty(true)
    },
    [layout],
  )

  const saveLayout = useCallback(async () => {
    if (!layout) return
    await localStorageAdapter.save(layout)
    setDirty(false)
  }, [layout])

  const write = useCallback(
    (binding: Binding, value: unknown, range?: { min: number; max: number }) => {
      const descriptor = resolve(lexicon?.document ?? null, binding)
      if (!descriptor) return { ok: false, error: "binding does not resolve" }
      const result = validateValue(descriptor, value, range)
      if (!result.ok) return result

      seqRef.current += 1
      const payload = { [binding.name]: result.value as unknown }
      socketRef.current?.write(
        seqRef.current,
        binding.collection === "signals" ? payload : {},
        binding.collection === "parameters" ? payload : {},
      )
      store.markPending(binding.collection, binding.name, result.value, seqRef.current)
      return result
    },
    [lexicon, store],
  )

  const value: DashboardContextValue = {
    store,
    lexicon,
    bootError,
    layout,
    dirty,
    editMode,
    setEditMode,
    addElement,
    removeElement,
    updateElement,
    applyPositions,
    saveLayout,
    write,
  }

  return <DashboardContext.Provider value={value}>{children}</DashboardContext.Provider>
}
