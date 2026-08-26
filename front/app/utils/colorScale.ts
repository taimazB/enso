/**
 * The diverging anomaly scale, as a function of value.
 *
 * `ColorLegend` renders `/domain`'s `colorStops` as a CSS gradient; anything
 * that has to colour an individual mark needs the same stops evaluated at a
 * point instead. Both read the one list that comes from `domain.yml`, so
 * changing `vmin`/`vmax`/`colormap` there still moves the whole app at once.
 */

export interface ColorStop { value: number, color: string }

function rgb(hex: string): [number, number, number] {
  const n = Number.parseInt(hex.slice(1), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

/**
 * Build an interpolating lookup over `stops`, which must be sorted by value.
 *
 * Values outside the stops' range clamp to the end colours rather than
 * extrapolating — the map's images saturate at vmin/vmax the same way, so a +5 °C
 * day reads as "off the top of the scale" in both views.
 */
export function colorScale(stops: ColorStop[]): (value: number | null) => string {
  const fallback = '#64748b'
  if (!stops.length) return () => fallback
  const parsed = stops.map(s => ({ value: s.value, rgb: rgb(s.color) }))
  const first = parsed[0]!
  const last = parsed[parsed.length - 1]!

  return (value) => {
    if (value == null || Number.isNaN(value)) return fallback
    if (value <= first.value) return stops[0]!.color
    if (value >= last.value) return stops[stops.length - 1]!.color

    let hi = 1
    while (hi < parsed.length - 1 && parsed[hi]!.value < value) hi++
    const a = parsed[hi - 1]!
    const b = parsed[hi]!
    const span = b.value - a.value
    const t = span === 0 ? 0 : (value - a.value) / span
    const mix = a.rgb.map((c, i) => Math.round(c + (b.rgb[i]! - c) * t))
    return `rgb(${mix[0]}, ${mix[1]}, ${mix[2]})`
  }
}
