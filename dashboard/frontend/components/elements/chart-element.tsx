"use client"

/**
 * uPlot line chart over the rolling window. Takes 1..N bindings (CLAUDE.md §4,
 * D7): overlaying two signals is the point of a chart, and a one-series-per-
 * element grid makes the interesting comparison impossible.
 *
 * Canvas, not SVG: a virtual-DOM chart re-renders a node tree per sample and
 * will not hold 10 Hz across a dozen charts. Redraws run on the shared rAF tick,
 * so chart count costs frames' worth of drawing, not React renders.
 */

import { useEffect, useRef } from "react"
import uPlot from "uplot"
import { useTheme } from "next-themes"

import { useDashboard } from "@/lib/store/dashboard-context"
import { onAnimationFrame } from "@/lib/store/raf"
import type { Descriptor } from "@/lib/lexicon/types"
import type { ChartOptions } from "@/lib/types/layout"

interface ChartElementProps {
  series: { name: string; descriptor: Descriptor }[]
  options: ChartOptions
}

const PALETTE = ["#38bdf8", "#f97316", "#a78bfa", "#34d399", "#f472b6", "#facc15"]

/** Read a CSS custom property and wrap it as an hsl() colour string. */
function cssVar(name: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return `hsl(${raw})`
}

export function ChartElement({ series, options }: ChartElementProps): JSX.Element {
  const { store } = useDashboard()
  const { resolvedTheme } = useTheme()
  const hostRef = useRef<HTMLDivElement | null>(null)
  const namesKey = series.map((entry) => entry.name).join(",")

  useEffect(() => {
    const host = hostRef.current
    if (!host) return undefined

    const hintMin = options.y_min ?? series[0]?.descriptor.min ?? null
    const hintMax = options.y_max ?? series[0]?.descriptor.max ?? null

    const axisStroke = cssVar("--muted-foreground")
    const gridStroke = cssVar("--border")

    const plot = new uPlot(
      {
        width: host.clientWidth || 300,
        height: host.clientHeight || 160,
        legend: { show: series.length > 1 },
        cursor: { show: false },
        scales: {
          x: { time: true },
          y: options.y_autoscale
            ? { auto: true }
            : {
                auto: true,
                // The descriptor's min/max is a display hint for an output
                // signal, not a limit: the plant may legitimately exceed it in a
                // transient, so the axis expands instead of the line clipping.
                range: (_u, dataMin, dataMax) =>
                  [
                    hintMin === null ? dataMin : Math.min(hintMin, dataMin),
                    hintMax === null ? dataMax : Math.max(hintMax, dataMax),
                  ] as [number, number],
              },
        },
        axes: [
          {
            stroke: axisStroke,
            ticks: { stroke: gridStroke, width: 1 },
            grid: { stroke: gridStroke, width: 1 },
          },
          {
            label: series[0]?.descriptor.unit ?? undefined,
            stroke: axisStroke,
            ticks: { stroke: gridStroke, width: 1 },
            grid: { stroke: gridStroke, width: 1 },
          },
        ],
        series: [
          {},
          ...series.map((entry, index) => ({
            label: entry.descriptor.label,
            stroke: options.stroke ?? PALETTE[index % PALETTE.length],
            width: 1.5,
            points: { show: options.points },
          })),
        ],
      },
      [[], ...series.map(() => [] as (number | null)[])] as uPlot.AlignedData,
      host,
    )

    const observer = new ResizeObserver(() => {
      plot.setSize({ width: host.clientWidth, height: host.clientHeight })
    })
    observer.observe(host)

    const stop = onAnimationFrame(() => {
      const buckets = series.map((entry) => store.seriesFor(entry.name))
      const length = buckets.reduce(
        (min, bucket) => Math.min(min, bucket?.ts.length ?? 0),
        Number.MAX_SAFE_INTEGER,
      )
      if (!Number.isFinite(length) || length === 0) return
      const base = buckets[0]
      if (!base) return
      // Every series is fed from the same rows and reset together by a snapshot,
      // so they stay index-aligned; the min length guards the first frame after
      // a subscription change.
      const xs = base.ts.slice(base.ts.length - length).map((ms) => ms / 1000)
      const data = [
        xs,
        ...buckets.map((bucket) => (bucket ? bucket.v.slice(bucket.v.length - length) : [])),
      ] as uPlot.AlignedData
      plot.setData(data)
    })

    return () => {
      stop()
      observer.disconnect()
      plot.destroy()
    }
    // Rebuilt when the bound series, the visual options, or the theme changes.
    // `series` is a fresh array each render, so the name list is the real
    // dependency. `resolvedTheme` triggers a full rebuild so axis and grid
    // colours are re-read from the current CSS variables.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [namesKey, options.y_autoscale, options.y_min, options.y_max, options.stroke, store, resolvedTheme])

  return <div ref={hostRef} className="h-full w-full min-h-0" />
}
