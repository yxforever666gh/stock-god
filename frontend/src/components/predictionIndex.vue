<script setup>
import {computed, defineAsyncComponent, h, onMounted, ref, watch} from 'vue'
import {useRoute, useRouter} from 'vue-router'
import {PREDICTION_SLOTS, predictionSlotBuyLabel, validPredictionSlot} from '../utils/prediction-slots.js'
import {usePolling} from '../composables/usePolling.js'
import {ListPredictionSlots} from '../services/prediction-api'

const tabs = [
  {name: '股票推荐记录', component: defineAsyncComponent(() => import('./predictionRecommendations.vue'))},
  {name: 'AI分析报告', component: defineAsyncComponent(() => import('./predictionReport.vue'))},
  {name: '股票收益率', component: defineAsyncComponent(() => import('./predictionYield.vue'))},
]
const route = useRoute()
const router = useRouter()
const selectedSlot = ref(validPredictionSlot(route.query.slot) ? String(route.query.slot) : '09:50')
const slotStates = ref([])
async function refreshSlots() { try { slotStates.value = await ListPredictionSlots() || [] } catch { slotStates.value = [] } }
const slotPolling = usePolling(refreshSlots, 15000, {shouldRun: () => nowTab.value !== 'AI分析报告'})
const selectedSlotInfo = computed(() => PREDICTION_SLOTS.find(slot => slot.value === selectedSlot.value) || PREDICTION_SLOTS[0])
const selectedSlotState = computed(() => slotStates.value.find(item => item.slot === selectedSlot.value))
const slotOptions = computed(() => PREDICTION_SLOTS.map(slot => {
 const state = slotStates.value.find(item => item.slot === slot.value)
 return {key: slot.value, label: slot.label, summary: predictionSlotBuyLabel(state)}
}))
function renderSlotLabel(option) {
 const current = option.key === selectedSlot.value
 return h('div', {class: 'prediction-slot-option-label'}, [
  h('span', {class: 'prediction-slot-option-title'}, String(option.label)),
  current ? h('span', {class: 'prediction-slot-current-mark'}, '当前') : null,
  h('span', {class: 'prediction-slot-option-summary'}, String(option.summary || '买入：等待报告')),
 ])
}
function slotNodeProps(option) {
 const current = option.key === selectedSlot.value
 return current ? {class: 'prediction-slot-option-current', 'aria-current': 'true'} : {'aria-current': 'false'}
}
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
 selectedSlot.value = slot
 if (route.query.slot !== slot) router.replace({name: 'prediction', query: {...route.query, slot}})
 void refreshSlots()
}
watch(() => route.query.slot, slot => updateSlot(validPredictionSlot(slot) ? String(slot) : '09:50'))
const nowTab = ref(tabs.some(tab => tab.name === route.query.name) ? String(route.query.name) : tabs[0].name)
const visited = ref([nowTab.value])
const validReportDay = value => ['recent5', 'all'].includes(value) || /^\d{4}-\d{2}-\d{2}$/.test(String(value || ''))
const reportDay = ref(validReportDay(route.query.reportDay) ? String(route.query.reportDay) : 'recent5')
const reportDates = ref([])
const reportDayOptions = computed(() => [
  {key: 'recent5', label: '最近5个交易日'},
  {key: 'all', label: '全部交易日'},
  ...reportDates.value.map(day => ({key: day, label: day})),
])
const reportDayLabel = computed(() => reportDayOptions.value.find(item => item.key === reportDay.value)?.label || reportDay.value)
function updateReportDates(dates) {
  reportDates.value = [...new Set((dates || []).filter(day => /^\d{4}-\d{2}-\d{2}$/.test(day)))].sort().reverse()
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
  if (name !== 'AI分析报告') void refreshSlots()
}
watch(() => route.query.name, name => updateTab(String(name || tabs[0].name)))
onMounted(() => slotPolling.start({immediate: true}))
</script>

<template>
  <n-card>
    <div class="prediction-slot-toolbar">
      <n-dropdown v-if="nowTab === 'AI分析报告'" trigger="click" :options="reportDayOptions" @select="updateReportDay">
        <n-button secondary size="small" class="prediction-slot-current-button" aria-label="选择交易日">
          <span class="prediction-slot-current-prefix">当前选择</span>
          <strong>{{ reportDayLabel }}</strong>
          <span class="prediction-slot-chevron" aria-hidden="true">⌄</span>
        </n-button>
      </n-dropdown>
      <n-dropdown v-else trigger="click" :options="slotOptions" :render-label="renderSlotLabel" :node-props="slotNodeProps" @select="updateSlot">
        <n-button secondary size="small" class="prediction-slot-current-button" aria-label="选择五分钟区间">
          <span class="prediction-slot-current-prefix">当前选择</span>
          <strong>{{ selectedSlotInfo.label }}</strong>
          <span class="prediction-slot-chevron" aria-hidden="true">⌄</span>
        </n-button>
      </n-dropdown>
      <div v-if="nowTab !== 'AI分析报告'" class="prediction-slot-status" aria-live="polite">
        <n-tag size="small" :type="buyTagType(selectedSlotState)" bordered="false">{{ predictionSlotBuyLabel(selectedSlotState) }}</n-tag>
      </div>
    </div>
    <n-tabs type="line" animated :value="nowTab" @update-value="updateTab">
      <n-tab-pane v-for="tab in tabs" :key="tab.name" :name="tab.name" :tab="tab.name">
        <component v-if="visited.includes(tab.name)" :is="tab.component"
          :key="tab.name === 'AI分析报告' ? tab.name : `${tab.name}:${selectedSlot}`"
          v-bind="tab.name === 'AI分析报告' ? {reportDay, active: nowTab === tab.name} : {slot: selectedSlot}"
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

:global(.prediction-slot-option-current .n-dropdown-option-body) {
  background: linear-gradient(90deg, #e9f8ee, #f9fffb);
  box-shadow: inset 3px 0 #18a058, 0 4px 12px rgba(24, 160, 88, .14);
  font-weight: 700;
}

.prediction-slot-option-label {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 2px 8px;
  min-width: 168px;
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
</style>
