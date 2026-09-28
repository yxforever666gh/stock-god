<script setup>
import {computed, ref, watch} from 'vue'
import {useMessage} from 'naive-ui'
import {GetPredictionRecommendationChart, RefreshPredictionRecommendationChart} from '../services/prediction-api'
import {formatMoney, formatPercent, formatPrice} from '../utils/number-format'
import {adaptPredictionChart, predictionChartOverlays} from '../charting/prediction-chart-adapter.js'
import {usePredictionChartPreferences} from '../composables/usePredictionChartPreferences'
import ChartDataMeta from './chart/ChartDataMeta.vue'
import MarketChartCanvas from './chart/MarketChartCanvas.vue'
import {usePredictionChart} from '../composables/usePredictionRequests.js'

const props = defineProps({
  recommendationId: {type: String, required: true},
  fallbackTrades: {type: Array, default: () => []},
})

const message = useMessage()
const {showPriceLines} = usePredictionChartPreferences()
const chartCanvas = ref(null)
const chartRequest = usePredictionChart(
  ({id}) => GetPredictionRecommendationChart(id),
  ({id}) => RefreshPredictionRecommendationChart(id),
)
const {chartData, initialLoading, refreshing, cacheError, refreshError} = chartRequest
const mode = ref('line')
const selectedSession = ref(null)

const model = computed(() => adaptPredictionChart(chartData.value || {}))
const fallbackTrades = computed(() => props.fallbackTrades.map(item => ({
  ...item,
  totalFees: item.totalFees ?? Number(item.commission || 0) + Number(item.stampDuty || 0) + Number(item.transferFee || 0),
})))
const trades = computed(() => chartData.value?.trades?.length ? chartData.value.trades : fallbackTrades.value)
const sessions = computed(() => chartData.value?.sessions || [])
const missingSessions = computed(() => {
  if (sessions.value.length) return sessions.value.filter(item => item.status === 'missing').map(item => item.date)
  return chartData.value?.missingSessions || []
})
const sessionOptions = computed(() => sessions.value.map(item => ({label: `${item.date}${item.status === 'missing' ? '（缺失）' : ''}`, value: item.date})))
const hasBuyTrade = computed(() => trades.value.some(item => String(item.side).toLowerCase() === 'buy'))
const isPartial = computed(() => chartData.value?.status === 'partial')
const isEmpty = computed(() => chartData.value?.status === 'empty' || (!initialLoading.value && model.value.bars.length === 0))
const currentYield = computed(() => Number(chartData.value?.currentNetYieldRate || 0))
const overlays = computed(() => predictionChartOverlays(model.value, trades.value, {showPriceLines: showPriceLines.value}))
const chartConfig = computed(() => ({
  viewMode: mode.value,
  mainIndicators: [],
  subIndicator: 'VOL',
  initialVisibleBars: 300,
}))

function resetRange() {
  selectedSession.value = null
  chartCanvas.value?.resetZoom()
}

function locateSession(date) {
  if (!date) return
  const found = chartCanvas.value?.zoomToTimeRange(`${date}T00:00:00`, `${date}T23:59:59`)
  if (!found) message.warning(`${date} 暂无分钟数据`)
}

const refreshChart = chartRequest.refresh
watch(() => props.recommendationId, id => {
  selectedSession.value = null
  void chartRequest.load({id})
}, {immediate: true})
</script>

<template>
  <section class="prediction-trade-chart">
    <n-flex justify="space-between" align="center" :wrap="true" class="chart-toolbar">
      <n-flex align="center" :wrap="true">
        <n-button-group>
          <n-button :type="mode === 'line' ? 'primary' : 'default'" @click="mode = 'line'">分时</n-button>
          <n-button :type="mode === 'candle' ? 'primary' : 'default'" @click="mode = 'candle'">1分钟K</n-button>
        </n-button-group>
        <n-select v-model:value="selectedSession" clearable placeholder="定位交易日" :options="sessionOptions" style="width:170px" @update:value="locateSession"/>
        <n-button @click="resetRange">范围复位</n-button>
        <n-flex align="center" :size="6" class="price-lines-toggle">
          <n-text depth="3">价格横线</n-text>
          <n-switch v-model:value="showPriceLines" aria-label="价格横线">
            <template #checked>显示</template>
            <template #unchecked>隐藏</template>
          </n-switch>
        </n-flex>
      </n-flex>
      <n-flex align="center" :wrap="true">
        <n-statistic label="最新价" :value="chartData?.currentPrice ? formatPrice(chartData.currentPrice) : '--'"/>
        <n-statistic label="预估净收益" :value="chartData && hasBuyTrade ? formatMoney(chartData.currentNetPnl) : '--'"/>
        <n-text :type="currentYield >= 0 ? 'error' : 'success'" strong>{{ chartData && hasBuyTrade ? formatPercent(currentYield) : '--' }}</n-text>
        <n-button type="primary" :loading="refreshing" :disabled="initialLoading" @click="refreshChart">刷新行情</n-button>
      </n-flex>
    </n-flex>

    <n-alert v-if="isPartial" type="warning" :bordered="false" class="chart-alert">
      分钟数据不完整，图中仅展示已取得的真实数据；缺失交易日：{{ missingSessions.join('、') || '部分时段' }}。
    </n-alert>
    <n-alert v-else-if="isEmpty && !initialLoading && !refreshing" type="info" :bordered="false" class="chart-alert">
      暂无可展示的分钟数据{{ missingSessions.length ? `；缺失交易日：${missingSessions.join('、')}` : '' }}。
    </n-alert>
    <n-alert v-if="refreshError || (cacheError && !chartData)" type="error" :bordered="false" class="chart-alert">
      {{ refreshError || cacheError }}。{{ chartData ? '已保留上次缓存图表。' : '' }}
    </n-alert>

    <ChartDataMeta :model="model" :loading="initialLoading || refreshing" :error="refreshError || cacheError" @refresh="refreshChart"/>
    <n-spin :show="initialLoading || (refreshing && !chartData)" description="正在读取分钟行情">
      <MarketChartCanvas
          v-show="model.bars.length"
          ref="chartCanvas"
          :model="model"
          :config="chartConfig"
          :overlays="overlays"
          :height="520"
      />
      <n-empty v-if="!model.bars.length && !initialLoading && !refreshing" description="暂无分钟走势" class="chart-empty"/>
    </n-spin>
    <n-flex justify="space-between" class="chart-footnote">
      <n-text depth="3">图表状态仅表示分钟数据覆盖情况；卖出检查与 AI 调用结果见下方时间线。十字光标保留量价来源及扣费净收益，真实成交标记保持不变。</n-text>
      <n-text depth="3">默认读取本地分钟缓存，点击“刷新行情”获取最新数据。行情时点 {{ String(chartData?.quoteAt || '--').replace('T', ' ').slice(0, 19) }} · 读取于 {{ String(chartData?.refreshedAt || '--').replace('T', ' ').slice(0, 19) }}</n-text>
    </n-flex>
  </section>
</template>

<style scoped>
.prediction-trade-chart {
  width: 100%;
}

.chart-toolbar {
  gap: 16px;
  margin-bottom: 10px;
}

.chart-toolbar :deep(.n-statistic) {
  min-width: 104px;
}

.chart-toolbar :deep(.n-statistic-value__content) {
  font-size: 18px;
}

.chart-alert {
  margin: 8px 0;
}

.chart-empty {
  height: 320px;
  justify-content: center;
}

.chart-footnote {
  gap: 12px;
  margin-top: 4px;
}
</style>
