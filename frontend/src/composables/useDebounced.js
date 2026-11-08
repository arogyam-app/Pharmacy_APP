import { ref, watch } from 'vue'

/**
 * Mirrors a source ref once it has been quiet for `delay` ms.
 * Keeps the search box from firing a request on every keystroke.
 */
export function useDebounced(source, delay = 250) {
  const debounced = ref(source.value)
  let timer = null

  watch(source, (value) => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      debounced.value = value
    }, delay)
  })

  return debounced
}
