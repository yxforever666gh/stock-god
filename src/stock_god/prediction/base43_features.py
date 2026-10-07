"""Pure as-of features: accepts T tape and S pre-open auction, never S tape."""

import numpy as np

from .base43_auction import NAMES as AUCTION_NAMES

GRID = np.r_[570, np.arange(571, 691), np.arange(781, 901)]
HISTORY = [
    "prior_return5_pct",
    "prior_return20_pct",
    "prior_volatility5",
    "prior_volatility20",
    "prior_volume5_over20",
    "prior_sealed_fraction5",
    "prior_sealed_fraction20",
    "prior_touched_fraction5",
    "prior_touched_fraction20",
]

DAY_NAMES = [
    "T_sealed",
    "T_unsealed",
    "T_untouched",
    "T_label_missing",
    "T_close_gain_pct",
    "T_open_to_close_pct",
    "T_buy_to_close_pct",
    "T_amplitude_pct",
    "T_close_location",
    "T_high_to_close_drawdown_pct",
    "T_max_observed_drawdown_pct",
    "T_first_hour_return_pct",
    "T_morning_return_pct",
    "T_afternoon_return_pct",
    "T_last60_return_pct",
    "T_last30_return_pct",
    "T_last15_return_pct",
    "T_log_volume",
    "T_first_hour_volume_fraction",
    "T_afternoon_volume_fraction",
    "T_last30_volume_fraction",
    "T_last15_volume_fraction",
    "T_first_touch_bar_end_minute",
    "T_touched_bar_fraction",
    "T_touched_close_gap_pct",
    "T_last_touch_bar_end_minute",
    "log_quantity",
    "log_buy_price",
    "quantity_over_T_volume",
    "T_path_missing",
    "history_missing_fraction",
]
BASE_NAMES = list(HISTORY) + DAY_NAMES
NEXT_NAMES = ["S_" + s for s in AUCTION_NAMES] + [
    "S_auction_vs_T_close_pct",
    "S_auction_vs_T_high_pct",
    "S_auction_vs_T_upper_pct",
    "quantity_over_S_auction_volume",
    "S_auction_final_missing",
    "S_auction_path_missing_fraction",
]
ALL_NAMES = BASE_NAMES + NEXT_NAMES
FAMILIES = {"T_only": BASE_NAMES, "auction": ALL_NAMES}


def divide(a, b):
    a, b = np.broadcast_arrays(np.asarray(a, float), np.asarray(b, float))
    return np.divide(a, b, out=np.full(a.shape, np.nan), where=np.isfinite(b) & (b > 0))


def _tape_build(T_prices, T_volumes, T_meta, history, next_auction, next_evidence, buy, qty):
    p = np.asarray(T_prices, float)
    v = np.asarray(T_volumes, float)
    history = np.asarray(history, float)
    a = np.asarray(next_auction, float).copy()
    e = np.asarray(next_evidence, float)
    buy, qty = np.asarray(buy, float), np.asarray(qty, float)
    n = len(buy)
    if p.shape != (n, 241, 4) or v.shape != (n, 241) or history.shape != (n, len(HISTORY)):
        raise ValueError("Unaligned V9 feature inputs")
    continuous = p[:, 1:]
    good_bar = (
        (continuous > 0).all(axis=2)
        & np.isfinite(v[:, 1:])
        & (v[:, 1:] >= 0)
        & (continuous[:, :, 1] >= np.maximum(continuous[:, :, 0], continuous[:, :, 3]))
        & (continuous[:, :, 2] <= np.minimum(continuous[:, :, 0], continuous[:, :, 3]))
    )
    complete = good_bar.all(axis=1)
    label = T_meta["label"]
    ref, upper = T_meta["ref"].astype(float), T_meta["upper"].astype(float)
    close, opening = p[:, -1, 3], p[:, 1, 0]
    critical = good_bar[:, -1] & (buy > 0) & (qty > 0) & np.isin(label, [0, 1, 2])
    high = np.where(complete, continuous[:, :, 1].max(axis=1), np.nan)
    low = np.where(complete, continuous[:, :, 2].min(axis=1), np.nan)
    volume = np.where(complete, v[:, 1:].sum(axis=1), np.nan)
    peak = np.maximum.accumulate(continuous[:, :, 1], axis=1)
    drawdown = np.where(complete, (divide(peak - continuous[:, :, 3], peak) * 100).max(axis=1), np.nan)
    hit = good_bar & (continuous[:, :, 1] >= upper[:, None]) & (upper[:, None] > 0)
    any_hit = hit.any(axis=1)
    first = np.where(complete & any_hit, GRID[1:][hit.argmax(axis=1)], np.nan)
    last = np.where(complete & any_hit, GRID[1:][239 - hit[:, ::-1].argmax(axis=1)], np.nan)
    values = {}
    for i, name in enumerate(HISTORY):
        values[name] = history[:, i]
    for k, name in enumerate(("T_sealed", "T_unsealed", "T_untouched")):
        values[name] = (label == k).astype(float)
    values.update(
        T_label_missing=(~np.isin(label, [0, 1, 2])).astype(float),
        T_close_gain_pct=(divide(close, ref) - 1) * 100,
        T_open_to_close_pct=np.where(complete, (divide(close, opening) - 1) * 100, np.nan),
        T_buy_to_close_pct=(divide(close, buy) - 1) * 100,
        T_amplitude_pct=divide(high - low, ref) * 100,
        T_close_location=np.where(high == low, 0.5, divide(close - low, high - low)),
        T_high_to_close_drawdown_pct=divide(high - close, high) * 100,
        T_max_observed_drawdown_pct=drawdown,
        T_first_hour_return_pct=np.where(
            good_bar[:, :60].all(axis=1), (divide(p[:, 60, 3], opening) - 1) * 100, np.nan
        ),
        T_morning_return_pct=np.where(
            good_bar[:, :120].all(axis=1), (divide(p[:, 120, 3], opening) - 1) * 100, np.nan
        ),
        T_afternoon_return_pct=np.where(
            good_bar[:, 120:].all(axis=1), (divide(close, p[:, 121, 0]) - 1) * 100, np.nan
        ),
        T_last60_return_pct=np.where(
            good_bar[:, 179:].all(axis=1), (divide(close, p[:, 180, 3]) - 1) * 100, np.nan
        ),
        T_last30_return_pct=np.where(
            good_bar[:, 209:].all(axis=1), (divide(close, p[:, 210, 3]) - 1) * 100, np.nan
        ),
        T_last15_return_pct=np.where(
            good_bar[:, 224:].all(axis=1), (divide(close, p[:, 225, 3]) - 1) * 100, np.nan
        ),
        T_log_volume=np.log1p(volume),
        T_first_hour_volume_fraction=divide(v[:, 1:61].sum(axis=1), volume),
        T_afternoon_volume_fraction=divide(v[:, 121:].sum(axis=1), volume),
        T_last30_volume_fraction=divide(v[:, -30:].sum(axis=1), volume),
        T_last15_volume_fraction=divide(v[:, -15:].sum(axis=1), volume),
        T_first_touch_bar_end_minute=first,
        T_touched_bar_fraction=np.where(complete, hit.mean(axis=1), np.nan),
        T_touched_close_gap_pct=np.where(complete & any_hit, (divide(close, upper) - 1) * 100, np.nan),
        T_last_touch_bar_end_minute=last,
        log_quantity=np.log1p(qty),
        log_buy_price=np.log1p(buy / 100),
        quantity_over_T_volume=divide(qty, volume),
        T_path_missing=(~complete).astype(float),
        history_missing_fraction=(~np.isfinite(history)).mean(axis=1),
    )
    auction_good = (
        np.isfinite(e[:, 1])
        & (e[:, 1] > 0)
        & np.isfinite(e[:, 4])
        & (e[:, 4] >= 33900)
        & (e[:, 4] < 34200)
        & (e[:, 7] == 0)
        & np.isfinite(a[:, 0])
    )
    a[~auction_good] = np.nan
    for i, name in enumerate(AUCTION_NAMES):
        values["S_" + name] = a[:, i]
    final = np.where(auction_good, e[:, 1] * 100, np.nan)
    values.update(
        S_auction_vs_T_close_pct=(divide(final, close) - 1) * 100,
        S_auction_vs_T_high_pct=(divide(final, high) - 1) * 100,
        S_auction_vs_T_upper_pct=(divide(final, upper) - 1) * 100,
        quantity_over_S_auction_volume=np.where(auction_good, divide(qty, e[:, 2]), np.nan),
        S_auction_final_missing=(~auction_good).astype(float),
        S_auction_path_missing_fraction=(~np.isfinite(a[:, 7:])).mean(axis=1),
    )
    x = np.column_stack([values[name] for name in ALL_NAMES]).astype(np.float32)
    x[~np.isfinite(x)] = np.nan
    return x, critical, auction_good


PREV_NAMES = [
    "prev_sealed",
    "prev_unsealed",
    "prev_label_missing",
    "prev_close_gain_pct",
    "prev_open_to_close_pct",
    "prev_amplitude_pct",
    "prev_close_location",
    "prev_high_to_close_drawdown_pct",
    "prev_max_observed_drawdown_pct",
    "prev_first_hour_return_pct",
    "prev_afternoon_return_pct",
    "prev_last30_return_pct",
    "prev_last15_return_pct",
    "prev_log_volume",
    "prev_first_hour_volume_fraction",
    "prev_afternoon_volume_fraction",
    "prev_last30_volume_fraction",
    "prev_first_touch_bar_end_minute",
    "prev_touched_bar_fraction",
    "prev_path_missing",
]
_SOURCE_COLUMNS = [ALL_NAMES.index(name.replace("prev_", "T_", 1)) for name in PREV_NAMES]
_COLUMN = {name: index for index, name in enumerate(PREV_NAMES)}


def build_previous_features(cache, di: int, sids) -> np.ndarray:
    """Return float32 (N, 20), reading only prices/volumes/meta at ``di - 1``.

    ``di`` is the prediction day's calendar index.  Its immediate previous
    calendar session is used even when a stock did not trade; no forward or
    backward filling across sessions occurs.  The first calendar day returns
    unknown values plus missing flags instead of indexing the array at -1.
    """
    stocks = np.asarray(sids)
    if stocks.ndim != 1 or (stocks.size and not np.issubdtype(stocks.dtype, np.integer)):
        raise ValueError("Previous-day feature stock ids must be an integer vector")
    stocks = stocks.astype(np.int64, copy=False)
    shape = cache["prices"].shape
    if (
        len(shape) != 4
        or shape[2:] != (241, 4)
        or cache["volumes"].shape != shape[:3]
        or cache["meta"].shape != shape[:2]
    ):
        raise ValueError("Previous-day cache prices, volumes and metadata do not align")
    if not isinstance(di, (int, np.integer)) or not 0 <= int(di) < shape[1]:
        raise ValueError("Prediction date index is outside the cache calendar")
    if np.any((stocks < 0) | (stocks >= shape[0])):
        raise ValueError("Previous-day stock index is outside the cache universe")
    result = np.full((len(stocks), len(PREV_NAMES)), np.nan, np.float32)
    if not len(stocks):
        return result
    if int(di) == 0:
        result[:, _COLUMN["prev_label_missing"]] = 1.0
        result[:, _COLUMN["prev_path_missing"]] = 1.0
        return result

    prior = int(di) - 1
    prices = np.asarray(cache["prices"][stocks, prior])
    volumes = np.asarray(cache["volumes"][stocks, prior])
    meta = np.asarray(cache["meta"][stocks, prior])
    n = len(stocks)
    full, _, _ = _tape_build(
        prices,
        volumes,
        meta,
        np.full((n, len(HISTORY)), np.nan, np.float32),
        np.full((n, len(AUCTION_NAMES)), np.nan, np.float32),
        np.full((n, 8), np.nan, np.float32),
        np.ones(n, np.int32),
        np.ones(n, np.int32),
    )
    result[:] = full[:, _SOURCE_COLUMNS]

    # V9's missing label is represented by all-zero class flags.  An opening
    # model must distinguish an unknown previous outcome from known untouched.
    last = prices[:, -1]
    last_good = (
        (last > 0).all(axis=1)
        & np.isfinite(volumes[:, -1])
        & (volumes[:, -1] >= 0)
        & (last[:, 1] >= np.maximum(last[:, 0], last[:, 3]))
        & (last[:, 2] <= np.minimum(last[:, 0], last[:, 3]))
    )
    known_label = np.isin(meta["label"], (0, 1, 2)) & last_good
    result[~known_label, _COLUMN["prev_sealed"]] = np.nan
    result[~known_label, _COLUMN["prev_unsealed"]] = np.nan
    result[:, _COLUMN["prev_label_missing"]] = (~known_label).astype(np.float32)
    # V9's close/reference helper assumes its original critical-row gate.
    # Here rows are retained, so a bad last quote must not become a -100% gain.
    result[~last_good | (meta["ref"] <= 0), _COLUMN["prev_close_gain_pct"]] = np.nan
    result[~np.isfinite(result)] = np.nan
    return result
