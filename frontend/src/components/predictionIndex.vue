<script setup>
import {computed, defineAsyncComponent, onMounted, ref, watch} from 'vue'
import {useRoute, useRouter} from 'vue-router'
import {ARCHIVED_PREDICTION_SLOTS, PREDICTION_SLOTS, predictionSlotBuyLabel, validPredictionSlot} from '../utils/prediction-slots.js'
import {usePolling} from '../composables/usePolling.js'
import {ListPredictionSlots} from '../services/prediction-api'

const tabs = [
  {name: '股票推荐记录', component: defineAsyncComponent(() => import('./predictionRecommendations.vue'))},
  {name: '模型与归档报告', component: defineAsyncComponent(() => import('./predictionReport.vue'))},
  {name: '股票收益率', component: defineAsyncComponent(() => import('./predictionYield.vue'))},
]
const route = useRoute()
const router = useRouter()
const selectedSlot = ref(validPredictionSlot(route.query.slot) ? String(route.query.slot) : 'base43')
const slotPickerOpen = ref(false)
const slotStates = ref([])
async function refreshSlots() { try { slotStates.value = await ListPredictionSlots() || [] } catch { slotStates.value = [] } }
const slotPolling = usePolling(refreshSlots, 15000, {shouldRun: () => nowTab.value !== '模型与归档报告'})
const selectedSlotInfo = computed(() => [...PREDICTION_SLOTS, ...ARCHIVED_PREDICTION_SLOTS].find(slot => slot.value === selectedSlot.value) || PREDICTION_SLOTS[0])
const selectedSlotState = computed(() => slotStates.value.find(item => item.slot === selectedSlot.value))
const slotOptions = computed(() => [...PREDICTION_SLOTS, ...ARCHIVED_PREDICTION_SLOTS].map(slot => {
 const state = slotStates.value.find(item => item.slot === slot.value)
 return {key: slot.value, label: slot.value === 'base43' ? slot.label : `归档 ${slot.label}`, summary: predictionSlotBuyLabel(state)}
}))
function buyTagType(state) {
 switch (state?.buyStatus) {
  case 'bought_full': return 'success'
  case 'bought_partial': return 'warning'
  case 'awaiting_quote', 'processing': return 'info'
  case 'cutoff', 'failed': return 'error'
  case 'disabled', 'no_recommendation', 'no_purchase': return 'default'
  default: return 'default'
 }
}
function updateSlot(slot) {
 if (!validPredictionSlot(slot)) return
 slotPickerOpen.value = false
 selectedSlot.value = slot
 if (route.query.slot !== slot) router.replace({name: 'prediction', query: {...route.query, slot}})
 void refreshSlots()
}
watch(() => route.query.slot, slot => updateSlot(validPredictionSlot(slot) ? String(slot) : 'base43'))
const nowTab = ref(tabs.some(tab => tab.name === route.query.name) ? String(route.query.name) : tabs[0].name)
const visited = ref([nowTab.value])
function validReportDay(value) {
  if (['recent5', 'all'].includes(value)) return true
  const text = String(value || '')
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return false
  const date = new Date(`${text}T00:00:00Z`)
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === text
}
const reportDay = ref(validReportDay(route.query.reportDay) ? String(route.query.reportDay) : 'recent5')
const reportDates = ref([])
const reportDateSet = computed(() => new Set(reportDates.value))
const reportDateValue = computed(() => reportDateSet.value.has(reportDay.value) ? reportDay.value : null)
function updateReportDates(dates) {
  reportDates.value = [...new Set((dates || []).filter(day => validReportDay(day) && !['recent5', 'all'].includes(day)))].sort().reverse()
  if (!['recent5', 'all'].includes(reportDay.value) && !reportDateSet.value.has(reportDay.value)) updateReportDay('recent5')
}
function reportDateDisabled(timestamp) {
  const date = new Date(timestamp)
  const day = [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-')
  return !reportDateSet.value.has(day)
}
function updateReportDay(day) {
  if (!validReportDay(day)) return
  reportDay.value = day
  if (route.query.reportDay !== day) router.replace({name: 'prediction', query: {...route.query, reportDay: day}})
}
watch(() => route.query.reportDay, day => updateReportDay(validReportDay(day) ? String(day) : 'recent5'))

function updateTab(name) {
  if (!tabs.some(tab => tab.name === name)) return
  nowTab.value = name
  if (!visited.value.includes(name)) visited.value.push(name)
  if (route.query.name !== name) router.replace({name: 'prediction', query: {...route.query, name}})
  if (name !== '模型与归档报告') void refreshSlots()
}
watch(() => route.query.name, name => updateTab(String(name || tabs[0].name)))
onMounted(() => slotPolling.start({immediate: true}))
</script>

<template>
  <n-card>
    <n-alert v-if="selectedSlot === 'base43'" type="info" :bordered="false">{{ predictionSlotBuyLabel(selectedSlotState || {slot: 'base43'}) }}。模型：{{ selectedSlotState?.modelReady ? '已准备' : '未准备' }}。BASE43 初始本金 30,000 元，收益按实际模拟成交单独计算。</n-alert>
    <n-alert v-else-if="selectedSlot !== 'base43'" type="info" :bordered="false">归档账户，只读；停止交易、估值与恢复写入。</n-alert>
    <div class="prediction-slot-toolbar">
      <template v-if="nowTab === '模型与归档报告'">
        <n-button-group>
          <n-button size="small" :type="reportDay === 'recent5' ? 'primary' : 'default'" @click="updateReportDay('recent5')">最近5个交易日</n-button>
          <n-button size="small" :type="reportDay === 'all' ? 'primary' : 'default'" @click="updateReportDay('all')">全部交易日</n-button>
        </n-button-group>
        <n-date-picker :formatted-value="reportDateValue" type="date" value-format="yyyy-MM-dd" clearable
          placeholder="日历选择交易日" aria-label="日历选择交易日" class="prediction-report-date"
          :disabled="!reportDates.length" :is-date-disabled="reportDateDisabled"
          @update:formatted-value="day => updateReportDay(day || 'recent5')"/>
      </template>
      <n-popover v-else v-model:show="slotPickerOpen" trigger="click" placement="bottom-start" :show-arrow="false">
        <template #trigger>
          <n-button secondary size="small" class="prediction-slot-current-button" aria-label="选择现役或归档账户">
            <span class="prediction-slot-current-prefix">账户</span>
            <strong>{{ selectedSlotInfo.label }}</strong>
            <span class="prediction-slot-chevron" aria-hidden="true">⌄</span>
          </n-button>
        </template>
        <div class="prediction-slot-grid" role="group" aria-label="选择现役或归档账户">
          <n-button v-for="option in slotOptions" :key="option.key" size="small" secondary
            class="prediction-slot-grid-item" :type="option.key === selectedSlot ? 'primary' : 'default'"
            :aria-current="option.key === selectedSlot ? 'true' : undefined" @click="updateSlot(option.key)">
            <span class="prediction-slot-option-title">{{ option.label }}</span>
            <span v-if="option.key === selectedSlot" class="prediction-slot-current-mark">当前</span>
            <span class="prediction-slot-option-summary">{{ option.summary }}</span>
          </n-button>
        </div>
      </n-popover>
      <div v-if="nowTab !== '模型与归档报告'" class="prediction-slot-status" aria-live="polite">
        <n-tag size="small" :type="buyTagType(selectedSlotState)" bordered="false">{{ predictionSlotBuyLabel(selectedSlotState) }}</n-tag>
      </div>
    </div>
    <n-tabs type="line" animated :value="nowTab" @update-value="updateTab">
      <n-tab-pane v-for="tab in tabs" :key="tab.name" :name="tab.name" :tab="tab.name">
        <component v-if="visited.includes(tab.name)" :is="tab.component"
          :key="tab.name === '模型与归档报告' ? tab.name : `${tab.name}:${selectedSlot}`"
          v-bind="tab.name === '模型与归档报告' ? {reportDay, active: nowTab === tab.name} : {slot: selectedSlot}"
          @trading-dates="updateReportDates"/>
      </n-tab-pane>
    </n-tabs>
  </n-card>
</template>

<style scoped>
.prediction-slot-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  min-height: 34px;
  margin-bottom: 16px;
}

.prediction-slot-current-button {
  border-color: #18a058 !important;
  background: linear-gradient(135deg, #ecf9f0, #f8fffa) !important;
  box-shadow: 0 5px 14px rgba(24, 160, 88, .2);
  color: #087443;
  font-weight: 700;
}

.prediction-slot-current-prefix {
  margin-right: 6px;
  color: #18a058;
  font-size: 12px;
  font-weight: 700;
}

.prediction-slot-status {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.prediction-slot-chevron {
  margin-left: 8px;
  font-size: 14px;
  line-height: 1;
}

.prediction-report-date {
  width: 190px;
}

.prediction-slot-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  width: min(720px, calc(100vw - 48px));
  max-height: min(400px, 65vh);
  overflow-y: auto;
}

.prediction-slot-grid-item {
  height: 54px;
  padding: 4px 8px;
}

.prediction-slot-grid-item :deep(.n-button__content) {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 2px 6px;
  width: 100%;
  text-align: left;
}

.prediction-slot-option-title {
  font-weight: 600;
}

.prediction-slot-current-mark {
  align-self: center;
  border-radius: 999px;
  background: #18a058;
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  padding: 0 6px;
}

.prediction-slot-option-summary {
  grid-column: 1 / -1;
  color: #7a7f87;
  font-size: 12px;
  font-weight: 400;
}

@media (max-width: 760px) {
  .prediction-slot-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
