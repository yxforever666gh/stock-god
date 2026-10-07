import test from 'node:test'
import assert from 'node:assert/strict'
import {ARCHIVED_PREDICTION_SLOTS, PREDICTION_SLOTS, predictionSlotBuyLabel, validPredictionSlot} from './prediction-slots.js'

test('morning accounts have 24 exclusive five-minute identities', () => {
  assert.equal(PREDICTION_SLOTS.length, 1)
  assert.equal(new Set(ARCHIVED_PREDICTION_SLOTS.map(slot => slot.value)).size, 24)
  assert.deepEqual(ARCHIVED_PREDICTION_SLOTS[0], {value: '09:30', label: '09:30–09:35'})
  assert.deepEqual(ARCHIVED_PREDICTION_SLOTS.at(-1), {value: '11:25', label: '11:25–11:30'})
  assert.equal(validPredictionSlot('base43'), true)
  assert.equal(predictionSlotBuyLabel({slot: 'base43', auctionSourceConfigured: false}), '竞价 API 未配置，未执行选股')
  assert.equal(predictionSlotBuyLabel({status: 'archived'}), '归档账户，只读')
  assert.equal(validPredictionSlot('11:30'), false)
  assert.equal(validPredictionSlot('09:50'), true)
})

test('slot labels show only buy execution', () => {
  assert.equal(predictionSlotBuyLabel({buyStatus: 'bought_full', boughtCount: 5, buyTargetCount: 5}), '买入：已买入 5/5')
  assert.equal(predictionSlotBuyLabel({buyStatus: 'bought_partial', boughtCount: 3, buyTargetCount: 5}), '买入：已买入 3/5，本轮结束')
  assert.equal(predictionSlotBuyLabel({buyStatus: 'awaiting_quote', pendingBuyCount: 2}), '买入：等待行情（2 笔）')
  assert.equal(predictionSlotBuyLabel({buyStatus: 'cutoff'}), '买入：窗口已截止')
  assert.equal(predictionSlotBuyLabel(), '买入：等待报告')
})

 test('MeoZ state blocks buys until authenticated and verified', () => {
  for (const status of ['unconfigured', 'unauthorized', 'unverified', 'incomplete', 'error']) {
    assert.match(predictionSlotBuyLabel({slot: 'base43', auctionSourceConfigured: true, auctionSourceStatus: status}), /未执行选股/)
  }
  assert.equal(predictionSlotBuyLabel({slot: 'base43', auctionSourceConfigured: true, auctionSourceStatus: 'ready', buyStatus: 'awaiting_quote'}), '买入：等待行情')
  assert.equal(predictionSlotBuyLabel({slot: 'base43', auctionSourceStatus: 'error', auctionSourceMessage: '提供者暂不可用'}), '提供者暂不可用')
})
