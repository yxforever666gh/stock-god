"""Decode auction phases before aggregation; never interpret snapshots as orders."""

import numpy as np

FINAL = [
    "auction_gain_pct",
    "log_auction_volume",
    "log_auction_amount",
    "auction_volume_ratio20",
    "auction_final_unmatched_fraction",
    "auction_final_spread_bps",
    "auction_best_book_imbalance",
]
PATH = [
    "auction_drift_5m_pct",
    "auction_drift_1m_pct",
    "auction_drift_10s_pct",
    "auction_match_log_growth_5m",
    "auction_match_log_growth_1m",
    "auction_last_virtual_imbalance",
    "auction_imbalance_change_5m",
    "auction_imbalance_change_1m",
    "auction_positive_imbalance_time_fraction",
    "auction_virtual_range_5m_pct",
    "auction_virtual_drawdown_pct",
    "auction_early_peak_to_0920_pct",
    "auction_final_vs_virtual_pct",
]
NAMES = FINAL + PATH
FEATURE_COUNT = 20
# Numeric CSV column positions after timestamp: ref,current,volume,amount,cjcs,
# b1p,b1v,b2p,b2v,a1p,a1v,a2p,a2v,total_bid,average_bid,total_ask,average_ask.


def decode_pre(row):
    p = row[5]
    v = row[6]
    a = row[9]
    av = row[10]
    b2 = row[8]
    a2 = row[12]
    if not (
        np.isfinite(p)
        and p > 0
        and np.isfinite(a)
        and abs(p - a) < 0.00001
        and np.isfinite(v)
        and v >= 0
        and np.isfinite(av)
        and abs(v - av) < 0.001
    ):
        return np.nan, np.nan, np.nan
    imbalance = np.nan
    if np.isfinite(b2) and np.isfinite(a2) and b2 >= 0 and a2 >= 0 and min(b2, a2) == 0:
        u = b2 - a2
        den = 2 * v + abs(u)
        if den > 0:
            imbalance = u / den
    return p, v, imbalance


def summarize_group(times, values):
    """A single stock/date; final auction data must be available before09:30.

    Returns unnormalized features, evidence counters and the quoted reference.
    Repeated identical timestamps are ignored; conflicting ones invalidate the
    stock-day's new features, without changing its V4 candidate eligibility.
    """
    out = np.full(FEATURE_COUNT, np.nan)
    raw = np.full(8, np.nan)
    # raw: ref,finalprice,finalvolume,finalamount,finaltime,precount,maxgap,conflict
    checkpoints = np.full((3, 3), np.nan)
    ct = np.full(3, -1.0)
    targets = np.array([33600, 33840, 33890])  # 09:20,09:24,09:24:50
    late_p = np.nan
    late_v = np.nan
    late_i = np.nan
    late_t = -1
    first_late = -1
    count = 0
    gapmax = 0
    weighted = 0.0
    duration = 0.0
    peak = -np.inf
    low = np.inf
    early_peak = -np.inf
    conflict = False
    prior_time = -1
    previous = np.full(values.shape[1], np.nan)
    for j in range(len(times)):
        t = times[j]
        r = values[j]
        if t < 33300 or t >= 34200:
            continue
        if t == prior_time:
            equal = True
            for c in range(len(r)):
                if not ((np.isnan(r[c]) and np.isnan(previous[c])) or r[c] == previous[c]):
                    equal = False
            if not equal:
                conflict = True
            continue
        prior_time = t
        previous = r.copy()
        if np.isfinite(r[0]) and r[0] > 0:
            if np.isfinite(raw[0]) and abs(raw[0] - r[0]) > 0.00001:
                conflict = True
            raw[0] = r[0]
        if t < 33900:
            p, v, i = decode_pre(r)
            if not np.isfinite(p):
                continue
            if t < 33600:
                early_peak = max(early_peak, p)
            for c in range(3):
                if t <= targets[c]:
                    checkpoints[c, 0] = p
                    checkpoints[c, 1] = v
                    checkpoints[c, 2] = i
                    ct[c] = t
            if t >= 33600:
                if first_late < 0:
                    first_late = t
                if late_t >= 33600:
                    dt = t - late_t
                    gapmax = max(gapmax, dt)
                    if np.isfinite(late_i):
                        weighted += dt * (late_i > 0)
                        duration += dt
                count += 1
                peak = max(peak, p)
                low = min(low, p)
            late_p = p
            late_v = v
            late_i = i
            late_t = t
        elif not np.isfinite(raw[4]):
            p = r[1]
            v = r[2]
            a = r[3]
            if not (
                np.isfinite(p)
                and p > 0
                and np.isfinite(v)
                and v > 0
                and np.isfinite(a)
                and a > 0
                and abs(a / v - p) <= 0.011
            ):
                continue
            raw[1] = p
            raw[2] = v
            raw[3] = a
            raw[4] = t
            out[1] = np.log1p(v)
            out[2] = np.log1p(a)
            b = r[5]
            bv = r[6]
            s = r[9]
            sv = r[10]
            if (
                np.isfinite(b)
                and np.isfinite(s)
                and b > 0
                and s > b
                and np.isfinite(bv)
                and np.isfinite(sv)
                and bv >= 0
                and sv >= 0
            ):
                out[5] = (s - b) / p * 10000
                if bv + sv > 0:
                    out[6] = (bv - sv) / (bv + sv)
                u = np.nan
                if abs(p - b) < 0.00001:
                    u = bv
                elif abs(p - s) < 0.00001:
                    u = -sv
                if np.isfinite(u):
                    out[4] = u / (2 * v + abs(u))
    raw[5] = count
    raw[6] = gapmax
    raw[7] = 1.0 if conflict else 0.0
    if conflict:
        return np.full(FEATURE_COUNT, np.nan), raw
    if np.isfinite(raw[0]) and np.isfinite(raw[1]):
        out[0] = (raw[1] / raw[0] - 1) * 100
    recent = late_t >= 33870
    if recent:
        out[12] = late_i
        for c in range(3):
            age = targets[c] - ct[c]
            tol = 30 if c < 2 else 10
            if ct[c] >= 0 and age <= tol and np.isfinite(checkpoints[c, 0]):
                out[7 + c] = (late_p / checkpoints[c, 0] - 1) * 100
                if c < 2:
                    if checkpoints[c, 1] > 0 and late_v > 0:
                        out[10 + c] = np.log(late_v / checkpoints[c, 1])
                    if np.isfinite(late_i) and np.isfinite(checkpoints[c, 2]):
                        out[13 + c] = late_i - checkpoints[c, 2]
        if count >= 8 and first_late <= 33630 and gapmax <= 60 and duration >= 240:
            out[15] = weighted / duration
            if low > 0:
                out[16] = (peak / low - 1) * 100
            if peak > 0:
                out[17] = (late_p / peak - 1) * 100
        if np.isfinite(early_peak) and ct[0] >= 33570 and checkpoints[0, 0] > 0:
            out[18] = (checkpoints[0, 0] / early_peak - 1) * 100
        if np.isfinite(raw[1]):
            out[19] = (raw[1] / late_p - 1) * 100
    return out, raw
