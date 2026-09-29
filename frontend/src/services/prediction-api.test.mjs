import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const moduleURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const pathsSource = await readFile(new URL('./api-types.generated.ts', import.meta.url), 'utf8')
const pathsURL = moduleURL(ts.transpileModule(pathsSource, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText)
const http = moduleURL(`
export const requestJSON = async (path, options = {}) => ({path, ...options});
export const command = requestJSON;
export const withPath = (path, values) => path.replace(/\\{(\\w+)\\}/g, (_, key) => encodeURIComponent(String(values[key])));
export const withQuery = (path, values) => {const query = new URLSearchParams(Object.entries(values).filter(([, value]) => value !== '' && value !== undefined).map(([key, value]) => [key, String(value)])); return query.size ? path + '?' + query : path};
`)
async function service(filename) {
  const source = await readFile(new URL(filename, import.meta.url), 'utf8')
  const script = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText
    .replace(/from ['"]\.\/api-types.generated['"]/g, `from '${pathsURL}'`)
    .replace(/from ['"]\.\/http-client['"]/g, `from '${http}'`)
    .replace(/from ['"]\.\/http-error.js['"]/g, `from '${new URL('./http-error.js', import.meta.url).href}'`)
  return import(moduleURL(script))
}

test('prediction data, chart, settings and replay calls use the new routes and preserve request values', async () => {
  const prediction = await service('./prediction-api.ts')
  const account = await prediction.GetPredictionAccount('10:05')
  assert.equal(account.path, '/api/v1/prediction/account?slot=10%3A05')
  const list = await prediction.ListPredictionPerformanceRecommendations(200, 400, ['09:30', '11:25'], '2026-09-01', '2026-09-25')
  const query = new URL(list.path, 'http://localhost').searchParams
  assert.equal(query.get('offset'), '400')
  assert.equal(query.get('slots'), '09:30,11:25')
  assert.equal(query.get('boughtOnly'), 'true')
  assert.equal(query.get('from'), '2026-09-01')
  assert.equal(query.get('to'), '2026-09-25')
  const reportPage = await prediction.BrowsePredictionRuns(2, '2026-09-29', false)
  const reportQuery = new URL(reportPage.path, 'http://localhost').searchParams
  assert.equal(reportQuery.get('page'), '2')
  assert.equal(reportQuery.get('day'), '2026-09-29')
  assert.equal(reportQuery.get('allReports'), 'false')
  assert.deepEqual(await prediction.RefreshPredictionRecommendationChart('id/a'), {path: '/api/v1/prediction/recommendations/id%2Fa/chart/refresh', method: 'POST'})

  const settings = await service('./settings-api.js')
  const payload = {revision: 4, config: {predictionAutoEnabled: false}, aiConfigs: []}
  assert.deepEqual(await settings.UpdatePredictionConfig(payload), {path: '/api/v1/prediction/settings', method: 'PUT', body: payload})
  assert.deepEqual(await settings.TestAIConfig(12), {path: '/api/v1/prediction/ai/configs/test', method: 'POST', body: {id: 12}})

  const audit = await service('./prediction-audit-api.ts')
  assert.deepEqual(await audit.ListPredictionReplayModelConfigs(), {path: '/api/v1/prediction/ai/configs'})
  assert.deepEqual(await audit.GetPredictionRunAudit('run-1'), {path: '/api/v1/prediction/analysis-runs/run-1/audit'})
  assert.deepEqual(await audit.CreatePredictionReplay('run-1', 12), {path: '/api/v1/prediction/replays', method: 'POST', body: {sourceOwnerId: 'run-1', modelConfigId: 12}})
  assert.deepEqual(await audit.GetPredictionReplay('replay-1'), {path: '/api/v1/prediction/replays/replay-1'})
})

test('published route constants contain no removed product APIs', async () => {
  const {API_PATHS} = await import(pathsURL)
  for (const path of Object.values(API_PATHS)) assert.doesNotMatch(path, /\/api\/v1\/(research(?:2|-centers)?|watchlist|groups|knowledge)(?:\/|$)/)
})
