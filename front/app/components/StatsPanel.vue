<template>
  <div v-if="!stats.n" class="flex grow items-center justify-center px-6 text-center text-sm text-muted">
    <UIcon v-if="loading" name="i-mdi-loading" class="size-5 animate-spin" />
    <span v-else>{{ emptyMessage }}</span>
  </div>

  <!-- `@container`, not viewport breakpoints: the dock is dragged between 420px
       and 1000px, so how many cards fit is a question about this element. -->
  <div v-else class="@container flex min-h-0 grow flex-col gap-4 overflow-y-auto px-1">
    <!-- The headline is the bucket the map is on, not the latest one: the panel
         describes the frame on screen, so stepping the date moves this. -->
    <section class="shrink-0">
      <p class="text-sm text-muted">
        {{ variableLabel }} &middot; {{ currentLabel }}
      </p>
      <p class="mt-1 flex items-baseline gap-2">
        <span class="text-5xl font-semibold leading-none tabular-nums" :style="{ color: currentColor }">
          {{ stats.current ? fmt(stats.current.value) : '—' }}
        </span>
        <span v-if="unit" class="text-xl text-muted">{{ unit }}</span>
        <span v-if="valueGloss" class="text-base text-muted">{{ valueGloss }}</span>
      </p>
      <p v-if="rankLine" class="mt-2 text-sm text-dimmed">{{ rankLine }}</p>
    </section>

    <!-- Cards rather than a definition list. The strip chart that used to sit
         here was a decoy: the full series with its axes, tooltip and dataZoom is
         already on screen to the right, and a second silent copy of it competed
         for attention with the numbers, which are the reason to open this tab.
         The space it freed goes to the labels, which now read at the same size
         as the rest of the app instead of shrinking away from their values. -->
    <!-- The cards take the height the panel has, up to a cap. Five numbers
         cannot honestly fill a 900px dock — past this the boxes stop reading as
         a set of figures and start reading as empty panels — so what is left
         over is left over, with the basis note pinned to the bottom of it. -->
    <div class="grid max-h-92 grow auto-rows-fr grid-cols-1 gap-2 @xs:grid-cols-2">
      <div
        v-for="(row, i) in rows"
        :key="row.label"
        class="flex flex-col justify-center rounded-lg border border-default bg-elevated/40 px-3 py-2.5"
        :class="spanBoth(i) ? '@xs:col-span-2' : ''"
      >
        <p class="truncate text-sm text-muted">{{ row.label }}</p>
        <p class="mt-0.5 flex items-baseline gap-1.5">
          <span class="text-2xl font-semibold tabular-nums text-highlighted">{{ row.value }}</span>
          <span v-if="row.unit" class="text-sm text-muted">{{ row.unit }}</span>
        </p>
        <p class="mt-0.5 truncate text-xs text-dimmed">{{ row.note || ' ' }}</p>
      </div>
    </div>

    <!-- Said once, plainly, rather than qualifying each card: every number above
         is over buckets at the current period, which is what the chart and the
         map beside it are also showing. -->
    <!-- The area-mean note goes ABOVE the basis line and is not folded into it:
         one says what the numbers are computed over, the other says what a
         decimal category is, and the second is only ever shown in region scope. -->
    <div class="mt-auto shrink-0 pt-2 text-xs leading-snug text-dimmed">
      <p v-if="areaMeanNote" class="mb-1.5 text-muted">{{ areaMeanNote }}</p>
      <p>{{ basisNote }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Series } from '~/stores/main'
import type { ColorStop } from '~/utils/colorScale'
import { NO_CLASS_COLOR, colorScale } from '~/utils/colorScale'
import { bucketLabel, type Period } from '~/utils/periods'
import { ordinal, summarise, wholeClasses } from '~/utils/stats'

const props = defineProps<{
  /** Whichever series the current scope names — a cell's or a region's. */
  series: Series | null
  loading?: boolean
  emptyMessage?: string
  /** The active variable's stops, spread over the range in force. */
  stops: ColorStop[]
  categorical?: boolean
  unit?: string
  /** Decimals to print, from /domain's per-variable `precision`. */
  precision?: number
  /** Print a leading + on positive values. True for a signed field. */
  signed?: boolean
  variableLabel: string
  period: Period
  selectedDate?: string | null
}>()

const stats = computed(() => summarise(props.series, props.selectedDate))
const scale = computed(() => colorScale(props.stops, props.categorical ? NO_CLASS_COLOR : undefined))

const currentColor = computed(() =>
  stats.value.current ? scale.value(stats.value.current.value) : 'var(--ui-text-muted)')

const currentLabel = computed(() =>
  stats.value.current ? bucketLabel(stats.value.current.date, props.period) : 'no value for this bucket')

function fmt(value: number): string {
  // `/domain`'s `precision` for `mhw` is 0, which is right for the thing it
  // describes — a class is a whole number and "Cat 3.00" is nonsense. It is
  // wrong for an area mean of classes: Nino 3.4 spends the entire archive under
  // 2, so rounding to 0 decimals prints its record week as a flat `2` and its
  // archive mean as `0`, destroying exactly the signal a region series carries.
  // The chart next door already prints these to two decimals; this keeps the
  // panel agreeing with it.
  const digits = isAreaMeanOfClasses.value ? 2 : props.precision ?? 2
  const text = value.toFixed(digits)
  return props.signed && value > 0 ? `+${text}` : text
}

/**
 * A category's name, but only where the value actually *is* a category.
 *
 * The test is integrality rather than the variable being categorical, and that
 * is the whole point: a point reads a whole class (3 -> Severe), while a region
 * reads a cos(lat)-weighted **area mean** of classes (0.855), which is not a
 * class and must not be named as one. The one integer a region hits in practice
 * is 0, where "no heatwave" is exactly right.
 */
const className = computed(() => {
  const value = stats.value.current?.value
  if (!props.categorical || value == null || !Number.isInteger(value)) return null
  if (value === 0) return 'no heatwave'
  return props.stops.find(s => s.value === value)?.label ?? null
})

/**
 * Whether this series' categories are whole classes or area means of them —
 * the same test `TimeseriesChart` puts on its y-axis, from the same helper.
 */
const isWholeClasses = computed(() => wholeClasses(props.series))

/** True where a decimal category needs explaining: `mhw` in region scope. */
const isAreaMeanOfClasses = computed(() => !!props.categorical && !isWholeClasses.value)

/**
 * What rides beside the headline number.
 *
 * `mhw` has no unit, so a region's `0.59` would otherwise sit on the card
 * entirely unlabelled — and the one reading it has every reason to take it for a
 * category, which is the misreading this whole pair of computeds exists to
 * prevent. A class gets its name; a mean of classes gets said to be one.
 */
const valueGloss = computed(() =>
  className.value ?? (isAreaMeanOfClasses.value ? 'mean category' : null))

/**
 * Rank of the bucket on the map among every bucket in the archive, largest
 * first. "Most severe" rather than "warmest" for `mhw`, whose values are not
 * temperatures — the same distinction `index.vue`'s `rankOrder` makes.
 */
const rankLine = computed(() => {
  const { rank, n } = stats.value
  if (rank == null) return null
  const noun = { daily: 'days', weekly: 'weeks', monthly: 'months' }[props.period]
  return `${ordinal(rank)} ${props.categorical ? 'most severe' : 'warmest'} of ${n.toLocaleString()} ${noun}`
})

/**
 * The unit rides beside the value rather than inside it, so the numerals in a
 * column of cards line up on their own — `tabular-nums` buys nothing if half of
 * each string is a suffix of a different width.
 */
const rows = computed(() => {
  const s = stats.value
  const noun = { daily: 'day', weekly: 'week', monthly: 'month' }[props.period]
  const unit = props.unit ?? ''
  const out: Array<{ label: string, value: string, unit: string, note: string }> = []

  if (s.max) out.push({ label: `Highest ${noun}`, value: fmt(s.max.value), unit, note: s.max.date })
  if (s.min) out.push({ label: `Lowest ${noun}`, value: fmt(s.min.value), unit, note: s.min.date })
  if (s.recentMean != null) {
    out.push({
      label: 'Last 12 months',
      value: fmt(s.recentMean),
      unit,
      // The comparison is the reason the card is here: a mean with nothing to
      // read it against is a number, not a finding.
      note: s.mean != null ? `${fmt(s.recentMean - s.mean)} vs mean` : '',
    })
  }
  // A trend in categories per decade would be a number without a unit behind
  // it — the classes are ordinal, so the distance between Cat 1 and Cat 2 is
  // not a quantity that can be averaged into a slope. The count of affected
  // buckets answers the same question honestly.
  if (props.categorical) {
    if (s.activeRecent) {
      out.push({
        label: `Heatwave ${noun}s`,
        value: `${s.activeRecent.hits} of ${s.activeRecent.of}`,
        unit: '',
        note: 'last 12 months',
      })
    }
  }
  else if (s.trendPerDecade != null) {
    out.push({
      label: 'Trend',
      value: `${s.trendPerDecade > 0 ? '+' : ''}${s.trendPerDecade.toFixed(2)}`,
      unit,
      note: 'per decade',
    })
  }
  return out
})

/** An odd last card fills the row rather than leaving a hole beside it. */
function spanBoth(i: number): boolean {
  return i === rows.value.length - 1 && rows.value.length % 2 === 1
}

const basisNote = computed(() => {
  const noun = { daily: 'daily', weekly: 'weekly', monthly: 'monthly' }[props.period]
  const span = props.series?.dates?.length
    ? `${props.series.dates[0]!.slice(0, 4)}–${props.series.dates[props.series.dates.length - 1]!.slice(0, 4)}`
    : ''
  // Worth saying for mhw specifically: a weekly bucket is the MAX of its days
  // (shared/buckets.py), so these are weeks that reached a category, not days.
  const reduction = props.categorical && props.period !== 'daily'
    ? ` A ${props.period === 'weekly' ? 'week' : 'month'} takes its worst day's category.`
    : ''
  return `Over ${noun} values, ${span}.${reduction}`
})

/**
 * What a decimal category means, said where the decimal is.
 *
 * A region's `mhw` value is `sum(cat * cos(lat)) / sum(cos(lat))` over every
 * ocean cell in the box, heatwave-free ones included as 0 — so it is one number
 * standing for two things at once, severity and extent, and it cannot be read as
 * a category. The worked pair is the part worth printing: it is what makes the
 * degeneracy concrete, and without it "area mean" reads as a technicality rather
 * than as a warning that 0.5 does not mean "half a category".
 *
 * Only in region scope. At a point the value IS a class, `className` names it,
 * and this would be noise.
 */
const areaMeanNote = computed(() => {
  if (!isAreaMeanOfClasses.value) return ''
  return 'This is an area mean of NOAA\u2019s five classes, not a class: every ocean cell in '
    + 'the box counts, heatwave-free ones as 0. It mixes severity with extent, so it cannot be '
    + 'read as a category \u2014 half the box at Cat 1 and a tenth of it at Cat 5 both come to '
    + 'about 0.5.'
})
</script>
