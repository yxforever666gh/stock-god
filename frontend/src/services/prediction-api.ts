import {requestJSON, withPath, withQuery} from './http-client'
import {API_PATHS} from './api-types.generated'
import type {RecommendationChart, PredictionAccountOverview, PredictionAnalysisRun, PredictionAnalysisRunBrowse, PredictionAnalysisRunSummary, PredictionPerformance, PredictionPortfolioPerformance, PredictionRecommendation, PredictionRecommendationDetail} from './api-types.generated'

export const ListPredictionRuns = (limit = 100, offset = 0, slot = ""): Promise<PredictionAnalysisRunSummary[]> => requestJSON(withQuery(API_PATHS.listPredictionAnalysisRuns, {limit, offset, slot}))
export const BrowsePredictionRuns = (page = 1, day = 'recent5', allReports = true): Promise<PredictionAnalysisRunBrowse> => requestJSON(withQuery(API_PATHS.browsePredictionAnalysisRuns, {page, day, allReports}))
export const GetPredictionRun = (id: string): Promise<PredictionAnalysisRun> => requestJSON(withPath(API_PATHS.getPredictionAnalysisRun, {id}))
export const ListPredictionRecommendations = (limit = 100, offset = 0, slot = "base43"): Promise<PredictionRecommendation[]> => requestJSON(withQuery(API_PATHS.listPredictionRecommendations, {limit, offset, slot}))
export const ListPredictionPerformanceRecommendations = (limit = 100, offset = 0, slots: string[] = [], from = "", to = ""): Promise<PredictionRecommendation[]> => requestJSON(withQuery(API_PATHS.listPredictionRecommendations, {limit, offset, slots: slots.join(','), from, to, boughtOnly: true}))
export const GetPredictionRecommendation = (id: string): Promise<PredictionRecommendationDetail> => requestJSON(withPath(API_PATHS.getPredictionRecommendation, {id}))
export const GetPredictionAccount = (slot = "base43"): Promise<PredictionAccountOverview> => requestJSON(withQuery(API_PATHS.getPredictionAccount, {slot}))
export const GetPredictionPerformance = (slot = "base43"): Promise<PredictionPerformance> => requestJSON(withQuery(API_PATHS.getPredictionPerformance, {slot}))
export const GetPredictionPortfolioPerformance = (slots: string[] = [], from = "", to = ""): Promise<PredictionPortfolioPerformance> => requestJSON(withQuery(API_PATHS.getPredictionPortfolioPerformance, {slots: slots.join(','), from, to}))
export const GetPredictionRecommendationChart = (id: string): Promise<RecommendationChart> => requestJSON(withPath(API_PATHS.getPredictionRecommendationChart, {id}))
export const RefreshPredictionRecommendationChart = (id: string): Promise<RecommendationChart> => requestJSON(withPath(API_PATHS.refreshPredictionRecommendationChart, {id}), {method: 'POST'})

export const ListPredictionSlots = () => requestJSON(API_PATHS.listPredictionSlots)
