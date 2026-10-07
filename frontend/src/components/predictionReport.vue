<script setup>
import {computed, h, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import {NButton, NTag} from 'naive-ui'
import AppMarkdownPreview from './AppMarkdownPreview.vue'
import PredictionAuditPanel from './prediction-audit/PredictionAuditPanel.vue'
import {usePredictionDetail} from '../composables/usePredictionRequests.js'
import {usePolling} from '../composables/usePolling.js'
import {BrowsePredictionRuns, GetPredictionRun} from '../services/prediction-api'

const props = defineProps({reportDay: {type: String, default: 'recent5'}, active: {type: Boolean, default: true}})
const emit = defineEmits(['trading-dates'])
const allReports = ref(true)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / 100)))
const loading = ref(false)
const listError = ref('')
const detailRequest = usePredictionDetail(GetPredictionRun)
const {detail, visible, loading: detailLoading, error: detailError} = detailRequest
let requestVersion = 0
const labels = {running: '分析中', success: '已推荐', no_recommendation: '空仓', failed: '失败', missed_window: '错过交易窗口'}
const emailLabels = {pending: '待发送', sending: '发送中', retry_wait: '等待重试', sent: '已发送', failed: '发送失败', cancelled: '已取消'}
const dateTime = value => value ? String(value).slice(0, 19).replace('T', ' ') : '--'
const coverage = value => value !== null && value !== undefined && Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)}%` : '--'
const qualityLabel = value => value === null || value === undefined ? '未记录' : value ? '降级' : '完整'
const qualityType = value => value === null || value === undefined ? 'default' : value ? 'warning' : 'success'
const degradedReason = run => run?.degraded === null || run?.degraded === undefined ? '历史运行未记录证据质量' : run.degraded ? '辅助证据不完整，具体来源状态请查看证据审计' : '无'
const shortFailureReason = value => { const text = String(value || '').replace(/\s+/g, ' ').trim(); return text.length > 160 ? `${text.slice(0, 160)}…（完整信息见审计）` : text || '--' }
const type = status => status === 'success' ? 'success' : status === 'failed' ? 'error' : status === 'running' ? 'warning' : 'info'
const show = row => detailRequest.show(row.runId)
const columns = [
 {title: '计划区间', key: 'scheduledSlot', width: 100},
 {title: '所属区间', key: 'slot', width: 100, render: row => row.slot || '--'},
 {title: '展示状态', key: 'published', width: 120, render: row => row.published ? '区间首份报告' : row.status === 'running' ? '运行中' : '仅保留报告'},
 {title: '归档原因', key: 'archiveReason', minWidth: 200},
  {title: '交易日', key: 'tradingDate', width: 110},
  {title: '尝试', key: 'attemptNo', width: 80, render: row => `第${row.attemptNo || 1}次`},
  {title: '触发', key: 'triggerSource', width: 125, render: row => row.triggerSource || '--'},
  {title: '启动时间', key: 'startedAt', width: 170, render: row => dateTime(row.startedAt)},
  {title: '报告产生时间', key: 'generatedAt', width: 170, render: row => dateTime(row.generatedAt)},
  {title: '证据覆盖', key: 'evidenceCoveragePct', width: 105, render: row => coverage(row.evidenceCoveragePct)},
  {title: '证据质量', key: 'degraded', width: 100, render: row => h(NTag, {type: qualityType(row.degraded), bordered: false}, {default: () => qualityLabel(row.degraded)})},
  {title: '时效', key: 'onTime', width: 90, render: row => h(NTag, {type: row.onTime ? 'success' : 'warning', bordered: false}, {default: () => row.onTime ? '准时' : '迟到'})},
  {title: '状态', key: 'status', width: 120, render: row => h(NTag, {type: type(row.status), bordered: false}, {default: () => labels[row.status] || row.status})},
  {title: '模型', key: 'modelName', minWidth: 180, render: row => `${row.providerName || '--'} / ${row.modelName || '--'}`},
  {title: '推荐数', key: 'recommendationCount', width: 90},
  {title: '邮件', key: 'emailDeliveryStatus', width: 110, render: row => h(NTag, {type: row.emailDeliveryStatus === 'sent' ? 'success' : row.emailDeliveryStatus === 'failed' ? 'error' : 'info', bordered: false}, {default: () => emailLabels[row.emailDeliveryStatus] || '未排队'})},
  {title: '说明', key: 'failureReason', minWidth: 220, ellipsis: {tooltip: true}, render: row => shortFailureReason(row.failureReason)},
  {title: '操作', key: 'action', width: 90, render: row => h(NButton, {size: 'small', tertiary: true, type: 'primary', onClick: () => show(row)}, {default: () => '查看'})},
]
async function loadPage(nextPage = page.value, refreshDetail = false) {
  const version = ++requestVersion
  loading.value = true
  listError.value = ''
  try {
    const result = await BrowsePredictionRuns(nextPage, props.reportDay, allReports.value)
    if (version !== requestVersion) return
    rows.value = result.items || []
    total.value = result.total || 0
    page.value = nextPage
    emit('trading-dates', result.tradingDates || [])
    if (visible.value && (refreshDetail || !detail.value || detail.value.status === 'running')) await detailRequest.refresh()
  } catch (reason) {
    if (version === requestVersion) listError.value = reason?.message || String(reason)
  } finally {
    if (version === requestVersion) loading.value = false
  }
}
const refresh = () => loadPage(page.value, true)
const polling = usePolling(async () => {
  await loadPage(1)
}, 5000, {shouldRun: () => props.active && page.value === 1 && rows.value.some(row => row.status === 'running')})
watch(allReports, () => { void loadPage(1) })
watch(() => props.reportDay, () => { void loadPage(1) })
watch(() => props.active, active => { if (active) void loadPage(page.value) })
onMounted(() => { void loadPage(1); polling.start({immediate: false}) })
onBeforeUnmount(() => { requestVersion++ })
</script>

<template>
  <n-space vertical>
    <n-alert type="info" :bordered="false">BASE43 使用原 43 特征与最近五模型均值，固定最多两名正分标的。盘前输入截至 09:29:59；竞价 API 未配置时不会生成选股报告。旧 AI 报告保留为只读历史。</n-alert>
    <n-checkbox v-model:checked="allReports">全部报告（包含失败、后到和午休后报告）</n-checkbox>
    <n-flex justify="end"><n-button :loading="loading" @click="refresh">刷新</n-button></n-flex>
    <n-alert v-if="listError" type="error" :bordered="false">{{ listError }}</n-alert>
    <n-data-table :columns="columns" :data="rows" :loading="loading" :scroll-x="2210" :row-key="row => row.runId"/>
    <n-flex justify="space-between" align="center">
      <n-text depth="3">共 {{ total }} 条；每页 100 条</n-text>
      <n-pagination :page="page" :page-count="pageCount" :page-size="100" :disabled="loading" @update:page="next => loadPage(next)"/>
    </n-flex>
  </n-space>
  <n-modal v-model:show="visible">
    <n-card title="模型与归档报告" closable style="width:min(1380px,96vw);max-height:94vh" @close="visible=false">
      <n-scrollbar style="max-height:82vh">
        <n-alert v-if="detailError" type="error">{{ detailError }} <n-button text @click="detailRequest.refresh">重试</n-button></n-alert>
        <n-spin :show="detailLoading">
          <template v-if="detail">
            <PredictionAuditPanel :owner-id="String(detail.runId)" :active="visible">
              <template #final-result>
                <n-descriptions bordered :column="3" style="margin-bottom:12px">
                  <n-descriptions-item label="分析尝试">第{{detail.attemptNo || 1}}次</n-descriptions-item>
                  <n-descriptions-item label="触发来源">{{detail.triggerSource || '--'}}</n-descriptions-item>
                  <n-descriptions-item label="本次可买名额">{{detail.requestedSlots || 0}}</n-descriptions-item>
                  <n-descriptions-item label="启动时间">{{dateTime(detail.startedAt)}}</n-descriptions-item>
                  <n-descriptions-item label="报告产生时间">{{dateTime(detail.generatedAt)}}</n-descriptions-item>
                  <n-descriptions-item label="证据覆盖">{{coverage(detail.evidenceCoveragePct)}}</n-descriptions-item>
                  <n-descriptions-item label="证据质量" :span="3">
                    <n-tag :type="qualityType(detail.degraded)" :bordered="false">{{qualityLabel(detail.degraded)}}</n-tag>
                    <span style="margin-left:8px">{{degradedReason(detail)}}</span>
                  </n-descriptions-item>
                </n-descriptions>
                <n-alert type="info" :show-icon="false" style="margin-bottom:12px">
                  邮件：{{ emailLabels[detail.emailDeliveryStatus] || '未排队' }}；尝试 {{ detail.emailAttemptCount || 0 }} 次；发送时间 {{ dateTime(detail.emailSentAt) }}
                  <template v-if="detail.emailLastError">；错误：{{ detail.emailLastError }}</template>
                </n-alert>
                <AppMarkdownPreview :model-value="detail.reportMarkdown || detail.failureReason || '暂无报告'"/>
              </template>
            </PredictionAuditPanel>
          </template>
        </n-spin>
      </n-scrollbar>
    </n-card>
  </n-modal>
</template>
