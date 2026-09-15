/**
 * One requestAnimationFrame loop shared by every chart.
 *
 * Twelve charts each running their own rAF would still be capped by the display
 * refresh, but each would redraw independently and the work would scale with
 * chart count per frame. Sharing the tick means twelve charts cost one frame's
 * scheduling, not twelve.
 */

type Tick = () => void

const subscribers = new Set<Tick>()
let handle: number | null = null

function loop(): void {
  handle = null
  subscribers.forEach((tick) => tick())
  if (subscribers.size > 0) handle = requestAnimationFrame(loop)
}

export function onAnimationFrame(tick: Tick): () => void {
  subscribers.add(tick)
  if (handle === null) handle = requestAnimationFrame(loop)
  return () => {
    subscribers.delete(tick)
    if (subscribers.size === 0 && handle !== null) {
      cancelAnimationFrame(handle)
      handle = null
    }
  }
}
