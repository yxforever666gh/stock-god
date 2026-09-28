import {getCurrentScope, onScopeDispose, ref, watch} from 'vue'

function requestIdentity(clearLoading) {
  let version = 0, disposed = false
  const invalidate = () => { version++; clearLoading() }
  if (getCurrentScope()) onScopeDispose(() => { disposed = true; invalidate() })
  return {begin: () => disposed ? null : ++version, current: value => !disposed && value !== null && value === version, invalidate}
}

export function usePredictionDetail(loader) {
  const detail = ref(null), visible = ref(false), loading = ref(false), error = ref(''), selectedId = ref('')
  const request = requestIdentity(() => { loading.value = false })
  async function load(keepDetail) {
    const id = selectedId.value, version = request.begin()
    if (!request.current(version) || !id || !visible.value) return
    if (!keepDetail) detail.value = null
    loading.value = true
    error.value = ''
    try {
      const result = await loader(id)
      if (request.current(version)) detail.value = result
    } catch (reason) {
      if (request.current(version)) error.value = reason?.message || String(reason)
    } finally {
      if (request.current(version)) loading.value = false
    }
  }
  function show(id) { selectedId.value = id; visible.value = true; return load(false) }
  watch(visible, value => {
    if (!value) { request.invalidate(); detail.value = null; error.value = ''; selectedId.value = '' }
  }, {flush: 'sync'})
  return {detail, visible, loading, error, selectedId, show, refresh: () => load(true)}
}

export function usePredictionList(loader, {key = 'recommendationId', pageSize = 200} = {}) {
  const rows = ref([]), loading = ref(false), error = ref(''), hasMore = ref(true)
  let offset = 0
  const request = requestIdentity(() => { loading.value = false })
  const unique = items => [...new Map(items.map(item => [item[key], item])).values()]
  async function load(mode) {
    if (mode !== 'reset' && (loading.value || (mode === 'more' && !hasMore.value))) return
    const version = request.begin()
    if (!request.current(version)) return
    if (mode === 'reset') { rows.value = []; offset = 0; hasMore.value = true }
    const start = mode === 'head' ? 0 : offset
    loading.value = true
    error.value = ''
    try {
      const response = await loader(pageSize, start)
      if (!request.current(version)) return
      if (!Array.isArray(response)) throw new Error('列表响应格式不正确')
      if (mode === 'head') {
        const newIds = new Set(response.map(item => item[key]))
        rows.value = [...unique(response), ...rows.value.filter(item => !newIds.has(item[key]))]
        offset = Math.max(offset, response.length)
        hasMore.value ||= response.length === pageSize
      } else {
        rows.value = unique([...rows.value, ...response])
        offset = start + response.length
        hasMore.value = response.length === pageSize
      }
    } catch (reason) {
      if (request.current(version)) error.value = reason?.message || String(reason)
    } finally {
      if (request.current(version)) loading.value = false
    }
  }
  return {rows, loading, error, hasMore, refresh: () => load('reset'), refreshHead: () => load('head'), loadMore: () => load('more')}
}

export function usePredictionChart(readChart, refreshChart) {
  const chartData = ref(null), initialLoading = ref(false), refreshing = ref(false), cacheError = ref(''), refreshError = ref('')
  let selectedKey, selectedVersion
  const request = requestIdentity(() => { initialLoading.value = false; refreshing.value = false })
  async function refresh() {
    const key = selectedKey, version = selectedVersion
    if (initialLoading.value || refreshing.value || !request.current(version)) return
    refreshing.value = true
    refreshError.value = ''
    try {
      const result = await refreshChart(key)
      if (request.current(version)) { chartData.value = result; cacheError.value = '' }
    } catch (reason) {
      if (request.current(version)) refreshError.value = reason?.message || String(reason)
    } finally {
      if (request.current(version)) refreshing.value = false
    }
  }
  async function load(key) {
    const version = request.begin()
    if (!request.current(version)) return
    selectedKey = key
    selectedVersion = version
    chartData.value = null
    cacheError.value = ''
    refreshError.value = ''
    refreshing.value = false
    initialLoading.value = true
    try {
      const result = await readChart(key)
      if (request.current(version)) chartData.value = result
    } catch (reason) {
      if (request.current(version)) cacheError.value = reason?.message || String(reason)
    } finally {
      if (request.current(version)) initialLoading.value = false
    }
  }
  return {chartData, initialLoading, refreshing, cacheError, refreshError, load, refresh}
}
