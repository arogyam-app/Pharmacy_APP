<script setup>
/**
 * A date input that is always dd/mm/yyyy, regardless of the browser or OS
 * locale.
 *
 * Native `<input type="date">` renders in whatever locale the browser is
 * set to -- mm/dd/yyyy on an en-US machine, which is what every "the date
 * is backwards" report has turned out to be. A `lang="en-GB"` attribute
 * looks like a fix and works on some Chromium builds, but is not reliable
 * even there and Firefox ignores it outright. This sidesteps the whole
 * question: it is a plain text field that always displays and accepts
 * dd/mm/yyyy, and emits/accepts the same ISO "yyyy-mm-dd" string as a
 * native date input, so nothing else in the app has to know the
 * difference.
 */
import { ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' }, // ISO yyyy-mm-dd, or ''
  required: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

function isoToDmy(iso) {
  if (!iso) return ''
  const [y, m, d] = iso.slice(0, 10).split('-')
  return y && m && d ? `${d}/${m}/${y}` : ''
}

/** Rejects a typed date the calendar does not have, e.g. "31/02/2026". */
function dmyToIso(dmy) {
  const match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(dmy)
  if (!match) return null
  const [, d, m, y] = match
  const date = new Date(Number(y), Number(m) - 1, Number(d))
  const real =
    date.getFullYear() === Number(y) &&
    date.getMonth() === Number(m) - 1 &&
    date.getDate() === Number(d)
  return real ? `${y}-${m}-${d}` : null
}

const text = ref(isoToDmy(props.modelValue))

// The parent can still set modelValue itself -- a "today" default, or
// loading an existing row -- so the visible text has to track it too.
watch(
  () => props.modelValue,
  (v) => {
    const formatted = isoToDmy(v)
    if (formatted !== text.value) text.value = formatted
  },
)

function onInput(event) {
  // Digits only, with the slashes inserted automatically -- typing
  // "14082026" alone becomes "14/08/2026" as it goes.
  const digits = event.target.value.replace(/\D/g, '').slice(0, 8)
  let out = digits.slice(0, 2)
  if (digits.length > 2) out += '/' + digits.slice(2, 4)
  if (digits.length > 4) out += '/' + digits.slice(4, 8)
  text.value = out

  const iso = dmyToIso(out)
  if (iso) emit('update:modelValue', iso)
  else if (out === '') emit('update:modelValue', '')
}
</script>

<template>
  <input
    :value="text"
    type="text"
    inputmode="numeric"
    placeholder="dd/mm/yyyy"
    maxlength="10"
    pattern="\d{2}/\d{2}/\d{4}"
    title="dd/mm/yyyy"
    :required="required"
    @input="onInput"
  />
</template>
