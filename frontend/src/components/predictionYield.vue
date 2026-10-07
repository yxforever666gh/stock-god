<script setup>
import {computed, h, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import {useRoute, useRouter} from 'vue-router'
import {NButton, NTag, NText, useMessage} from 'naive-ui'
import {GetPredictionPerformance, GetPredictionPortfolioPerformance, GetPredictionRecommendation, ListPredictionPerformanceRecommendations} from '../services/prediction-api'
import {formatDrawdown, formatInteger, formatMoney, formatNumber, formatPercent, formatPrice} from '../utils/number-format'
import {PREDICTION_SLOTS, validPredictionSlot} from '../utils/prediction-slots.js'
import AppMarkdownPreview from './AppMarkdownPreview.vue'
import PredictionTradeChart from './PredictionTradeChart.vue'
import PredictionHistoryFooter from './PredictionHistoryFooter.vue'
import PredictionPortfolioReturnChart from './PredictionPortfolioReturnChart.vue'
import {usePredictionDetail, usePredictionList} from '../composables/usePredictionRequests.js'

const props = defineProps({slot: {type: String, default: 'base43'}})
const route = useRoute(), router = useRouter(), message = useMessage()
const mode = ref('individual')
const querySlots = String(route.query.performanceSlots || '').split(',').filter(validPredictionSlot)
const selectedSlots = ref(querySlots.length ? [...new Set(querySlots)] : PREDICTION_SLOTS.map(item => item.value))
const fromDate = ref(/^\d{4}-\d{2}-\d{2}$/.test(String(route.query.performanceFrom || '')) ? String(route.query.performanceFrom) : null)
const toDate = ref(/^\d{4}-\d{2}-\d{2}$/.test(String(route.query.performanceTo || '')) ? String(route.query.performanceTo) : null)
const slotOptions = PREDICTION_SLOTS
const effectiveSlots = computed(() => mode.value === 'integrated' ? selectedSlots.value : [props.slot])
const loading = ref(false), performance = ref(null), portfolio = ref(null)
let active = true
let requestVersion = 0

const history = usePredictionList(async (limit, offset) => await ListPredictionPerformanceRecommendations(limit, offset, effectiveSlots.value, fromDate.value || '', toDate.value || '') || [])
const {rows, loading: listLoading, error: listError, hasMore} = history
const detailRequest = usePredictionDetail(GetPredictionRecommendation)
const {detail, visible, loading: detailLoading, error: detailError} = detailRequest
onBeforeUnmount(() => { active = false; requestVersion++ })

const rate = value => value === null || value === undefined ? '--' : formatPercent(value)
const ratio = value => rate(value).replace(/^\+/, '')
const fee = value => value === null || value === undefined ? '--' : formatMoney(-Math.abs(Number(value)))
const drawdownRate = value => value === null || value === undefined ? '--' : formatDrawdown(value)
const colorType = value => Number(value || 0) >= 0 ? 'error' : 'success'
const yieldClass = value => value === null || value === undefined ? '' : Number(value) >= 0 ? 'yield-positive' : 'yield-negative'
const dateTime = value => value ? String(value).slice(0, 19).replace('T', ' ') : '--'
const executionModeLabels = {live_after_signal: '信号后实时成交', recovered_target_minute: '历史目标分钟价', scheduled_slot_sell: '分区定时卖出', recovered_slot_sell: '重启恢复卖出'}
const executionMode = trade => executionModeLabels[trade?.executionMode] || trade?.executionMode || '--'
const degradedReason = analysis => analysis?.degraded === null || analysis?.degraded === undefined ? '历史运行未记录证据质量' : analysis.degraded ? '辅助证据不完整，具体来源状态请查看证据审计' : '无'
const hasBuy = row => Boolean(row.buyAt) && Number(row.buyPrice || 0) > 0
const statusLabels = {active: '持仓中', sell_pending: '待卖出', closed: '已平仓'}
const outcomeLabels = {sealed: '封板', broken: '炸板', untouched: '未触板'}
const outcomeType = {sealed: 'error', broken: 'warning', untouched: 'default'}
const outcomeText = row => row.buyDayLimitStatus === 'complete' ? outcomeLabels[row.buyDayLimitOutcome] || '待统计' : row.buyDayLimitStatus === 'unavailable' ? '数据不足' : '待统计'
const outcomeCard = metric => `${formatInteger(metric?.count)} 次 / ${ratio(metric?.rate)}`
const assessment = computed(() => !portfolio.value?.closedTrades ? '暂无已平仓样本' : portfolio.value.closedTrades < 30 ? '样本不足，仅供观察' : '已有阶段性样本')

const columns = computed(() => [
  ...(mode.value === 'integrated' ? [{title: '账户区间', key: 'slot', width: 100}] : []),
  {title: '股票', key: 'stockCode', minWidth: 170, render: row => h(NButton, {text: true, type: 'primary', onClick: () => show(row)}, {default: () => `${row.stockName}（${row.stockCode}）`})},
  {title: '实际买入', key: 'buyAt', width: 165, render: row => dateTime(row.buyAt)},
  {title: '数量', key: 'quantity', width: 90, render: row => formatInteger(row.quantity)},
  {title: '净收益', key: 'netPnl', width: 120, render: row => h(NText, {type: colorType(row.netPnl), strong: true, class: 'yield-table-value'}, {default: () => formatMoney(row.netPnl)})},
  {title: '净收益率', key: 'netYieldRate', width: 110, render: row => h(NText, {type: colorType(row.netYieldRate), strong: true, class: 'yield-table-value'}, {default: () => rate(row.netYieldRate)})},
  {title: '买入日触板结果', key: 'buyDayLimitOutcome', width: 130, render: row => h(NTag, {size: 'small', type: outcomeType[row.buyDayLimitOutcome] || 'default', bordered: false}, {default: () => outcomeText(row)})},
  {title: '状态', key: 'status', width: 90, render: row => statusLabels[row.status] || row.status},
])

const accountCards = computed(() => mode.value !== 'individual' ? [] : [
  ['账户区间', props.slot], ['初始本金', formatMoney(performance.value?.initialContribution)], ['追加本金', formatMoney(performance.value?.topUpContribution)],
  ['累计投入本金', formatMoney(performance.value?.cumulativeExternalCapital)], ['账户净值', formatMoney(performance.value?.netAssetValue)], ['可用现金', formatMoney(performance.value?.cash)],
  ['累计净收益', formatMoney(performance.value?.netProfit), yieldClass(performance.value?.netProfit)], ['总费用', fee(performance.value?.totalFees), performance.value?.totalFees === null || performance.value?.totalFees === undefined ? '' : 'yield-negative'], ['交易事件最大回撤', drawdownRate(performance.value?.maxDrawdown), performance.value?.maxDrawdown === null || performance.value?.maxDrawdown === undefined ? '' : 'yield-negative'],
])
const periodCards = computed(() => [
  ['区间收益率', rate(portfolio.value?.periodReturn), yieldClass(portfolio.value?.periodReturn)],
  ...(mode.value === 'individual' ? [['交易日天数', portfolio.value?.tradingDayCount === null || portfolio.value?.tradingDayCount === undefined ? '--' : `${formatInteger(portfolio.value.tradingDayCount)} 天`]] : []),
  ['实际买入', `${formatInteger(portfolio.value?.boughtTrades)} 笔`], ['已平仓', `${formatInteger(portfolio.value?.closedTrades)} 笔`], ['胜率', ratio(portfolio.value?.winRate)],
  ['封板', outcomeCard(portfolio.value?.sealed)], ['炸板', outcomeCard(portfolio.value?.broken)], ['未触板', outcomeCard(portfolio.value?.untouched)], ['待统计', `${formatInteger(portfolio.value?.pendingOutcomeCount)} 笔`],
  ['数据不完整账户', `${formatInteger(portfolio.value?.incompleteAccountCount)} 个`], ['无活动账户', `${formatInteger(portfolio.value?.noActivityAccountCount)} 个`], ['有效账户', `${formatInteger(portfolio.value?.effectiveAccountCount)} / ${formatInteger(portfolio.value?.selectedAccountCount)}`],
])

function show(row) { detailRequest.show(row.recommendationId) }
function selectAllSlots() { selectedSlots.value = PREDICTION_SLOTS.map(item => item.value) }
function syncURL() {
  router.replace({name: 'prediction', query: {
    ...route.query,
    performanceMode: mode.value,
    performanceSlots: mode.value === 'integrated' ? selectedSlots.value.join(',') : undefined,
    performanceFrom: fromDate.value || undefined,
    performanceTo: toDate.value || undefined,
  }})
}
async function refresh() {
  if (!active) return
  if (!effectiveSlots.value.length) { message.warning('请至少选择一个账户'); return }
  if (fromDate.value && toDate.value && fromDate.value > toDate.value) { message.error('开始日期不能晚于结束日期'); return }
  const version = ++requestVersion
  loading.value = true
  try {
    const requests = [GetPredictionPortfolioPerformance(effectiveSlots.value, fromDate.value || '', toDate.value || ''), history.refresh()]
    if (mode.value === 'individual') requests.push(GetPredictionPerformance(props.slot))
    const [portfolioResult,, accountResult] = await Promise.all(requests)
    if (!active || version !== requestVersion) return
    portfolio.value = portfolioResult
    performance.value = mode.value === 'individual' ? accountResult : null
  } catch (error) {
    if (active && version === requestVersion) message.error(error?.message || String(error))
  } finally {
    if (active && version === requestVersion) loading.value = false
  }
}

watch([mode, selectedSlots, fromDate, toDate], () => { syncURL(); void refresh() }, {deep: true})
watch(() => props.slot, () => { if (mode.value === 'individual') void refresh() })
onMounted(() => { syncURL(); void refresh() })
</script>

<template>
  <n-space vertical size="large">
    <n-flex align="center" :wrap="true" class="performance-filter-bar">

      <n-date-picker v-model:formatted-value="fromDate" type="date" value-format="yyyy-MM-dd" clearable placeholder="开始日期" :is-date-disabled="ts => ts > Date.now()"/>
      <span>至</span>
      <n-date-picker v-model:formatted-value="toDate" type="date" value-format="yyyy-MM-dd" clearable placeholder="结束日期" :is-date-disabled="ts => ts > Date.now()"/>
      <n-button :loading="loading" @click="refresh">刷新</n-button>
    </n-flex>

    <template v-if="mode === 'individual'">
      <n-text strong>当前独立账户</n-text>
      <n-grid :cols="3" :x-gap="12" :y-gap="12" responsive="screen">
        <n-gi v-for="item in accountCards" :key="item[0]"><n-card size="small"><n-statistic :label="item[0]" :value="item[1]" :class="item[2]"/></n-card></n-gi>
      </n-grid>
    </template>

    <n-text strong>{{mode === 'integrated' ? '集成账户区间统计' : '当前账户区间统计'}}</n-text>
    <n-grid :cols="4" :x-gap="12" :y-gap="12" responsive="screen">
      <n-gi v-for="item in periodCards" :key="item[0]"><n-card size="small"><n-statistic :label="item[0]" :value="item[1]" :class="item[2]"/></n-card></n-gi>
    </n-grid>
    <n-alert type="info" :bordered="false">
      {{assessment}}。区间收益率（TWR）按最新完整交易日收盘更新，中和外部加资与历史内部划拨；集成账户对有真实持仓或交易暴露且估值完整的账户等权平均。胜率只统计所选实际买入日范围内的已平仓股票；封板、炸板、未触板按实际买入后的完整分钟至当日收盘互斥统计，数据不足与尚未收盘的股票单列为待统计。
    </n-alert>

    <n-card title="收益率日变化" size="small"><PredictionPortfolioReturnChart :points="portfolio?.curve || []"/></n-card>
    <n-flex justify="space-between" align="center"><n-text depth="3">收益页仅列出实际买入股票；未买入推荐仍可在“股票推荐记录”查看。点击股票可查看持仓期分钟走势。</n-text></n-flex>
    <n-data-table :columns="columns" :data="rows" :loading="loading" :scroll-x="mode === 'integrated' ? 1180 : 1080" :row-key="row => row.recommendationId"/>
    <PredictionHistoryFooter :count="rows.length" :has-more="hasMore" :loading="listLoading" :error="listError" @load-more="history.loadMore"/>
  </n-space>

  <n-modal v-model:show="visible">
    <n-card class="research-detail-card" title="收益与成交详情" closable @close="visible=false">
      <n-scrollbar style="max-height:87vh">
        <n-alert v-if="detailError" type="error">{{ detailError }} <n-button text @click="detailRequest.refresh">重试</n-button></n-alert>
        <n-spin :show="detailLoading">
          <template v-if="detail">
            <n-descriptions bordered :column="3">
              <n-descriptions-item label="股票">{{detail.recommendation.stockName}}（{{detail.recommendation.stockCode}}）</n-descriptions-item>
              <n-descriptions-item label="净收益"><n-text v-if="hasBuy(detail.recommendation)" class="yield-table-value" :type="colorType(detail.recommendation.netPnl)" strong>{{formatMoney(detail.recommendation.netPnl)}}</n-text><span v-else>--</span></n-descriptions-item>
              <n-descriptions-item label="净收益率"><n-text v-if="hasBuy(detail.recommendation)" class="yield-table-value" :type="colorType(detail.recommendation.netYieldRate)" strong>{{rate(detail.recommendation.netYieldRate)}}</n-text><span v-else>--</span></n-descriptions-item>
              <n-descriptions-item label="买入日触板结果">{{outcomeText(detail.recommendation)}}</n-descriptions-item>
              <n-descriptions-item :label="detail.recommendation.slot === 'base43' ? '预测收益分数' : '最终分'">{{detail.recommendation.slot === 'base43' ? `${formatNumber(detail.recommendation.finalScore, 4)}%` : formatNumber(detail.recommendation.finalScore, 1)}}</n-descriptions-item>
              <n-descriptions-item label="状态">{{statusLabels[detail.recommendation.status] || detail.recommendation.status}}</n-descriptions-item>
              <n-descriptions-item label="信号时间">{{dateTime(detail.recommendation.signalAt)}}</n-descriptions-item>
              <n-descriptions-item label="启动时间">{{dateTime(detail.analysis.startedAt)}}</n-descriptions-item>
              <n-descriptions-item label="报告产生时间">{{dateTime(detail.analysis.generatedAt)}}</n-descriptions-item>
              <n-descriptions-item label="目标 / 实际买入">{{dateTime(detail.recommendation.targetBuyAt)}} / {{dateTime(detail.recommendation.buyAt)}}</n-descriptions-item>
              <n-descriptions-item label="目标 / 实际卖出">{{dateTime(detail.recommendation.targetSellAt)}} / {{dateTime(detail.recommendation.sellAt)}}</n-descriptions-item>
              <n-descriptions-item label="证据降级" :span="3">{{degradedReason(detail.analysis)}}</n-descriptions-item>
            </n-descriptions>
            <n-divider title-placement="left">持仓期分钟走势</n-divider>
            <PredictionTradeChart :recommendation-id="detail.recommendation.recommendationId" :fallback-trades="detail.trades || []"/>
            <n-divider title-placement="left">完整报告</n-divider>
            <AppMarkdownPreview :model-value="detail.analysis.reportMarkdown || '暂无报告'"/>
            <n-divider title-placement="left">成交记录</n-divider>
            <n-data-table :data="detail.trades || []" :columns="[
              {title:'方向',key:'side'}, {title:'时间',key:'tradedAt',render:r=>dateTime(r.tradedAt)}, {title:'成交价',key:'executionPrice',render:r=>formatPrice(r.executionPrice)},
              {title:'价格来源',key:'priceSource',render:r=>`${r.priceSource || '--'}${r.priceStale ? '（历史价格）' : ''}`}, {title:'执行模式',key:'executionMode',render:r=>executionMode(r)},
              {title:'数量',key:'quantity',render:r=>formatInteger(r.quantity)}, {title:'净现金流',key:'netCashFlow',render:r=>formatMoney(r.netCashFlow)}
            ]"/>
          </template>
        </n-spin>
      </n-scrollbar>
    </n-card>
  </n-modal>
</template>

<style scoped>
.research-detail-card { width: min(1600px, 96vw); max-height: 96vh; }
.performance-filter-bar { gap: 10px; }
.slot-multi-select { width: min(620px, 72vw); }
.yield-positive :deep(.n-statistic-value__content) { color: #d03050; font-size: 24px; font-weight: 700; }
.yield-negative :deep(.n-statistic-value__content) { color: #18a058; font-size: 24px; font-weight: 700; }
.yield-table-value { font-size: 16px; }
</style>
