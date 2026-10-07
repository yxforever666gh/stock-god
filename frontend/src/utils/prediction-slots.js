export const ARCHIVED_PREDICTION_SLOTS = Array.from({length: 24}, (_, index) => {
  const minute = 570 + index * 5
  const clock = value => `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`
  return {value: clock(minute), label: `${clock(minute)}–${clock(minute + 5)}`}
})

export const PREDICTION_SLOTS = [{value: 'base43', label: 'BASE43'}]
export const validPredictionSlot = value => [...PREDICTION_SLOTS, ...ARCHIVED_PREDICTION_SLOTS].some(slot => slot.value === value)

export const auctionSourceLabels = {unconfigured: '竞价 API 未配置，未执行选股', unauthorized: '竞价 API 鉴权失败，未执行选股', unverified: '竞价数据尚未核验，未执行选股', ready: '竞价数据已核验', incomplete: '竞价数据不完整，未执行选股', error: '竞价数据获取失败，未执行选股'}

const nonNegativeInteger = value => Math.max(0, Number.parseInt(value, 10) || 0)

export function predictionSlotBuyLabel(state = {}) {
  const bought = nonNegativeInteger(state?.boughtCount)
  const target = nonNegativeInteger(state?.buyTargetCount)
  const pending = nonNegativeInteger(state?.pendingBuyCount)
  if (state?.archivedAt || state?.status === 'archived') return '归档账户，只读'
  if (state?.slot === 'base43' && (state?.auctionSourceStatus !== 'ready' || !state?.auctionSourceConfigured)) return state?.auctionSourceMessage || auctionSourceLabels[state?.auctionSourceStatus] || auctionSourceLabels.unconfigured
  switch (state?.buyStatus) {
    case 'bought_full':
      return `买入：已买入 ${bought}/${target}`
    case 'bought_partial':
      return `买入：已买入 ${bought}/${target}，本轮结束`
    case 'awaiting_quote':
      return `买入：等待行情${pending > 0 ? `（${pending} 笔）` : ''}`
    case 'no_recommendation':
      return '买入：本轮无标的'
    case 'cutoff':
      return '买入：窗口已截止'
    case 'disabled':
      return '买入：新增买入已关闭'
    case 'failed':
      return '买入：执行失败'
    case 'processing':
      return '买入：处理中'
    case 'no_purchase':
      return '买入：本轮未成交'
    default:
      return '买入：等待报告'
  }
}
