import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import {compileScript, compileTemplate, parse} from '@vue/compiler-sfc'
import {createRenderer} from 'vue'

const moduleURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const uiStub = moduleURL("export const NButton='button', NTag='tag', NText='text'; export const useMessage=()=>({error(){},warning(){},success(){}})")
const childStub = moduleURL('export default {render(){return null}}')
const dragStub = moduleURL(`import {ref} from ${JSON.stringify(import.meta.resolve('vue'))}; export const useDraggableDataTableColumns=columns=>({tableRef:ref(null),columnsRef:ref(columns)})`)
const routerStub = moduleURL("export const useRoute=()=>({query:{},name:'prediction'}); export const useRouter=()=>({replace:async target=>{globalThis.__researchRouteReplacements?.push(target)}})")
const renderer = createRenderer({
  createComment: text => ({text}), insert() {}, remove() {}, parentNode: () => null,
  nextSibling: () => null, createElement: tag => ({tag}), createText: text => ({text}),
  setText() {}, setElementText() {}, patchProp() {},
})
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate) }

async function pageComponent(filename) {
  const url = new URL(filename, import.meta.url)
  const {descriptor} = parse(await readFile(url, 'utf8'), {filename})
  const compiled = compileScript(descriptor, {id: filename})
  const template = compileTemplate({source: descriptor.template.content, filename, id: filename, compilerOptions: {bindingMetadata: compiled.bindings}})
  assert.deepEqual(template.errors, [], `${filename} template compiles`)
  const script = compiled.content.replace(/import\s*\{([^}]+)\}\s*from\s*(['"])([^'"]+)\2/g, (whole, names, quote, specifier) => {
    if (!specifier.includes('/services/') || specifier.endsWith('.js')) return whole
    const exports = names.split(',').map(name => name.trim()).filter(Boolean).map(name => `export const ${name}=(...args)=>globalThis.__researchPageFixtures[${JSON.stringify(name)}](...args)`).join(';')
    return `import {${names}} from ${JSON.stringify(moduleURL(exports))}`
  }).replace(/from (['"])([^'"]+)\1/g, (_, quote, specifier) => {
    let resolved = specifier
    if (specifier === 'naive-ui') resolved = uiStub
    else if (specifier === 'vue-router') resolved = routerStub
    else if (specifier.endsWith('.vue')) resolved = childStub
    else if (specifier.includes('useDraggableDataTableColumns')) resolved = dragStub
    else if (specifier.startsWith('.')) resolved = new URL(/\.(?:js|mjs)$/.test(specifier) ? specifier : `${specifier}.js`, url).href
    else if (!specifier.startsWith('data:')) resolved = import.meta.resolve(specifier)
    return `from ${JSON.stringify(resolved)}`
  })
  const {default: component} = await import(moduleURL(script))
  return {...component, render: () => null}
}

test('prediction account switch isolates list requests and ignores an unmounted account response', async () => {
  const pending = new Map()
  globalThis.__researchPageFixtures = {
    GetPredictionAccount: async () => ({}),
    ListPredictionRecommendations: (limit, offset, slot) => new Promise(resolve => pending.set(slot, resolve)),
  }
  const component = await pageComponent('predictionRecommendations.vue')
  const first = renderer.createApp(component, {slot: '09:30'})
  first.mount({})
  await flush()
  first.unmount()
  const second = renderer.createApp(component, {slot: '09:35'})
  const vm = second.mount({})
  try {
    await flush()
    assert.deepEqual([...pending.keys()], ['09:30', '09:35'])
    pending.get('09:35')([{recommendationId: 'new-account'}])
    await flush()
    pending.get('09:30')([{recommendationId: 'old-account'}])
    await flush()
    assert.deepEqual(vm.$.setupState.rows.map(row => row.recommendationId), ['new-account'])
  } finally {
    second.unmount()
    delete globalThis.__researchPageFixtures
  }
})

test('prediction uses server ranks and execution states without selection roles or score gates', async () => {
  const rows = [
    {recommendationId: 'first', selectionRank: 1, displaySelectionRank: 1, finalScore: 51, status: 'buy_pending'},
    {recommendationId: 'second', selectionRank: 2, displaySelectionRank: 2, finalScore: 50, status: 'buy_pending'},
    {recommendationId: 'promoted', selectionRole: 'observation', selectionRank: 4, displaySelectionRank: 3, finalScore: 30, status: 'active', buyAt: '2026-09-15T09:52:00+08:00', buyPrice: 10, quantity: 100},
    {recommendationId: 'pending', selectionRole: 'primary', selectionRank: 1, displaySelectionRole: 'primary', displaySelectionRank: 3, finalScore: 57, status: 'buy_pending'},
    {recommendationId: 'standby', selectionRole: 'observation', status: 'analysis_only', selectionRank: 5, displaySelectionRole: 'candidate', displaySelectionRank: 3},
    {recommendationId: 'failed', selectionRole: 'primary', selectionRank: 1, displaySelectionRole: '', displaySelectionRank: 0},
  ]
  globalThis.__researchPageFixtures = {GetPredictionAccount: async () => ({}), ListPredictionRecommendations: async () => rows}
  const app = renderer.createApp(await pageComponent('predictionRecommendations.vue'))
  const vm = app.mount({})
  try {
    await flush()
    const state = vm.$.setupState
    const rank = state.columnsRef.find(column => column.key === 'displaySelectionRank')
    assert.equal(rank.title, '排名')
    assert.equal(state.columnsRef.some(column => column.key === 'selectionRole'), false)
    assert.deepEqual(state.rows.map(rank.render), ['1', '2', '3', '3', '3', '--'])
    assert.equal(state.statusLabel(rows[0]), '待买入')
    assert.equal(state.statusLabel(rows[1]), '待买入')
    assert.equal(state.statusLabel(rows[2]), '持仓中')
    assert.equal(state.statusLabel(rows[3]), '待买入')
    assert.equal(state.statusLabel(rows[4]), '仅分析')
    assert.equal(state.columnsRef.find(column => column.key === 'quantity').render(rows[4]), '--')
    assert.equal(state.columnsRef.find(column => column.key === 'netYieldRate').render(rows[4]), '--')
    assert.equal(state.hasScoreExplanation({reportMarkdown: '# 历史报告'}), false)
    assert.equal(state.hasScoreExplanation({reportMarkdown: '# 报告\n\n## 分项评分依据\n市场18分'}), true)
    const source = await readFile(new URL('predictionRecommendations.vue', import.meta.url), 'utf8')
    assert.match(source, /报告排名/)
    assert.doesNotMatch(source, /主选|候选|selectionRole|displaySelectionRole/)
    assert.match(source, /两席等分盘前现金/)
    assert.match(source, /失败席位保留现金，不补排、不重分/)
    assert.match(source, /历史未记录逐项评分说明/)
    for (const field of ['marketScore', 'sectorScore', 'stockScore', 'catalystScore', 'riskDeduction']) assert.ok(source.includes(`detail.recommendation.${field}`))
  } finally {
    app.unmount()
    delete globalThis.__researchPageFixtures
  }
})

test('prediction uses a four-column account panel and calendar for report dates', async () => {
  const source = await readFile(new URL('predictionIndex.vue', import.meta.url), 'utf8')
  assert.match(source, /<n-popover v-else v-model:show="slotPickerOpen"/)
  assert.match(source, /slotOptions/)
  assert.match(source, /repeat\(4, minmax\(0, 1fr\)\)/)
  assert.match(source, /repeat\(2, minmax\(0, 1fr\)\)/)
  assert.match(source, /width: min\(720px, calc\(100vw - 48px\)\)/)
  assert.match(source, /BASE43/)
  assert.match(source, /predictionSlotBuyLabel/)
  assert.doesNotMatch(source, /predictionSlotReportLabel|reportTagType|持仓：/)
  assert.doesNotMatch(source, /已完成|尚未完成/)
  assert.match(source, /选择现役或归档账户/)
  assert.match(source, /<n-date-picker/)
  assert.match(source, /日历选择交易日/)
  assert.match(source, /最近5个交易日/)
  assert.match(source, /全部交易日/)
  assert.doesNotMatch(source, /reportDayOptions|<n-dropdown/)
  assert.doesNotMatch(source, /<n-tab v-for="slot in PREDICTION_SLOTS"/)
})

test('account panel closes on selection and report calendar permits only recorded dates', async () => {
  globalThis.__researchPageFixtures = {ListPredictionSlots: async () => []}
  globalThis.__researchRouteReplacements = []
  const app = renderer.createApp(await pageComponent('predictionIndex.vue'))
  const vm = app.mount({})
  try {
    const state = vm.$.setupState
    assert.equal(state.selectedSlot, 'base43')
    assert.equal(state.slotOptions.length, 25)
    state.slotPickerOpen = true
    state.updateSlot('10:05')
    assert.equal(state.slotPickerOpen, false)
    assert.equal(state.selectedSlot, '10:05')
    assert.equal(globalThis.__researchRouteReplacements.at(-1).query.slot, '10:05')
    assert.equal(state.reportDay, 'recent5')
    state.updateReportDates(['2026-09-29', '2026-09-28'])
    state.updateReportDay('2026-09-29')
    assert.equal(state.reportDateValue, '2026-09-29')
    assert.equal(state.reportDateDisabled(new Date(2026, 8, 29).getTime()), false)
    assert.equal(state.reportDateDisabled(new Date(2026, 8, 27).getTime()), true)
    assert.equal(globalThis.__researchRouteReplacements.at(-1).query.reportDay, '2026-09-29')
    state.updateReportDay('recent5')
    assert.equal(state.reportDateValue, null)
    state.updateReportDay('all')
    assert.equal(state.reportDay, 'all')
    assert.equal(state.validReportDay('2026-02-30'), false)
  } finally {
    app.unmount()
    delete globalThis.__researchPageFixtures
    delete globalThis.__researchRouteReplacements
  }
})

test('prediction report explains the one-report daily limit and does not offer another analysis', async () => {
  globalThis.__researchPageFixtures = {BrowsePredictionRuns: async () => ({items: [], total: 0, tradingDates: []}), GetPredictionRun: async () => ({})}
  const app = renderer.createApp(await pageComponent('predictionReport.vue'))
  const vm = app.mount({})
  try {
    await flush()
    const state = vm.$.setupState
    assert.equal(state.columns.some(column => column.key === 'selectionCounts'), false)
    assert.equal(state.columns.find(column => column.key === 'attemptNo').title, '尝试')
    for (const status of ['failed', 'success', 'no_recommendation']) {
      const action = state.columns.find(column => column.key === 'action').render({status, runId: status})
      assert.equal(action.children, '查看')
    }
    const source = await readFile(new URL('predictionReport.vue', import.meta.url), 'utf8')
    assert.match(source, /BASE43 使用原 43 特征与最近五模型均值/)
    assert.match(source, /盘前输入截至 09:29:59/)
    assert.equal(state.columns.find(column => column.key === 'slot').title, '所属区间')
    assert.match(source, /竞价 API 未配置时不会生成选股报告/)
    assert.doesNotMatch(source, /主选|候选|补位|主备|主\/备|09:55/)
    assert.match(source, /}, 5000,/)
    assert.match(source, /<n-pagination/)
    assert.doesNotMatch(source, /PredictionHistoryFooter|加载更多/)
  } finally {
    app.unmount()
    delete globalThis.__researchPageFixtures
  }
})

test('prediction yield lists only bought rows and exposes one mutually exclusive limit outcome', async () => {
  const rows = [{recommendationId: 'bought', status: 'active', buyAt: '2026-09-15T09:52:00+08:00', buyPrice: 10, quantity: 100, netPnl: 8, netYieldRate: 0.008, buyDayLimitStatus: 'complete', buyDayLimitOutcome: 'sealed'}]
  globalThis.__researchPageFixtures = {
    GetPredictionPerformance: async () => ({initialContribution: 10000, topUpContribution: 20000, cumulativeExternalCapital: 30000, netAssetValue: 31545.07, cash: 280.99, netProfit: 1545.07, totalFees: 572.96, maxDrawdown: 0.0425}),
    GetPredictionPortfolioPerformance: async () => ({slots: ['09:50'], curve: [], periodReturn: 0.0919, tradingDayCount: 12, selectedAccountCount: 1, effectiveAccountCount: 1, boughtTrades: 50, closedTrades: 45, winningTrades: 26, winRate: 0.5778, sealed: {count: 21, rate: 0.42}, broken: {count: 5, rate: 0.1}, untouched: {count: 24, rate: 0.48}, pendingOutcomeCount: 0, incompleteAccountCount: 0, noActivityAccountCount: 0}),
    ListPredictionPerformanceRecommendations: async () => rows,
  }
  const app = renderer.createApp(await pageComponent('predictionYield.vue'))
  const vm = app.mount({})
  try {
    await flush()
    const state = vm.$.setupState
    assert.deepEqual(state.rows.map(row => row.recommendationId), ['bought'])
    assert.equal(state.columns.find(column => column.key === 'quantity').render(rows[0]), '100')
    assert.equal(state.outcomeText(rows[0]), '封板')
    assert.deepEqual(state.accountCards.map(item => item[0]), ['账户区间', '初始本金', '追加本金', '累计投入本金', '账户净值', '可用现金', '累计净收益', '总费用', '交易事件最大回撤'])
    assert.equal(state.accountCards.find(item => item[0] === '总费用')[1], '-¥572.96')
    assert.deepEqual(state.periodCards.map(item => item[0]), ['区间收益率', '交易日天数', '实际买入', '已平仓', '胜率', '封板', '炸板', '未触板', '待统计', '数据不完整账户', '无活动账户', '有效账户'])
    assert.equal(state.columns.some(column => ['hitFiveBeforeSell', 'hitLimitUpFullDay', 'hitMinusThree'].includes(column.key)), false)
    const source = await readFile(new URL('predictionYield.vue', import.meta.url), 'utf8')
    assert.match(source, /累计投入本金/)
    assert.match(source, /交易事件最大回撤/)
    assert.match(source, /独立账户/)
    assert.match(source, /集成账户/)
    assert.match(source, /买入日触板结果/)
    assert.match(source, /:cols="3"/)
    assert.match(source, /mode.value === 'individual' \? \[\['交易日天数'/)
    assert.match(source, /performanceSlots/)
    assert.doesNotMatch(source, /multiple filterable clearable/)
    assert.equal(state.effectiveSlots[0], 'base43')
  } finally {
    app.unmount()
    delete globalThis.__researchPageFixtures
  }
})

for (const filename of ['predictionRecommendations.vue', 'predictionYield.vue']) {
  test(`${filename}: all 201 historical rows remain reachable and details reject late responses`, async () => {
    const rows = Array.from({length: 201}, (_, index) => ({recommendationId: `r${index}`, runId: `r${index}`, activatedAt: '2026-09-07', status: 'closed'}))
    const details = new Map()
    globalThis.__researchPageFixtures = new Proxy({}, {get: (_, name) => {
      if (String(name).startsWith('List') && !String(name).includes('CashFlows')) return async (limit, offset) => rows.slice(offset, offset + limit)
      if (String(name).includes('Recommendation') || name === 'GetAIAnalysisReport' || name === 'GetPredictionRun') return id => new Promise(resolve => details.set(id, resolve))
      return async () => ({})
    }})
    const app = renderer.createApp(await pageComponent(filename))
    const vm = app.mount({})
    try {
      const state = vm.$.setupState
      await flush()
      while (state.history.hasMore.value) await state.history.loadMore()
      assert.equal(state.rows.length, 201)
      const show = state.showDetail || state.show
      const a = show({recommendationId: 'a', runId: 'a'}), b = show({recommendationId: 'b', runId: 'b'})
      details.get('b')({id: 'b'}); await b
      details.get('a')({id: 'a'}); await a
      assert.equal(state.detail.id, 'b')
      await state.history.refresh()
      assert.equal(state.rows.length, 200)
    } finally {
      app.unmount()
      delete globalThis.__researchPageFixtures
    }
  })
}

test('prediction refreshes a pending detail when its list reaches a terminal state first', async () => {
  let finishOldDetail, detailReads = 0, listStatus = 'running'
  globalThis.__researchPageFixtures = {
    BrowsePredictionRuns: async () => ({items: [{runId: 'r', status: listStatus}], total: 1, tradingDates: ['2026-09-29']}),
    GetPredictionRun: async () => {
      detailReads++
      if (detailReads === 1) return await new Promise(resolve => { finishOldDetail = resolve })
      return {runId: 'r', status: 'success'}
    },
  }
  const app = renderer.createApp(await pageComponent('predictionReport.vue'))
  const vm = app.mount({})
  try {
    const state = vm.$.setupState
    await flush()
    const original = state.show({runId: 'r'})
    listStatus = 'success'
    await state.polling.run()
    assert.equal(state.rows[0].status, 'success')
    finishOldDetail({runId: 'r', status: 'running'})
    await original
    assert.equal(state.detail.status, 'success')
    assert.equal(state.detailLoading, false)
    assert.equal(detailReads, 2)
  } finally {
    app.unmount()
    delete globalThis.__researchPageFixtures
  }
})

test('report pages 100 rows and keeps a selected trading day on later pages', async () => {
  const calls = []
  const runs = Array.from({length: 101}, (_, index) => ({runId: `run-${index}`, status: 'success'}))
  globalThis.__researchPageFixtures = {
    BrowsePredictionRuns: async (page, day) => {
      calls.push([page, day])
      return {items: runs.slice((page - 1) * 100, page * 100), total: 101, page, pageSize: 100, tradingDates: ['2026-09-29']}
    },
    GetPredictionRun: async () => ({}),
  }
  const app = renderer.createApp(await pageComponent('predictionReport.vue'), {reportDay: '2026-09-29'})
  const vm = app.mount({})
  try {
    await flush()
    const state = vm.$.setupState
    assert.equal(state.rows.length, 100)
    assert.equal(state.pageCount, 2)
    await state.loadPage(2)
    assert.equal(state.rows.length, 1)
    assert.deepEqual(calls, [[1, '2026-09-29'], [2, '2026-09-29']])
  } finally {
    app.unmount()
    delete globalThis.__researchPageFixtures
  }
})

for (const filename of ['predictionRecommendations.vue']) {
  test(`${filename}: Shanghai date separators span loaded pages and tolerate missing dates`, async () => {
    const rows = Array.from({length: 200}, (_, index) => ({recommendationId: `r${index}`, signalAt: '2026-09-10T00:05:00+08:00'}))
    rows.push(
      {recommendationId: 'older', signalAt: '2026-09-09T15:59:00Z'},
      {recommendationId: 'same', signalAt: '2026-09-09 23:58:00'},
      {recommendationId: 'missing'},
      {recommendationId: 'after-missing', signalAt: '2026-09-08'},
      {recommendationId: 'invalid', signalAt: 'invalid'},
    )
    globalThis.__researchPageFixtures = new Proxy({}, {get: (_, name) => String(name).startsWith('List') ? async (limit, offset) => rows.slice(offset, offset + limit) : async () => ({})})
    const app = renderer.createApp(await pageComponent(filename))
    const vm = app.mount({})
    try {
      await flush()
      const state = vm.$.setupState
      assert.equal(state.rows.length, 200)
      assert.equal(state.recommendationRowClass(state.rows[0], 0), '')
      assert.equal(state.recommendationRowClass(state.rows[199], 199), '')
      assert.equal(state.signalDate('2026-09-09T16:05:00Z'), state.signalDate(state.rows[0].signalAt))
      await state.history.loadMore()
      assert.equal(state.recommendationRowClass(state.rows[200], 200), 'recommendation-date-start')
      assert.equal(state.recommendationRowClass(state.rows[201], 201), '')
      for (const index of [202, 203, 204]) assert.equal(state.recommendationRowClass(state.rows[index], index), '')
      const source = await readFile(new URL(filename, import.meta.url), 'utf8')
      assert.equal((source.match(/:row-class-name="recommendationRowClass"/g) || []).length, 1)
      assert.match(source, /:deep\(\.recommendation-date-start > td\)\s*\{\s*border-top: 2px solid #000;/)
      rows.length = 0
      await state.history.refresh()
      assert.deepEqual(state.rows, [])
    } finally {
      app.unmount()
      delete globalThis.__researchPageFixtures
    }
  })
}
