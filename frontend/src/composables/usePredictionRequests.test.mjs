import assert from 'node:assert/strict'
import test from 'node:test'
import {effectScope} from 'vue'
import {usePredictionChart, usePredictionDetail, usePredictionList} from './usePredictionRequests.js'

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return {promise, resolve, reject}
}
const flush = async () => { await Promise.resolve(); await Promise.resolve() }

test('details reject superseded responses and closing clears loading and late failures', async () => {
  const a = deferred(), b = deferred(), c = deferred()
  const scope = effectScope()
  const resource = scope.run(() => usePredictionDetail(id => ({a, b, c})[id].promise))
  const first = resource.show('a'), second = resource.show('b')
  b.resolve({id: 'b'}); await second
  a.resolve({id: 'a'}); await first
  assert.equal(resource.detail.value.id, 'b')
  const last = resource.show('c')
  resource.visible.value = false
  c.reject(new Error('obsolete failure')); await last
  assert.equal(resource.detail.value, null)
  assert.equal(resource.loading.value, false)
  assert.equal(resource.error.value, '')
  scope.stop()
})

test('failed detail stops the spinner and can be retried', async () => {
  let fail = true
  const scope = effectScope()
  const resource = scope.run(() => usePredictionDetail(async id => { if (fail) throw new Error('offline'); return {id} }))
  await resource.show('a')
  assert.equal(resource.loading.value, false)
  assert.equal(resource.error.value, 'offline')
  fail = false; await resource.refresh()
  assert.equal(resource.detail.value.id, 'a')
  scope.stop()
})

test('pagination advances by server rows, deduplicates and refresh resets the first page', async () => {
  const calls = []
  const pages = [[{id: 3}, {id: 2}], [{id: 2}, {id: 1}], [], [{id: 4}, {id: 3}]]
  const scope = effectScope()
  const list = scope.run(() => usePredictionList(async (limit, offset) => { calls.push([limit, offset]); return pages.shift() }, {key: 'id', pageSize: 2}))
  await list.refresh(); await list.loadMore(); await list.loadMore()
  assert.deepEqual(list.rows.value.map(row => row.id), [3, 2, 1])
  assert.equal(list.hasMore.value, false)
  await list.refresh()
  assert.deepEqual(list.rows.value.map(row => row.id), [4, 3])
  assert.deepEqual(calls, [[2, 0], [2, 2], [2, 4], [2, 0]])
  scope.stop()
})

test('refresh wins over in-flight load more; a failed page remains retryable', async () => {
  const old = deferred()
  let calls = 0
  const scope = effectScope()
  const list = scope.run(() => usePredictionList(async () => {
    calls++
    if (calls === 2) return old.promise
    if (calls === 4) throw new Error('offline')
    return [{id: calls}]
  }, {key: 'id', pageSize: 1}))
  await list.refresh()
  const more = list.loadMore()
  await list.refresh()
  old.resolve([{id: 2}]); await more
  assert.deepEqual(list.rows.value.map(row => row.id), [3])
  await list.loadMore()
  assert.equal(list.error.value, 'offline')
  assert.equal(list.hasMore.value, true)
  await list.loadMore()
  assert.deepEqual(list.rows.value.map(row => row.id), [3, 5])
  scope.stop()
})

test('polling the first page preserves older rows and permits new overflow pages', async () => {
  const scope = effectScope()
  let page = [{id: 1}]
  const list = scope.run(() => usePredictionList(async () => page, {key: 'id', pageSize: 2}))
  await list.refresh()
  assert.equal(list.hasMore.value, false)
  page = [{id: 3}, {id: 2}]
  await list.refreshHead()
  assert.deepEqual(list.rows.value.map(row => row.id), [3, 2, 1])
  assert.equal(list.hasMore.value, true)
  scope.stop()
})

test('late cache results cannot replace a new stock after it has refreshed', async () => {
  const cacheA = deferred()
  const scope = effectScope()
  const refreshKeys = []
  const chart = scope.run(() => usePredictionChart(key => key === 'a' ? cacheA.promise : Promise.resolve({id: key}), async key => { refreshKeys.push(key); return {id: `${key}-new`} }))
  const first = chart.load('a')
  await chart.load('b')
  assert.equal(chart.chartData.value.id, 'b')
  assert.deepEqual(refreshKeys, [])
  await chart.refresh()
  cacheA.resolve({id: 'a'}); await first
  assert.equal(chart.chartData.value.id, 'b-new')
  assert.deepEqual(refreshKeys, ['b'])
  scope.stop()
})

test('chart identity change discards old cache and releases old refresh loading', async () => {
  const cacheA = deferred(), refreshA = deferred(), refreshB = deferred()
  const scope = effectScope()
  const chart = scope.run(() => usePredictionChart(key => key === 'a' ? cacheA.promise : Promise.resolve({id: key}), key => key === 'a' ? refreshA.promise : refreshB.promise))
  const first = chart.load('a')
  cacheA.resolve({id: 'a'}); await flush()
  await first
  const oldRefresh = chart.refresh(); await flush()
  assert.equal(chart.refreshing.value, true)
  const second = chart.load('b'); await second
  refreshA.resolve({id: 'a-refreshed'}); await oldRefresh
  assert.equal(chart.chartData.value.id, 'b')
  const newRefresh = chart.refresh(); await flush()
  refreshB.resolve({id: 'b-refreshed'}); await newRefresh
  assert.equal(chart.chartData.value.id, 'b-refreshed')
  assert.equal(chart.refreshing.value, false)
  scope.stop()
})

test('unmount invalidates delayed list and chart responses', async () => {
  const pending = deferred()
  let refreshCalls = 0
  const scope = effectScope()
  const list = scope.run(() => usePredictionList(() => pending.promise, {key: 'id'}))
  const chart = scope.run(() => usePredictionChart(() => pending.promise, async () => { refreshCalls++ }))
  const a = list.refresh(), b = chart.load('a')
  scope.stop(); pending.resolve([{id: 'late'}]); await Promise.all([a, b])
  assert.deepEqual(list.rows.value, [])
  assert.equal(chart.chartData.value, null)
  assert.equal(refreshCalls, 0)
  await chart.load('b')
  assert.equal(chart.initialLoading.value, false)
})
