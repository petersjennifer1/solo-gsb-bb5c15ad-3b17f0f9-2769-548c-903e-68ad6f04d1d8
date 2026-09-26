"""展柜扰动恢复分析（disturbance recovery，纯计算，只读）。

以**扰动事件时刻** ``event_ts`` 为界：

- **基线**：事件前窗口 ``[event_ts − baseline_seconds, event_ts]``（含端点）
  内读数的温度、湿度各自的**中位数**；窗口内样本数不足
  ``min_baseline_samples`` 时不做分析（由上层返回明确的 422）。
- **恢复判定**：事件后窗口 ``[event_ts, event_ts + horizon_seconds]``
  （含端点）内把读数视为**分段线性**曲线，温度、湿度两个维度分别以
  ``基线 ± 容差`` 为容差带，进入/离开容差带的时刻在跨越线段内**线性
  插值精确求出**，不做阶梯近似；恰在带边界（含等于）算在带内。
- 仅当**两个维度同时在带内连续达到** ``hold_seconds`` 才判定恢复，
  ``recovered_at`` 为连续在带起点之后满 hold 的精确时刻；任一维离开
  容差带、或相邻读数间隔**大于** ``max_gap_seconds``（恰好等于仍连续）
  即中断并重新计时，断档两侧绝不拼接。
- 事件后窗口内任一维相对基线的**最大偏差**（绝对值，并列取最早时刻）
  随结果返回；窗口内没有任何读数时给出 ``no_post_event_data`` 原因，
  有读数但满 horizon 仍未连续在带时给出
  ``not_recovered_within_horizon``。

全部计算确定性：相同输入（读数、事件时刻与各参数）恒得相同结果。
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Protocol

NO_POST_EVENT_DATA = "no_post_event_data"
NOT_RECOVERED_WITHIN_HORIZON = "not_recovered_within_horizon"


class _SampleLike(Protocol):
    ts: dt.datetime
    temperature: float
    humidity: float


class InsufficientBaseline(Exception):
    """事件前基线窗口内样本数少于 ``min_baseline_samples``。"""

    def __init__(self, sample_count: int, min_required: int):
        super().__init__(
            f"基线样本不足：事件前窗口内仅 {sample_count} 个读数，"
            f"至少需要 {min_required} 个"
        )
        self.sample_count = sample_count
        self.min_required = min_required


@dataclass(frozen=True)
class Baseline:
    """事件前窗口基线（分维中位数）。"""

    temperature: float
    humidity: float
    sample_count: int
    window_start: dt.datetime
    window_end: dt.datetime


@dataclass(frozen=True)
class _Piece:
    """某一维（或两维共同）连续在容差带内的时间片。"""

    start_ts: dt.datetime
    end_ts: dt.datetime


@dataclass
class RecoveryResult:
    """扰动恢复分析结果。"""

    event_ts: dt.datetime
    horizon_end_ts: dt.datetime
    baseline: Baseline
    recovered: bool
    recovered_at: dt.datetime | None
    recovery_seconds: float | None
    post_sample_count: int
    max_temp_deviation: float | None
    max_temp_deviation_ts: dt.datetime | None
    max_humidity_deviation: float | None
    max_humidity_deviation_ts: dt.datetime | None
    not_recovered_reason: str | None


def _cross_ts(t0: dt.datetime, t1: dt.datetime, frac: float) -> dt.datetime:
    """插值出交点时刻；端点分数直接返回原对象，保证相邻区间精确相接。"""
    if frac <= 0.0:
        return t0
    if frac >= 1.0:
        return t1
    return t0 + dt.timedelta(seconds=frac * (t1 - t0).total_seconds())


def _value_at(
    t0: dt.datetime,
    t1: dt.datetime,
    v0: float,
    v1: float,
    ts: dt.datetime,
) -> float:
    """线段 (t0,v0)→(t1,v1) 在时刻 ``ts`` 的线性插值取值。"""
    span = (t1 - t0).total_seconds()
    frac = (ts - t0).total_seconds() / span
    return v0 + (v1 - v0) * frac


def _in_band_fractions(
    v0: float,
    v1: float,
    lo: float,
    hi: float,
) -> tuple[float, float] | None:
    """单采样区间内某一维处于容差带 [lo, hi]（含边界）的端点分数。

    值沿线段线性变化，进入/离开带边界的交点按线性插值精确求出。
    返回 ``(f0, f1)``（相对采样区间起点的分数，0 ≤ f0 ≤ f1 ≤ 1）；
    整段都不在带内时返回 ``None``。
    """
    crossings: list[float] = []
    if v1 != v0:
        for level in (lo, hi):
            f = (level - v0) / (v1 - v0)
            if 0.0 < f < 1.0:
                crossings.append(f)
    in0 = lo <= v0 <= hi
    in1 = lo <= v1 <= hi
    if in0 and in1:
        return 0.0, 1.0
    if in0:  # 起点在带内、终点出带：在（唯一的）交点离开
        return 0.0, min(crossings)
    if in1:  # 起点出带、终点在带内：在（唯一的）交点进入
        return max(crossings), 1.0
    # 两端都在带外：线性线段至多依次穿过两条边界，穿越整段带时有两个交点
    if len(crossings) >= 2:
        f_enter, f_leave = sorted(crossings)[:2]
        return f_enter, f_leave
    return None


def _merge(pieces: list[_Piece]) -> list[_Piece]:
    """合并相接（含端点恰好相接）的在带时间片；断档两侧天然不接触。"""
    merged: list[_Piece] = []
    for p in sorted(pieces, key=lambda x: (x.start_ts, x.end_ts)):
        if merged and p.start_ts <= merged[-1].end_ts:
            if p.end_ts > merged[-1].end_ts:
                merged[-1] = _Piece(merged[-1].start_ts, p.end_ts)
        else:
            merged.append(_Piece(p.start_ts, p.end_ts))
    return merged


def _intersect(a: list[_Piece], b: list[_Piece]) -> list[_Piece]:
    """两维各自在带区间的交集：两维同时在带内的连续时间片。"""
    out: list[_Piece] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i].start_ts, b[j].start_ts)
        end = min(a[i].end_ts, b[j].end_ts)
        if start <= end:
            out.append(_Piece(start, end))
        if a[i].end_ts < b[j].end_ts:
            i += 1
        else:
            j += 1
    return out


def analyze_recovery(
    samples: list[_SampleLike],
    event_ts: dt.datetime,
    baseline_seconds: float,
    horizon_seconds: float,
    min_baseline_samples: int,
    temp_tolerance: float,
    humidity_tolerance: float,
    hold_seconds: float,
    max_gap_seconds: float,
) -> RecoveryResult:
    """对单个展柜执行扰动恢复分析。

    samples 无需预先排序；同一展柜的读数应已按 (device_sn, ts) 合并。
    基线窗口样本不足时抛 :class:`InsufficientBaseline`。
    """
    ordered = sorted(samples, key=lambda s: s.ts)
    baseline_start = event_ts - dt.timedelta(seconds=baseline_seconds)
    horizon_end = event_ts + dt.timedelta(seconds=horizon_seconds)

    baseline_samples = [s for s in ordered if baseline_start <= s.ts <= event_ts]
    if len(baseline_samples) < min_baseline_samples:
        raise InsufficientBaseline(len(baseline_samples), min_baseline_samples)

    baseline = Baseline(
        temperature=statistics.median(s.temperature for s in baseline_samples),
        humidity=statistics.median(s.humidity for s in baseline_samples),
        sample_count=len(baseline_samples),
        window_start=baseline_start,
        window_end=event_ts,
    )
    temp_lo = baseline.temperature - temp_tolerance
    temp_hi = baseline.temperature + temp_tolerance
    hum_lo = baseline.humidity - humidity_tolerance
    hum_hi = baseline.humidity + humidity_tolerance

    post = [s for s in ordered if event_ts <= s.ts <= horizon_end]

    # 事件后窗口内没有任何读数：曲线完全未知
    if not post:
        return RecoveryResult(
            event_ts=event_ts,
            horizon_end_ts=horizon_end,
            baseline=baseline,
            recovered=False,
            recovered_at=None,
            recovery_seconds=None,
            post_sample_count=0,
            max_temp_deviation=None,
            max_temp_deviation_ts=None,
            max_humidity_deviation=None,
            max_humidity_deviation_ts=None,
            not_recovered_reason=NO_POST_EVENT_DATA,
        )

    temp_pieces: list[_Piece] = []
    hum_pieces: list[_Piece] = []
    # 记录每个事件后读数是否被某条有效（未断档）线段覆盖
    covered_post: set[dt.datetime] = set()
    # 最大偏差候选：窗口内全部读数 + 窗口边界落在线段内部时的插值取值
    temp_candidates: list[tuple[float, dt.datetime]] = [
        (abs(s.temperature - baseline.temperature), s.ts) for s in post
    ]
    hum_candidates: list[tuple[float, dt.datetime]] = [
        (abs(s.humidity - baseline.humidity), s.ts) for s in post
    ]

    for i in range(len(ordered) - 1):
        t0, t1 = ordered[i].ts, ordered[i + 1].ts
        if t1 < event_ts or t0 > horizon_end:
            continue  # 线段与事件后窗口无交集
        span = (t1 - t0).total_seconds()
        if span <= 0.0 or span > max_gap_seconds:
            continue  # 采样间隔超过 max_gap：曲线断开，未知区间不参与恢复计时

        for ts in (t0, t1):
            if event_ts <= ts <= horizon_end:
                covered_post.add(ts)

        # 把线段裁剪到事件后窗口（含端点）
        a = max(t0, event_ts)
        b = min(t1, horizon_end)

        for value0, value1, lo, hi, store in (
            (ordered[i].temperature, ordered[i + 1].temperature,
             temp_lo, temp_hi, temp_pieces),
            (ordered[i].humidity, ordered[i + 1].humidity,
             hum_lo, hum_hi, hum_pieces),
        ):
            fracs = _in_band_fractions(value0, value1, lo, hi)
            if fracs is None:
                continue
            d_start = _cross_ts(t0, t1, fracs[0])
            d_end = _cross_ts(t0, t1, fracs[1])
            c_start, c_end = max(d_start, a), min(d_end, b)
            if c_start <= c_end:
                store.append(_Piece(c_start, c_end))

        # 窗口边界落在该线段内部时，边界插值点同样参与最大偏差统计
        for ts in (a, b):
            if t0 < ts < t1:
                temp_candidates.append(
                    (abs(_value_at(t0, t1, ordered[i].temperature,
                                   ordered[i + 1].temperature, ts)
                         - baseline.temperature), ts)
                )
                hum_candidates.append(
                    (abs(_value_at(t0, t1, ordered[i].humidity,
                                   ordered[i + 1].humidity, ts)
                         - baseline.humidity), ts)
                )

    # 未被任何有效线段覆盖的事件后读数（线段两端均断档）：
    # 该点本身的在带状态以零长时间片计入，使 hold_seconds=0 时可在
    # 该点恢复；正数 hold 仍无法在孤立点上满足。
    for s in post:
        if s.ts in covered_post:
            continue
        if temp_lo <= s.temperature <= temp_hi:
            temp_pieces.append(_Piece(s.ts, s.ts))
        if hum_lo <= s.humidity <= hum_hi:
            hum_pieces.append(_Piece(s.ts, s.ts))

    joint = _intersect(_merge(temp_pieces), _merge(hum_pieces))

    recovered_at: dt.datetime | None = None
    for piece in joint:
        if (piece.end_ts - piece.start_ts).total_seconds() >= hold_seconds:
            recovered_at = piece.start_ts + dt.timedelta(seconds=hold_seconds)
            break

    def _max_dev(candidates: list[tuple[float, dt.datetime]]):
        # 最大偏差；并列时取最早时刻（列表按时间升序构造，严格不等号即可）
        best_dev, best_ts = -1.0, None
        for dev, ts in candidates:
            if dev > best_dev:
                best_dev, best_ts = dev, ts
        return best_dev, best_ts

    max_temp, max_temp_ts = _max_dev(temp_candidates)
    max_hum, max_hum_ts = _max_dev(hum_candidates)

    return RecoveryResult(
        event_ts=event_ts,
        horizon_end_ts=horizon_end,
        baseline=baseline,
        recovered=recovered_at is not None,
        recovered_at=recovered_at,
        recovery_seconds=(
            (recovered_at - event_ts).total_seconds()
            if recovered_at is not None
            else None
        ),
        post_sample_count=len(post),
        max_temp_deviation=max_temp,
        max_temp_deviation_ts=max_temp_ts,
        max_humidity_deviation=max_hum,
        max_humidity_deviation_ts=max_hum_ts,
        not_recovered_reason=(
            None if recovered_at is not None else NOT_RECOVERED_WITHIN_HORIZON
        ),
    )
