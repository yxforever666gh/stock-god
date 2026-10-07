<script setup>
import {h, onBeforeUnmount, onMounted, ref} from 'vue'
import {NTag, useMessage} from 'naive-ui'
import {GetConfig, GetPredictionConfig, TestAIConfig, TestPredictionEmail, UpdateConfig, UpdatePredictionConfig} from '../services/settings-api'
import {EventsEmit} from '../services/browser-runtime.mjs'
import MinuteProviderSettings from './settings/MinuteProviderSettings.vue'
import AiConfigSettings from './settings/AiConfigSettings.vue'
import {acceptSavedModelIDs, importPredictionSettings, predictionPayload} from './settings/prediction-settings.js'
import {PREDICTION_SLOTS, validPredictionSlot} from '../utils/prediction-slots.js'

const message = useMessage()
const formRef = ref(null)
const formValue = ref({
  darkTheme: true,
  tushareToken: '',
  qgqpBId: '',
  updateBasicInfoOnStart: false,
  refreshInterval: 1,
  minuteLongHistoryHintEnabled: true,
  minuteProviderOrder: ['tencent', 'sina', 'akshare', 'private'],
  akshareEnabled: true,
  sinaMinuteEnabled: true,
  tencentMinuteEnabled: true,
  akshareMinuteSourceMode: 'auto',
  privateMinute: {
    enabled: false,
    baseUrl: '',
    apiKey: '',
    timeoutSec: 60,
    minIntervalMs: 1200,
    proxyMode: 'disable',
    level: '1min',
  },
  openAI: {aiConfigs: []},
  experimentalEvidenceEnabled: false,
  predictionAutoEnabled: true,
  meozApiKey: '',
  predictionEmail: {
    enabled: false,
    slots: [],
    to: '',
    from: '',
    smtpHost: '',
    smtpPort: 465,
    smtpUsername: '',
    smtpPassword: '',
  },
})

const aiConfigTestStates = ref({})
const settingsLoaded = ref(false)
const persistedConfig = ref({revision: 0, config: {}, aiConfigs: []})
const draftConfig = ref({})
const autoSaveState = ref('idle')
const autoSaveError = ref('')
const autoSaveLastSavedAt = ref('')
const predictionEmailTesting = ref(false)
let activeSavePromise = null
let queuedAutoSave = false
let pageVersion = 0
let globalSavePromise = Promise.resolve()
let nextAiConfigLocalKey = 1
const aiConfigLocalKeys = new WeakMap()

const akshareMinuteSourceOptions = [
  {label: '自动', value: 'auto'},
  {label: '新浪', value: 'sina'},
  {label: '东方财富', value: 'em'},
]
const privateMinuteProxyModeOptions = [
  {label: '强制直连', value: 'disable'},
  {label: '跟随系统代理', value: 'inherit'},
]
const privateMinuteLevelOptions = [
  {label: '1 分钟', value: '1min'},
  {label: '5 分钟', value: '5min'},
  {label: '15 分钟', value: '15min'},
  {label: '30 分钟', value: '30min'},
  {label: '60 分钟', value: '60min'},
]
const aiProtocolOptions = [
  {label: 'Chat Completions', value: 'chat_completions'},
  {label: 'OpenAI Responses', value: 'openai_responses'},
  {label: 'Anthropic Messages', value: 'anthropic_messages'},
]
const predictionEmailSlotOptions = PREDICTION_SLOTS

function normalizeAiProtocol(value) {
  return ['openai_responses', 'anthropic_messages'].includes(String(value || '').trim())
    ? String(value).trim()
    : 'chat_completions'
}

function normalizeAiConfigs(configs) {
  return (configs || []).map((item, index) => ({
    ...item,
    sort: index + 1,
    disabled: item?.disabled === true,
    apiProtocol: normalizeAiProtocol(item?.apiProtocol),
  }))
}

function normalizeProviderOrder(order) {
  const valid = ['tencent', 'sina', 'akshare', 'private']
  const normalized = []
  for (const provider of Array.isArray(order) ? order : []) {
    if (valid.includes(provider) && !normalized.includes(provider)) normalized.push(provider)
  }
  for (const provider of valid) {
    if (!normalized.includes(provider)) normalized.push(provider)
  }
  return normalized
}

function applyConfigToForm(config) {
  const aiConfigs = normalizeAiConfigs(config?.aiConfigs || [])
  formValue.value.tushareToken = config?.tushareToken || ''
  formValue.value.qgqpBId = config?.qgqpBId || ''
  formValue.value.minuteLongHistoryHintEnabled = config?.minuteLongHistoryHintEnabled !== false
  formValue.value.minuteProviderOrder = normalizeProviderOrder(config?.minuteProviderOrder)
  formValue.value.akshareEnabled = config?.akshareEnabled !== false
  formValue.value.sinaMinuteEnabled = config?.sinaMinuteEnabled !== false
  formValue.value.tencentMinuteEnabled = config?.tencentMinuteEnabled !== false
  formValue.value.akshareMinuteSourceMode = config?.akshareMinuteSourceMode || 'auto'
  formValue.value.privateMinute = {
    enabled: config?.privateMinuteEnabled === true,
    baseUrl: config?.privateMinuteBaseUrl || '',
    apiKey: config?.privateMinuteApiKey || '',
    timeoutSec: config?.privateMinuteTimeoutSec || 60,
    minIntervalMs: Number.isFinite(config?.privateMinuteMinIntervalMs) ? config.privateMinuteMinIntervalMs : 1200,
    proxyMode: config?.privateMinuteProxyMode || 'disable',
    level: config?.privateMinuteLevel || '1min',
  }
  formValue.value.openAI.aiConfigs = aiConfigs
  formValue.value.experimentalEvidenceEnabled = config?.experimentalEvidenceEnabled === true
  formValue.value.predictionAutoEnabled = config?.predictionAutoEnabled !== false
  formValue.value.meozApiKey = config?.meozApiKey || ''
  formValue.value.predictionEmail = {
    enabled: config?.predictionEmailEnabled === true,
    slots: [...new Set((Array.isArray(config?.predictionEmailSlots) ? config.predictionEmailSlots : []).filter(validPredictionSlot))],
    to: config?.predictionEmailTo || '',
    from: config?.predictionEmailFrom || '',
    smtpHost: config?.predictionEmailSmtpHost || '',
    smtpPort: Number.isFinite(config?.predictionEmailSmtpPort) && config.predictionEmailSmtpPort > 0 ? config.predictionEmailSmtpPort : 465,
    smtpUsername: config?.predictionEmailSmtpUsername || '',
    smtpPassword: config?.predictionEmailSmtpPassword || '',
  }
}

function aiConfigRowKey(aiConfig) {
  if (!aiConfig || typeof aiConfig !== 'object') return `new-${nextAiConfigLocalKey++}`
  if (!aiConfigLocalKeys.has(aiConfig)) aiConfigLocalKeys.set(aiConfig, aiConfig.ID ? `id-${aiConfig.ID}` : `new-${nextAiConfigLocalKey++}`)
  return aiConfigLocalKeys.get(aiConfig)
}

function renumberAiConfigSorts() {
  formValue.value.openAI.aiConfigs.forEach((item, index) => {
    item.sort = index + 1
    item.apiProtocol = normalizeAiProtocol(item.apiProtocol)
  })
}

function addAiConfig() {
  formValue.value.openAI.aiConfigs.push({
    sort: formValue.value.openAI.aiConfigs.length + 1,
    disabled: false,
    name: '',
    baseUrl: 'https://api.deepseek.com',
    apiKey: '',
    modelName: 'deepseek-chat',
    apiProtocol: 'chat_completions',
    temperature: 0.1,
    maxTokens: 4096,
    timeOut: 300,
    httpProxy: '',
    httpProxyEnabled: false,
  })
  queueAutoSave()
}

function removeAiConfig(index) {
  formValue.value.openAI.aiConfigs.splice(index, 1)
  renumberAiConfigSorts()
  queueAutoSave()
}

function moveItem(items, sourceIndex, targetIndex) {
  if (!Number.isInteger(sourceIndex) || !Number.isInteger(targetIndex) || sourceIndex === targetIndex ||
      sourceIndex < 0 || targetIndex < 0 || sourceIndex >= items.length || targetIndex >= items.length) return false
  const [moved] = items.splice(sourceIndex, 1)
  items.splice(targetIndex, 0, moved)
  return true
}

function handleAiConfigMove(sourceIndex, targetIndex) {
  if (moveItem(formValue.value.openAI.aiConfigs, sourceIndex, targetIndex)) {
    renumberAiConfigSorts()
    queueAutoSave()
  }
}

function handleProviderMove(sourceIndex, targetIndex) {
  if (moveItem(formValue.value.minuteProviderOrder, sourceIndex, targetIndex)) queueAutoSave()
}

function aiConfigTestKey(aiConfig, index) {
  return aiConfigRowKey(aiConfig) || `new-${index}`
}

function aiConfigTestState(aiConfig, index) {
  return aiConfigTestStates.value[aiConfigTestKey(aiConfig, index)] || {}
}

async function testAiConfig(index) {
  const current = formValue.value.openAI.aiConfigs[index]
  const version = pageVersion
  const key = aiConfigTestKey(current, index)
  aiConfigTestStates.value = {...aiConfigTestStates.value, [key]: {loading: true, result: null}}
  try {
    if (!await saveCurrentConfig({notifyError: true})) return
    if (version !== pageVersion || !formValue.value.openAI.aiConfigs.includes(current)) return
    const savedKey = aiConfigTestKey(current, index)
    if (!current?.ID) throw new Error('请先保存 AI 配置后再测试')
    const result = await TestAIConfig(Number(current.ID))
    if (version !== pageVersion) return
    aiConfigTestStates.value = {...aiConfigTestStates.value, [key]: {loading: false}, [savedKey]: {loading: false, result}}
    result?.success ? message.success(`模型测试成功：${result.contentPreview || result.message}`) : message.error(result?.message || '模型测试失败')
  } catch (error) {
    if (version !== pageVersion) return
    aiConfigTestStates.value = {...aiConfigTestStates.value, [key]: {loading: false, result: {success: false, message: error?.message || String(error)}}}
    message.error(error?.message || String(error || '模型测试失败'))
  } finally {
    if (version === pageVersion) aiConfigTestStates.value = {...aiConfigTestStates.value, [key]: {...aiConfigTestStates.value[key], loading: false}}
  }
}

function buildConfigPayload() {
  renumberAiConfigSorts()
  const values = {
    ...draftConfig.value,
    tushareToken: formValue.value.tushareToken,
    qgqpBId: formValue.value.qgqpBId,
    minuteLongHistoryHintEnabled: formValue.value.minuteLongHistoryHintEnabled,
    minuteProviderOrder: formValue.value.minuteProviderOrder,
    akshareEnabled: formValue.value.akshareEnabled,
    sinaMinuteEnabled: formValue.value.sinaMinuteEnabled,
    tencentMinuteEnabled: formValue.value.tencentMinuteEnabled,
    akshareMinuteSourceMode: formValue.value.akshareMinuteSourceMode,
    privateMinuteEnabled: formValue.value.privateMinute.enabled,
    privateMinuteBaseUrl: formValue.value.privateMinute.baseUrl,
    privateMinuteApiKey: formValue.value.privateMinute.apiKey,
    privateMinuteTimeoutSec: formValue.value.privateMinute.timeoutSec,
    privateMinuteMinIntervalMs: formValue.value.privateMinute.minIntervalMs,
    privateMinuteProxyMode: formValue.value.privateMinute.proxyMode,
    privateMinuteLevel: formValue.value.privateMinute.level,
    experimentalEvidenceEnabled: formValue.value.experimentalEvidenceEnabled === true,
    predictionAutoEnabled: formValue.value.predictionAutoEnabled,
    meozApiKey: formValue.value.meozApiKey,
    predictionEmailEnabled: formValue.value.predictionEmail.enabled,
    predictionEmailSlots: formValue.value.predictionEmail.slots,
    predictionEmailTo: formValue.value.predictionEmail.to,
    predictionEmailFrom: formValue.value.predictionEmail.from,
    predictionEmailSmtpHost: formValue.value.predictionEmail.smtpHost,
    predictionEmailSmtpPort: formValue.value.predictionEmail.smtpPort,
    predictionEmailSmtpUsername: formValue.value.predictionEmail.smtpUsername,
    predictionEmailSmtpPassword: formValue.value.predictionEmail.smtpPassword,
  }
  return predictionPayload(persistedConfig.value, values, formValue.value.openAI.aiConfigs)
}

function getPredictionEmailConfigError(requireConfig = formValue.value.predictionEmail.enabled, requireSlots = requireConfig) {
  if (!requireConfig) return ''
  const email = formValue.value.predictionEmail
  if (requireSlots && (!Array.isArray(email.slots) || email.slots.length === 0)) return '开启股票预测自动邮件时，请至少选择一个时间段'
  if (!String(email.to || '').trim()) return '请填写股票预测报告收件人'
  if (!String(email.smtpHost || '').trim()) return '请填写股票预测 SMTP 主机'
  if (!Number.isInteger(email.smtpPort) || email.smtpPort < 1 || email.smtpPort > 65535) return '股票预测 SMTP 端口必须在 1 到 65535 之间'
  if (!String(email.smtpUsername || '').trim()) return '请填写股票预测 SMTP 用户名'
  if (!String(email.smtpPassword || '').trim()) return '请填写股票预测 SMTP 授权码'
  return ''
}

function getMinuteSourceConfigError() {
  if (formValue.value.privateMinute.enabled && !String(formValue.value.privateMinute.baseUrl || '').trim()) return '已启用私人分钟线接口，请填写调用 URL'
  if (formValue.value.privateMinute.enabled && !String(formValue.value.privateMinute.apiKey || '').trim()) return '已启用私人分钟线接口，请填写 API Key'
  const publicEnabled = formValue.value.akshareEnabled || formValue.value.sinaMinuteEnabled || formValue.value.tencentMinuteEnabled
  const privateOneMinuteEnabled = formValue.value.privateMinute.enabled && formValue.value.privateMinute.level === '1min'
  return publicEnabled || privateOneMinuteEnabled ? '' : '至少启用一个适用于 1 分钟图的数据接口'
}

function formatSaveTime(date = new Date()) {
  const pad = value => String(value).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

async function runPersist({notifyError = false} = {}) {
  if (!settingsLoaded.value || autoSaveState.value === 'conflict') return false
  const version = pageVersion
  const validationError = getMinuteSourceConfigError() || getPredictionEmailConfigError()
  autoSaveError.value = ''
  if (validationError) {
    autoSaveState.value = 'error'
    autoSaveError.value = validationError
    if (notifyError) message.error(validationError)
    return false
  }
  const submittedRows = [...formValue.value.openAI.aiConfigs]
  const payload = buildConfigPayload()
  try {
    const result = await UpdatePredictionConfig(payload)
    if (version !== pageVersion) return false
    acceptSavedModelIDs(formValue.value.openAI.aiConfigs, submittedRows, payload.aiConfigs, result.aiConfigs)
    persistedConfig.value = result
    autoSaveState.value = 'saved'
    autoSaveLastSavedAt.value = formatSaveTime()
    return true
  } catch (error) {
    if (version !== pageVersion) return false
    autoSaveState.value = error?.status === 409 ? 'conflict' : 'error'
    autoSaveError.value = error?.status === 409
      ? '股票预测设置已在其他页面更新。当前草稿已保留；重新加载会放弃草稿并读取最新设置。'
      : error?.message || String(error || '保存失败')
    if (notifyError) message.error(autoSaveError.value)
    return false
  }
}

function queueAutoSave(options = {}) {
  if (!settingsLoaded.value || autoSaveState.value === 'conflict') return Promise.resolve(false)
  queuedAutoSave = true
  if (activeSavePromise) return activeSavePromise
  const version = pageVersion
  const promise = (async () => {
    let saved = true
    while (queuedAutoSave && version === pageVersion) {
      queuedAutoSave = false
      autoSaveState.value = 'saving'
      saved = await runPersist(options)
      if (!saved) break
    }
    return saved
  })().finally(() => { if (activeSavePromise === promise) activeSavePromise = null })
  activeSavePromise = promise
  return promise
}

function saveCurrentConfig(options = {}) {
  return queueAutoSave(options)
}

function saveGlobalSettings(field) {
  if (!settingsLoaded.value) return
  const payload = {[field]: formValue.value[field]}
  globalSavePromise = globalSavePromise.then(async () => {
    const result = await UpdateConfig(payload)
    if (String(result || '').includes('失败')) throw new Error(result)
    EventsEmit('updateSettings')
  }).catch(error => { message.error(`通用设置保存失败：${error?.message || error}`) })
}

async function testPredictionEmail() {
  const validationError = getPredictionEmailConfigError(true, false)
  if (validationError) {
    message.error(validationError)
    return
  }
  predictionEmailTesting.value = true
  try {
    const email = formValue.value.predictionEmail
    const result = await TestPredictionEmail({
      to: email.to,
      from: email.from,
      smtpHost: email.smtpHost,
      smtpPort: email.smtpPort,
      smtpUsername: email.smtpUsername,
      smtpPassword: email.smtpPassword,
    })
    message.success(result || '股票预测测试邮件发送成功')
  } catch (error) {
    message.error(`测试邮件发送失败：${error?.message || error}`)
  } finally {
    predictionEmailTesting.value = false
  }
}

function handleImmediateFieldChange() { queueAutoSave() }
function handleTextFieldBlur() { queueAutoSave() }

function exportConfig() {
  if (!settingsLoaded.value) return
  const payload = {product: 'Stock God', ...buildConfigPayload()}
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'}))
  const link = document.createElement('a')
  link.href = url
  link.download = 'stock-god-prediction-settings.json'
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

function importConfig() {
  if (!settingsLoaded.value || autoSaveState.value === 'conflict') return
  const version = pageVersion
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.onchange = event => {
    const file = event.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = loadEvent => {
      if (version !== pageVersion) return
      try {
        const imported = importPredictionSettings(buildConfigPayload(), JSON.parse(loadEvent.target.result))
        draftConfig.value = imported.config
        applyConfigToForm({...imported.config, aiConfigs: imported.aiConfigs})
        queueAutoSave()
      } catch (error) {
        message.error(`配置文件无效：${error?.message || error}`)
      }
    }
    reader.readAsText(file)
  }
  input.click()
}

async function loadSettings() {
  const version = ++pageVersion
  settingsLoaded.value = false
  queuedAutoSave = false
  activeSavePromise = null
  autoSaveState.value = 'idle'
  autoSaveError.value = ''
  aiConfigTestStates.value = {}
  formValue.value.openAI.aiConfigs = []
  try {
    const [global, center] = await Promise.all([GetConfig(), GetPredictionConfig()])
    if (version !== pageVersion) return
    persistedConfig.value = center
    draftConfig.value = {...center.config}
    applyConfigToForm({...center.config, aiConfigs: center.aiConfigs})
    for (const key of ['darkTheme', 'updateBasicInfoOnStart', 'refreshInterval']) formValue.value[key] = global[key]
    settingsLoaded.value = true
  } catch (error) {
    if (version === pageVersion) message.error(`读取设置失败：${error?.message || error}`)
  }
}

onMounted(loadSettings)
onBeforeUnmount(() => {
  pageVersion++
  queuedAutoSave = false
  message.destroyAll()
})
</script>

<template>
  <n-flex justify="left" style="text-align: left">
    <n-form ref="formRef" :disabled="!settingsLoaded" label-placement="left" label-align="left" style="width: 100%">
      <n-space vertical size="large">
        <n-alert type="info" :show-icon="false">
          股票预测设置。策略参数从下一轮任务生效；关闭自动策略后停止新分析和新买入，当前分析可完成报告，已有持仓继续按退出规则管理。
        </n-alert>
        <n-card :title="() => h(NTag, {type: 'primary', bordered: false}, () => '通用设置')" size="small">
          <n-grid :cols="24" :x-gap="24">
            <n-form-item-gi :span="8" label="暗黑主题：" path="darkTheme">
              <n-switch v-model:value="formValue.darkTheme" @update:value="saveGlobalSettings('darkTheme')"/>
            </n-form-item-gi>
            <n-form-item-gi :span="8" label="启动时更新基础信息：" path="updateBasicInfoOnStart">
              <n-switch v-model:value="formValue.updateBasicInfoOnStart" @update:value="saveGlobalSettings('updateBasicInfoOnStart')"/>
            </n-form-item-gi>
            <n-form-item-gi :span="8" label="数据刷新间隔：" path="refreshInterval">
              <n-input-number v-model:value="formValue.refreshInterval" :min="1" @update:value="saveGlobalSettings('refreshInterval')">
                <template #suffix>秒</template>
              </n-input-number>
            </n-form-item-gi>
          </n-grid>
          <n-text depth="3">通用设置独立保存。</n-text>
        </n-card>

        <n-card :title="() => h(NTag, {type: 'primary', bordered: false}, () => '数据接口设置')" size="small">
          <n-grid :cols="24" :x-gap="24">
            <n-form-item-gi :span="12" label="Tushare Token：" path="tushareToken">
              <n-input v-model:value="formValue.tushareToken" type="password" show-password-on="click"
                       placeholder="Tushare API Token" clearable @blur="handleTextFieldBlur"/>
            </n-form-item-gi>
            <n-form-item-gi :span="12" label="东财唯一标识：" path="qgqpBId">
              <n-input v-model:value="formValue.qgqpBId" placeholder="东财唯一标识" clearable @blur="handleTextFieldBlur"/>
            </n-form-item-gi>
            <n-form-item-gi :span="8" label="长历史提示：" path="minuteLongHistoryHintEnabled">
              <n-switch v-model:value="formValue.minuteLongHistoryHintEnabled" @update:value="handleImmediateFieldChange"/>
            </n-form-item-gi>
            <n-gi :span="24">
              <n-divider title-placement="left">分钟线数据接口</n-divider>
              <n-alert type="info" :show-icon="false" style="margin-bottom:12px">
                此处来源排序只用于图表。股票预测分析和成交分钟链继续按腾讯、东方财富、本地缓存的固定顺序运行。
              </n-alert>
              <MinuteProviderSettings
                  :form-value="formValue"
                  :akshare-minute-source-options="akshareMinuteSourceOptions"
                  :private-minute-proxy-mode-options="privateMinuteProxyModeOptions"
                  :private-minute-level-options="privateMinuteLevelOptions"
                  @immediate-change="handleImmediateFieldChange"
                  @text-blur="handleTextFieldBlur"
                  @move-provider="handleProviderMove"/>
            </n-gi>
          </n-grid>
        </n-card>

        <n-card title="MeoZ 竞价数据" size="small">
          <n-form-item label="Secret key：" path="meozApiKey">
            <n-input v-model:value="formValue.meozApiKey" type="password" show-password-on="click" autocomplete="off" placeholder="输入 MeoZ secret key" @blur="handleTextFieldBlur"/>
          </n-form-item>
          <n-text depth="3">保存密钥后仍须通过竞价数据核验；鉴权失败、数据未核验或不完整时不执行新买入。</n-text>
        </n-card>
        <n-card title="BASE43 策略" size="small">
          <n-form-item label="自动策略：" path="predictionAutoEnabled">
            <n-switch v-model:value="formValue.predictionAutoEnabled" @update:value="handleImmediateFieldChange"/>
          </n-form-item>
          <n-text>每日滚动训练，原 43 特征与五模型均值；固定最多两只。竞价来源通过核验后才执行选股。关闭自动策略停止新选股与买入，已有持仓继续按退出规则管理。</n-text>
        </n-card>

        <n-card :title="() => h(NTag, {type: 'primary', bordered: false}, () => '股票预测报告邮件')" size="small">
          <n-grid :cols="24" :x-gap="24">
            <n-form-item-gi :span="24" label="自动发送报告：" path="predictionEmail.enabled">
              <n-switch v-model:value="formValue.predictionEmail.enabled" @update:value="handleImmediateFieldChange"/>
              <n-text depth="3" style="margin-left:12px">选中分区的首份有效报告落库后立即单独发送；失败后最多重试3次。</n-text>
            </n-form-item-gi>
            <n-form-item-gi :span="24" label="发送时间段：" path="predictionEmail.slots">
              <n-select v-model:value="formValue.predictionEmail.slots" multiple filterable clearable
                        max-tag-count="responsive" :options="predictionEmailSlotOptions"
                        placeholder="请选择一个或多个实际落盘时间段" @update:value="handleImmediateFieldChange"/>
            </n-form-item-gi>
            <n-form-item-gi :span="12" label="收件人：" path="predictionEmail.to">
              <n-input v-model:value="formValue.predictionEmail.to" type="textarea" :autosize="{minRows:2,maxRows:4}"
                       placeholder="多个地址可用逗号、分号或换行分隔" @blur="handleTextFieldBlur"/>
            </n-form-item-gi>
            <n-form-item-gi :span="12" label="发件人：" path="predictionEmail.from">
              <n-input v-model:value="formValue.predictionEmail.from" placeholder="留空时使用 SMTP 用户名" @blur="handleTextFieldBlur"/>
            </n-form-item-gi>
            <n-form-item-gi :span="10" label="SMTP 主机：" path="predictionEmail.smtpHost">
              <n-input v-model:value="formValue.predictionEmail.smtpHost" placeholder="smtp.example.com" @blur="handleTextFieldBlur"/>
            </n-form-item-gi>
            <n-form-item-gi :span="4" label="端口：" path="predictionEmail.smtpPort">
              <n-input-number v-model:value="formValue.predictionEmail.smtpPort" :min="1" :max="65535" @update:value="handleImmediateFieldChange"/>
            </n-form-item-gi>
            <n-form-item-gi :span="10" label="SMTP 用户名：" path="predictionEmail.smtpUsername">
              <n-input v-model:value="formValue.predictionEmail.smtpUsername" @blur="handleTextFieldBlur"/>
            </n-form-item-gi>
            <n-form-item-gi :span="12" label="SMTP 授权码：" path="predictionEmail.smtpPassword">
              <n-input v-model:value="formValue.predictionEmail.smtpPassword" type="password" show-password-on="click" @blur="handleTextFieldBlur"/>
            </n-form-item-gi>
            <n-form-item-gi :span="12">
              <n-button type="primary" secondary :loading="predictionEmailTesting" @click="testPredictionEmail">发送测试邮件</n-button>
            </n-form-item-gi>
            <n-gi :span="24"><n-alert type="info" :show-icon="false">按报告实际落盘分区判断，多个分区分别发送；未生成报告的分区不发送。测试邮件只验证 SMTP，不要求选择时间段，也不创建研究记录或模拟交易。</n-alert></n-gi>
          </n-grid>
        </n-card>

        <n-card :title="() => h(NTag, {type: 'primary', bordered: false}, () => '配置管理')" size="small">
          <n-space vertical align="center">
            <n-space>
              <n-button type="info" :disabled="!settingsLoaded" @click="exportConfig">导出股票预测配置</n-button>
              <n-button type="warning" :disabled="!settingsLoaded || autoSaveState === 'conflict'" @click="importConfig">导入股票预测配置</n-button>
            </n-space>
            <n-text type="error">导出的 JSON 包含完整明文 API Key、Token 和 SMTP 授权码，请仅保存在可信设备。</n-text>
            <n-text depth="3" v-if="autoSaveState === 'saving'">正在自动保存...</n-text>
            <n-text type="success" v-else-if="autoSaveState === 'saved'">已自动保存 {{ autoSaveLastSavedAt }}</n-text>
            <n-text type="error" v-else-if="autoSaveState === 'error'">自动保存失败：{{ autoSaveError }}</n-text>
            <template v-else-if="autoSaveState === 'conflict'">
              <n-text type="warning">{{ autoSaveError }}</n-text>
              <n-button type="warning" @click="loadSettings">放弃草稿并重新加载</n-button>
            </template>
          </n-space>
        </n-card>
      </n-space>
    </n-form>
  </n-flex>
</template>
