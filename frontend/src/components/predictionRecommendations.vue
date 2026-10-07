<script setup>
import {h, onMounted} from 'vue'
import {NButton, NTag, NText} from 'naive-ui'
import {GetPredictionAccount, GetPredictionRecommendation, ListPredictionRecommendations} from '../services/prediction-api'
import {useDraggableDataTableColumns} from '../composables/useDraggableDataTableColumns'
import AppMarkdownPreview from './AppMarkdownPreview.vue'
import PredictionTradeChart from './PredictionTradeChart.vue'
import PredictionHistoryFooter from './PredictionHistoryFooter.vue'
import {usePredictionDetail, usePredictionList} from '../composables/usePredictionRequests.js'
import {formatInteger, formatMoney, formatNumber, formatPercent, formatPrice} from '../utils/number-format'

const props = defineProps({slot: {type: String, default: 'base43'}})

const history = usePredictionList(async (limit, offset) => {
  if (offset === 0) await GetPredictionAccount(props.slot)
  return await ListPredictionRecommendations(limit, offset, props.slot) || []
})
const {rows, loading, error: listError, hasMore} = history
const detailRequest = usePredictionDetail(GetPredictionRecommendation)
const {detail, visible, loading: detailLoading, error: detailError} = detailRequest

const dateTime = value => value ? String(value).slice(0, 19).replace('T', ' ') : '--'
const statusLabels = {buy_pending: '待买入', standby: '待买入', standby_not_used: '未买入', active: '持仓中', sell_pending: '待卖出', closed: '已平仓', analysis_only: '仅分析', missed_cash: '资金不足', missed_untradable: '不可成交', missed_window: '错过窗口', cancelled_price: '价格取消'}
const statusType = status => status === 'closed' ? 'success' : status === 'analysis_only' ? 'default' : ['missed_cash', 'missed_untradable', 'missed_window', 'cancelled_price'].includes(status) ? 'error' : 'warning'
const colorType = value => Number(value || 0) >= 0 ? 'error' : 'success'
const hasBuy = row => Boolean(row.buyAt) && Number(row.buyPrice || 0) > 0
const executionModeLabels = {live_after_signal: '信号后实时成交', recovered_target_minute: '历史目标分钟价', scheduled_slot_sell: '分区定时卖出', recovered_slot_sell: '重启恢复卖出', historical_allocation_replay_v1: '历史动态仓位重放'}
const executionMode = trade => executionModeLabels[trade?.executionMode] || trade?.executionMode || '--'
const degradedReason = analysis => analysis?.degraded === null || analysis?.degraded === undefined ? '历史运行未记录证据质量' : analysis.degraded ? '辅助证据不完整，具体来源状态请查看证据审计' : '无'
const rankLabel = value => Number(value) > 0 ? formatInteger(value) : '--'
const statusLabel = row => statusLabels[row.status] || row.status
const hasScoreExplanation = analysis => /^#{1,6}\s+分项评分依据\s*$/m.test(analysis?.reportMarkdown || '')

const show = row => detailRequest.show(row.recommendationId)

const signalDateFormatter = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'})
function signalDate(value) {
  if (!value) return ''
  const text = String(value)
  const date = new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(text) ? text : `${text.replace(' ', 'T')}${text.length === 10 ? 'T00:00:00' : ''}+08:00`)
  return Number.isNaN(date.getTime()) ? '' : signalDateFormatter.format(date)
}
function recommendationRowClass(row, index) {
  if (index === 0) return ''
  const currentDate = signalDate(row.signalAt), previousDate = signalDate(rows.value[index - 1]?.signalAt)
  return currentDate && previousDate && currentDate !== previousDate ? 'recommendation-date-start' : ''
}

const defaultColumns = [
  {title: '信号时间', key: 'signalAt', width: 170, render: row => dateTime(row.signalAt)},
  {title: '排名', key: 'displaySelectionRank', width: 80, render: row => rankLabel(row.displaySelectionRank)},
  {title: '股票', key: 'stockCode', minWidth: 170, render: row => h(NButton, {text: true, type: 'primary', onClick: () => show(row)}, {default: () => `${row.stockName}（${row.stockCode}）`})},
  {title: '分数', key: 'finalScore', width: 110, render: row => row.slot === 'base43' ? `${formatNumber(row.finalScore, 4)}%` : formatNumber(row.finalScore, 1)},
  {title: '参考价', key: 'referencePrice', width: 95, render: row => formatPrice(row.referencePrice)},
  {title: '成交数量', key: 'quantity', width: 100, render: row => hasBuy(row) ? formatInteger(row.quantity) : '--'},
  {title: '买/卖价', key: 'buyPrice', width: 150, render: row => `${hasBuy(row) ? formatPrice(row.buyPrice) : '--'} / ${Number(row.sellPrice || 0) > 0 ? formatPrice(row.sellPrice) : '--'}`},
  {title: '当前价', key: 'currentPrice', width: 100, render: row => ['active', 'sell_pending'].includes(row.status) && Number(row.currentPrice || 0) > 0 ? formatPrice(row.currentPrice) : '--'},
  {title: '收益率', key: 'netYieldRate', width: 100, render: row => hasBuy(row) ? h(NText, {type: colorType(row.netYieldRate)}, {default: () => formatPercent(row.netYieldRate)}) : '--'},
  {title: '状态', key: 'status', width: 110, render: row => h(NTag, {type: statusType(row.status), bordered: false}, {default: () => statusLabel(row)})},
]
const {tableRef, columnsRef} = useDraggableDataTableColumns(defaultColumns, 'stock-god:prediction-recommendations:column-order:v2')

const refresh = history.refresh

onMounted(refresh)
</script>

<template>
  <n-space vertical>
    <n-alert type="info" :bordered="false">BASE43 五模型均值按正分降序、同分代码排序，最多两只。预算冻结为盘前现金；买入窗口 09:30:00—09:30:59，失败席位保留现金，不补排、不重分。</n-alert>
    <n-flex justify="space-between" align="center">
      <n-text depth="3">现役只有 BASE43 模拟账户。两席等分盘前现金，单席使用全部盘前现金；按 100 股整手、含费预算成交，不使用当天卖款。归档历史只读。</n-text>
      <n-button :loading="loading" @click="refresh">刷新</n-button>
    </n-flex>
    <div ref="tableRef">
      <n-data-table :columns="columnsRef" :data="rows" :row-class-name="recommendationRowClass" :loading="loading" :scroll-x="1185" :row-key="row => row.recommendationId"/>
    </div>
    <PredictionHistoryFooter :count="rows.length" :has-more="hasMore" :loading="loading" :error="listError" @load-more="history.loadMore"/>
  </n-space>

  <n-modal v-model:show="visible">
    <n-card class="research-detail-card" title="推荐与成交详情" closable @close="visible=false">
      <n-scrollbar style="max-height:87vh">
        <n-alert v-if="detailError" type="error">{{ detailError }} <n-button text @click="detailRequest.refresh">重试</n-button></n-alert>
        <n-spin :show="detailLoading">
          <template v-if="detail">
            <n-alert v-if="detail.recommendation.status === 'analysis_only'" type="info" :bordered="false" style="margin-bottom:12px">该推荐仅用于研究复盘，不会创建模拟成交或计入策略收益。</n-alert>
            <n-descriptions bordered :column="3">
              <n-descriptions-item label="股票">{{detail.recommendation.stockName}}（{{detail.recommendation.stockCode}}）</n-descriptions-item>
              <n-descriptions-item :label="detail.recommendation.slot === 'base43' ? '预测收益分数' : '最终分'">{{detail.recommendation.slot === 'base43' ? `${formatNumber(detail.recommendation.finalScore, 4)}%` : formatNumber(detail.recommendation.finalScore, 1)}}</n-descriptions-item>
              <n-descriptions-item label="状态">{{statusLabel(detail.recommendation)}}</n-descriptions-item>
              <n-descriptions-item label="列表排名">{{rankLabel(detail.recommendation.displaySelectionRank)}}</n-descriptions-item>
              <n-descriptions-item label="报告排名">{{rankLabel(detail.recommendation.selectionRank)}}</n-descriptions-item>
              <n-descriptions-item label="执行报价">{{formatPrice(detail.recommendation.executionQuotePrice)}} / {{dateTime(detail.recommendation.executionQuoteAt)}}</n-descriptions-item>
              <n-descriptions-item label="涨停距离">{{detail.recommendation.executionLimitDistancePct === null || detail.recommendation.executionLimitDistancePct === undefined ? '--' : `${formatNumber(detail.recommendation.executionLimitDistancePct, 3)}%`}}</n-descriptions-item>
              <n-descriptions-item v-if="detail.recommendation.failureReason || detail.recommendation.executionFailureCode" label="不成交原因" :span="3">{{detail.recommendation.failureReason || detail.recommendation.executionFailureCode}}</n-descriptions-item>
              <n-descriptions-item label="启动时间">{{dateTime(detail.analysis.startedAt)}}</n-descriptions-item>
              <n-descriptions-item label="报告产生时间">{{dateTime(detail.analysis.generatedAt)}}</n-descriptions-item>
              <n-descriptions-item label="目标 / 实际买入">{{detail.recommendation.status === 'analysis_only' ? '--' : dateTime(detail.recommendation.targetBuyAt)}} / {{dateTime(detail.recommendation.buyAt)}}</n-descriptions-item>
              <n-descriptions-item label="目标 / 实际卖出">{{dateTime(detail.recommendation.targetSellAt)}} / {{dateTime(detail.recommendation.sellAt)}}</n-descriptions-item>
              <n-descriptions-item label="证据降级" :span="3">{{degradedReason(detail.analysis)}}</n-descriptions-item>
              <n-descriptions-item v-if="detail.recommendation.slot !== 'base43'" label="分项评分" :span="3">市场 {{formatNumber(detail.recommendation.marketScore, 1)}} / 板块 {{formatNumber(detail.recommendation.sectorScore, 1)}} / 个股 {{formatNumber(detail.recommendation.stockScore, 1)}} / 催化 {{formatNumber(detail.recommendation.catalystScore, 1)}}；风险扣分 {{formatNumber(detail.recommendation.riskDeduction, 1)}}。<span v-if="!hasScoreExplanation(detail.analysis)">历史未记录逐项评分说明。</span><span v-else>逐项依据与来源见完整报告。</span></n-descriptions-item>
              <n-descriptions-item label="入选理由" :span="3">{{detail.recommendation.summary}}</n-descriptions-item>
              <n-descriptions-item label="关键量化" :span="3">{{detail.recommendation.quantData}}</n-descriptions-item>
              <n-descriptions-item label="新催化" :span="3">{{detail.recommendation.freshCatalyst || '无可核验新催化'}}</n-descriptions-item>
              <n-descriptions-item label="主要风险" :span="3">{{detail.recommendation.mainRisk}}</n-descriptions-item>
              <n-descriptions-item label="取消条件" :span="3">{{detail.recommendation.cancelConditions}}</n-descriptions-item>
            </n-descriptions>
            <n-divider title-placement="left">持仓期分钟走势</n-divider>
            <PredictionTradeChart :recommendation-id="detail.recommendation.recommendationId" :fallback-trades="detail.trades || []"/>
            <n-divider title-placement="left">完整报告</n-divider>
            <AppMarkdownPreview :model-value="detail.analysis.reportMarkdown || '暂无报告'"/>
            <n-divider title-placement="left">成交记录</n-divider>
            <n-data-table :data="detail.trades || []" :columns="[
              {title:'方向',key:'side'},
              {title:'时间',key:'tradedAt',render:r=>dateTime(r.tradedAt)},
              {title:'市场价',key:'marketPrice',render:r=>formatPrice(r.marketPrice)},
              {title:'成交价',key:'executionPrice',render:r=>formatPrice(r.executionPrice)},
              {title:'价格来源',key:'priceSource',render:r=>`${r.priceSource || '--'}${r.priceStale ? '（历史价格）' : ''}`},
              {title:'执行模式',key:'executionMode',render:r=>executionMode(r)},
              {title:'数量',key:'quantity',render:r=>formatInteger(r.quantity)},
              {title:'净现金流',key:'netCashFlow',render:r=>formatMoney(r.netCashFlow)}
            ]"/>
          </template>
        </n-spin>
      </n-scrollbar>
    </n-card>
  </n-modal>
</template>

<style scoped>
:deep(.recommendation-date-start > td) {
  border-top: 2px solid #000;
}

:deep(.draggable-column-title) {
  display: inline-flex;
  width: 100%;
  cursor: grab;
  user-select: none;
}

:deep(.draggable-column-title.column-dragging) {
  opacity: 0.55;
}

:deep(.draggable-column-title.column-drag-over) {
  box-shadow: inset 3px 0 0 #18a058;
}

.research-detail-card {
  width: min(1600px, 96vw);
  max-height: 96vh;
}
</style>
