"""展柜扰动恢复分析：基线中位数 / 双维容差带精确交点 / 连续在带计时
与断档、出带重计时单测 + 接口集成测试（内存 SQLite）。"""

from __future__ import annotations

import datetime as dt
import os

os.environ["CASE_DB_URL"] = "sqlite:///:memory:"
os.environ["CASE_PENALTY"] = "4.0"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine, init_db
from app.main import app
from app.recovery import (
    NO_POST_EVENT_DATA,
    NOT_RECOVERED_WITHIN_HORIZON,
    InsufficientBaseline,
    analyze_recovery,
)
from app.segmentation import Sample

BASE = dt.datetime(2026, 9, 1, 0, 0, tzinfo=dt.timezone.utc)

DEFAULTS = dict(
    baseline_seconds=900.0,
    horizon_seconds=3600.0,
    min_baseline_samples=3,
    temp_tolerance=1.0,
    humidity_tolerance=3.0,
    hold_seconds=60.0,
    max_gap_seconds=600.0,
)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def _sample(minutes: float, t: float, h: float) -> Sample:
    return Sample(BASE + dt.timedelta(minutes=minutes), t, h)


def _reading(sn, ts, t, h):
    return {"device_sn": sn, "ts": ts, "temperature": t, "humidity": h}


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _baseline(n: int = 3) -> list[Sample]:
    return [_sample(-10 + 4 * i, 20.0, 50.0) for i in range(n)]


# ---------- 基线 ----------


def test_baseline_uses_median_in_event_window():
    samples = [
        _sample(-10, 18.0, 40.0),
        _sample(-6, 22.0, 60.0),
        _sample(-2, 20.0, 50.0),
        _sample(5, 20.0, 50.0),
    ]
    r = analyze_recovery(samples, event_ts=BASE, **DEFAULTS)
    assert r.baseline.temperature == 20.0
    assert r.baseline.humidity == 50.0
    assert r.baseline.sample_count == 3
    assert r.baseline.window_start == BASE - dt.timedelta(seconds=900)
    assert r.baseline.window_end == BASE


def test_baseline_even_count_averages_two_middles():
    # 偶数个样本：中位数为中间两个的平均
    samples = [
        _sample(-8, 20.0, 50.0),
        _sample(-4, 22.0, 54.0),
        _sample(5, 20.0, 50.0),
    ]
    r = analyze_recovery(samples, event_ts=BASE, **{**DEFAULTS, "min_baseline_samples": 2})
    assert r.baseline.temperature == 21.0
    assert r.baseline.humidity == 52.0
    assert r.baseline.sample_count == 2


def test_baseline_window_inclusive_endpoints():
    # 恰在窗口起点与 event_ts 上的读数都计入基线；窗口外的不计入
    samples = [
        Sample(BASE - dt.timedelta(seconds=901), 100.0, 100.0),  # 窗口外
        Sample(BASE - dt.timedelta(seconds=900), 19.0, 49.0),    # 恰在起点：计入
        _sample(-6, 21.0, 51.0),
        Sample(BASE, 23.0, 53.0),                                # 恰在 event_ts：计入
        _sample(5, 20.0, 50.0),
    ]
    r = analyze_recovery(samples, event_ts=BASE, **{**DEFAULTS, "min_baseline_samples": 3})
    assert r.baseline.sample_count == 3
    assert r.baseline.temperature == 21.0
    assert r.baseline.humidity == 51.0


def test_insufficient_baseline_raises():
    with pytest.raises(InsufficientBaseline) as exc:
        analyze_recovery(_baseline(2) + [_sample(5, 20.0, 50.0)], event_ts=BASE, **DEFAULTS)
    assert exc.value.sample_count == 2
    assert exc.value.min_required == 3


# ---------- 恢复判定：精确交点与连续在带 ----------


def test_recovered_at_exact_dual_entry_plus_hold():
    # 事件点偏离；0→10min 线性回归：
    # 温度 30→20，带 [19,21]：21 的交点在 9min
    # 湿度 70→50，带 [47,53]：53 的交点在 8.5min
    # 两维共同在带 [9min, 12min]；hold 60s → recovered_at = 10min
    samples = _baseline(3) + [
        _sample(0, 30.0, 70.0),
        _sample(5, 25.0, 60.0),
        _sample(10, 20.0, 50.0),
        _sample(12, 20.0, 50.0),
    ]
    r = analyze_recovery(samples, event_ts=BASE, **DEFAULTS)
    assert r.recovered is True
    assert r.recovered_at == BASE + dt.timedelta(minutes=10)
    assert r.recovery_seconds == 600.0
    assert r.not_recovered_reason is None
    assert r.post_sample_count == 4


def test_hold_too_long_not_recovered():
    samples = _baseline(3) + [
        _sample(0, 30.0, 70.0),
        _sample(5, 25.0, 60.0),
        _sample(10, 20.0, 50.0),
        _sample(12, 20.0, 50.0),
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 181.0}
    )
    # 共同在带仅 [9min, 12min] = 180s
    assert r.recovered is False
    assert r.recovered_at is None
    assert r.recovery_seconds is None
    assert r.not_recovered_reason == NOT_RECOVERED_WITHIN_HORIZON


def test_hold_equal_joint_duration_recovers():
    samples = _baseline(3) + [
        _sample(0, 30.0, 70.0),
        _sample(10, 20.0, 50.0),
        _sample(13, 20.0, 50.0),
    ]
    # 共同在带 [9min, 13min] = 240s；hold 恰好 240s（含等于）→ 恢复
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 240.0}
    )
    assert r.recovered is True
    assert r.recovered_at == BASE + dt.timedelta(minutes=13)


def test_one_dimension_leaving_band_resets_hold():
    # 共同在带首段 [0, 1min]（60s）末尾湿度恰离开带边界：开区间意义上
    # 连续在带不足 hold=61s；湿度回归后两维重新共同在带，计时从头开始
    samples = _baseline(3) + [
        _sample(0, 20.0, 50.0),
        _sample(1, 20.0, 50.0),
        _sample(2, 20.0, 60.0),   # 湿度线性离开带（53 交点在 1.3min）
        _sample(5, 20.0, 50.0),   # 湿度线性回归（53 交点在 4.4min）
        _sample(8, 20.0, 50.0),
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 120.0}
    )
    # 重新计时后的共同在带段 [4.1min, 8min] 满足 120s → 6.1min 恢复
    assert r.recovered is True
    assert r.recovered_at == BASE + dt.timedelta(seconds=4.1 * 60 + 120)

    # 若湿度不出带，首段就能连续满足 120s（2min 即恢复）
    samples_steady = _baseline(3) + [
        _sample(0, 20.0, 50.0),
        _sample(1, 20.0, 50.0),
        _sample(2, 20.0, 50.0),
        _sample(5, 20.0, 50.0),
        _sample(8, 20.0, 50.0),
    ]
    r2 = analyze_recovery(
        samples_steady, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 120.0}
    )
    assert r2.recovered is True
    assert r2.recovered_at == BASE + dt.timedelta(minutes=2)


def test_gap_exceeding_max_gap_resets_hold():
    samples = _baseline(3) + [
        _sample(0, 20.5, 50.5),
        _sample(1, 20.5, 50.5),   # 事件后先在带 60s（hold=100s 尚不满足）
        _sample(30, 20.5, 50.5),  # 间隔 29min > max_gap=10min：断开重计时
        _sample(32, 20.5, 50.5),  # 重新计时 120s 后满足 hold=100s
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 100.0}
    )
    assert r.recovered is True
    assert r.recovered_at == BASE + dt.timedelta(minutes=31, seconds=40)


def test_gap_equal_max_gap_stays_continuous():
    samples = _baseline(3) + [
        _sample(0, 20.5, 50.5),
        _sample(1, 20.5, 50.5),
        _sample(30, 20.5, 50.5),  # 与上一点间隔恰为 max_gap=1740s：仍连续
        _sample(32, 20.5, 50.5),
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "max_gap_seconds": 1740.0, "hold_seconds": 600.0}
    )
    # 连续在带 [0, 32]：第 10min 即满足 600s
    assert r.recovered is True
    assert r.recovered_at == BASE + dt.timedelta(minutes=10)


def test_band_crossed_within_single_segment():
    # 单条线段内穿入又穿出容差带：15→25，带 [19,21]，
    # 交点分数 0.4 与 0.6（4min、6min）；hold=0 在进入瞬间恢复
    samples = _baseline(3) + [
        _sample(0, 25.0, 50.0),
        _sample(10, 15.0, 50.0),
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 0.0}
    )
    assert r.recovered is True
    assert r.recovered_at == BASE + dt.timedelta(minutes=4)
    assert r.recovery_seconds == 240.0


def test_band_boundary_inclusive():
    # 恰好落在带边界（含等于）算在带内：温度恒为 21 = 基线 + 容差
    samples = _baseline(3) + [
        _sample(0, 21.0, 50.0),
        _sample(1, 21.0, 50.0),
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 60.0}
    )
    assert r.recovered is True
    assert r.recovered_at == BASE + dt.timedelta(minutes=1)


def test_in_band_at_event_with_zero_hold_recovers_immediately():
    samples = _baseline(3) + [_sample(5, 20.0, 50.0)]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 0.0}
    )
    assert r.recovered is True
    assert r.recovered_at == BASE
    assert r.recovery_seconds == 0.0


def test_horizon_clips_recovery_window():
    # 共同在带 [9min, 12min]；horizon=600s 裁剪到 10min，恰好满足 60s hold
    samples = _baseline(3) + [
        _sample(0, 30.0, 70.0),
        _sample(5, 25.0, 60.0),
        _sample(10, 20.0, 50.0),
        _sample(12, 20.0, 50.0),
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "horizon_seconds": 600.0}
    )
    assert r.recovered is True
    assert r.recovered_at == BASE + dt.timedelta(minutes=10)
    assert r.horizon_end_ts == BASE + dt.timedelta(minutes=10)

    # horizon 短 1s：共同在带仅 59s，未恢复
    r2 = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "horizon_seconds": 599.0}
    )
    assert r2.recovered is False
    assert r2.not_recovered_reason == NOT_RECOVERED_WITHIN_HORIZON


def test_no_post_event_data():
    r = analyze_recovery(_baseline(3), event_ts=BASE, **DEFAULTS)
    assert r.recovered is False
    assert r.recovered_at is None
    assert r.recovery_seconds is None
    assert r.not_recovered_reason == NO_POST_EVENT_DATA
    assert r.post_sample_count == 0
    assert r.max_temp_deviation is None
    assert r.max_temp_deviation_ts is None
    assert r.max_humidity_deviation is None
    assert r.max_humidity_deviation_ts is None


def test_reading_exactly_at_event_ts_counts_as_post_data():
    # event_ts 上有读数但之后无数据：不是 no_post_event_data
    samples = _baseline(2) + [Sample(BASE, 30.0, 70.0)]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "min_baseline_samples": 2}
    )
    assert r.post_sample_count == 1
    assert r.not_recovered_reason == NOT_RECOVERED_WITHIN_HORIZON


def test_isolated_in_band_reading_recovers_only_with_zero_hold():
    # 事件后唯一读数与前一点间隔超过 max_gap：曲线两侧断开；
    # 该点在带内，hold=0 时可在该点恢复，正数 hold 不行
    samples = _baseline(3) + [_sample(30, 20.0, 50.0)]
    r0 = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 0.0}
    )
    assert r0.recovered is True
    assert r0.recovered_at == BASE + dt.timedelta(minutes=30)

    r1 = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 1.0}
    )
    assert r1.recovered is False
    assert r1.not_recovered_reason == NOT_RECOVERED_WITHIN_HORIZON


# ---------- 事件后最大偏差 ----------


def test_max_deviation_after_event():
    samples = _baseline(3) + [
        _sample(0, 30.0, 70.0),
        _sample(5, 25.0, 60.0),
        _sample(12, 20.0, 50.0),
    ]
    r = analyze_recovery(samples, event_ts=BASE, **DEFAULTS)
    assert r.max_temp_deviation == pytest.approx(10.0)
    assert r.max_temp_deviation_ts == BASE
    assert r.max_humidity_deviation == pytest.approx(20.0)
    assert r.max_humidity_deviation_ts == BASE


def test_max_deviation_tie_keeps_earliest():
    # 15 与 25 相对基线 20 偏差同为 5：取更早出现者（事件点 25）
    samples = _baseline(3) + [
        _sample(0, 25.0, 50.0),
        _sample(10, 15.0, 50.0),
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "hold_seconds": 0.0}
    )
    assert r.max_temp_deviation == pytest.approx(5.0)
    assert r.max_temp_deviation_ts == BASE


def test_max_deviation_includes_horizon_interior_interpolation():
    # horizon=5min 落在 0→10 的线性段内部：插值温度 25、湿度 60，
    # 而窗口内唯一读数（事件点）为 30/70 → 最大偏差仍取事件点读数
    samples = _baseline(3) + [
        _sample(0, 30.0, 70.0),
        _sample(10, 20.0, 50.0),
    ]
    r = analyze_recovery(
        samples, event_ts=BASE, **{**DEFAULTS, "horizon_seconds": 300.0}
    )
    assert r.max_temp_deviation == pytest.approx(10.0)
    assert r.max_humidity_deviation == pytest.approx(20.0)

    # 反向：事件点在带内、线段在 horizon 边界处偏离更多
    samples2 = _baseline(3) + [
        _sample(0, 20.0, 50.0),
        _sample(10, 30.0, 70.0),
    ]
    r2 = analyze_recovery(
        samples2, event_ts=BASE, **{**DEFAULTS, "horizon_seconds": 300.0}
    )
    assert r2.max_temp_deviation == pytest.approx(5.0)
    assert r2.max_temp_deviation_ts == BASE + dt.timedelta(minutes=5)
    assert r2.max_humidity_deviation == pytest.approx(10.0)
    assert r2.max_humidity_deviation_ts == BASE + dt.timedelta(minutes=5)


def test_unsorted_input_deterministic():
    samples = _baseline(3) + [
        _sample(12, 20.0, 50.0),
        _sample(0, 30.0, 70.0),
        _sample(10, 20.0, 50.0),
        _sample(5, 25.0, 60.0),
    ]
    r1 = analyze_recovery(samples, event_ts=BASE, **DEFAULTS)
    r2 = analyze_recovery(list(reversed(samples)), event_ts=BASE, **DEFAULTS)
    assert r1 == r2


# ---------- 接口集成 ----------


def _ingest(client, sn, rows):
    r = client.post("/api/v1/readings/batch", json={"readings": rows})
    assert r.status_code == 200, r.text


_PARAMS = {
    "event_ts": "2026-09-01T08:00:00+08:00",
    "baseline_seconds": 900,
    "horizon_seconds": 3600,
    "min_baseline_samples": 3,
    "temp_tolerance": 1,
    "humidity_tolerance": 3,
    "hold_seconds": 60,
    "max_gap": 600,
}


def test_recovery_endpoint_recovered(client):
    rows = [
        _reading("CASE-R1", "2026-09-01T07:50:00+08:00", 20.0, 50.0),
        _reading("CASE-R1", "2026-09-01T07:54:00+08:00", 20.0, 50.0),
        _reading("CASE-R1", "2026-09-01T07:58:00+08:00", 20.0, 50.0),
        _reading("CASE-R1", "2026-09-01T08:00:00+08:00", 30.0, 70.0),
        _reading("CASE-R1", "2026-09-01T08:05:00+08:00", 25.0, 60.0),
        _reading("CASE-R1", "2026-09-01T08:10:00+08:00", 20.0, 50.0),
        _reading("CASE-R1", "2026-09-01T08:12:00+08:00", 20.0, 50.0),
    ]
    _ingest(client, "CASE-R1", rows)
    r = client.get("/api/v1/cases/CASE-R1/recovery", params=_PARAMS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device_sn"] == "CASE-R1"
    assert body["event_ts"].startswith("2026-09-01T00:00:00")
    assert body["horizon_end_ts"].startswith("2026-09-01T01:00:00")
    # event_ts 上的读数同时计入基线窗口（含端点）与事件后窗口
    assert body["baseline"] == {
        "window_start": "2026-08-31T23:45:00Z",
        "window_end": "2026-09-01T00:00:00Z",
        "sample_count": 4,
        "temperature": 20.0,
        "humidity": 50.0,
    }
    assert body["post_sample_count"] == 4
    assert body["recovered"] is True
    assert _ts(body["recovered_at"]) == _ts("2026-09-01T00:10:00+00:00")
    assert body["recovery_seconds"] == pytest.approx(600.0)
    assert body["max_temp_deviation"] == pytest.approx(10.0)
    assert body["max_humidity_deviation"] == pytest.approx(20.0)
    assert _ts(body["max_temp_deviation_ts"]) == _ts("2026-09-01T00:00:00+00:00")
    assert body["not_recovered_reason"] is None


def test_recovery_endpoint_not_recovered_reason(client):
    rows = [
        _reading("CASE-R2", "2026-09-01T07:50:00+08:00", 20.0, 50.0),
        _reading("CASE-R2", "2026-09-01T07:54:00+08:00", 20.0, 50.0),
        _reading("CASE-R2", "2026-09-01T07:58:00+08:00", 20.0, 50.0),
        _reading("CASE-R2", "2026-09-01T08:00:00+08:00", 30.0, 70.0),
    ]
    _ingest(client, "CASE-R2", rows)
    r = client.get("/api/v1/cases/CASE-R2/recovery", params=_PARAMS)
    assert r.status_code == 200
    body = r.json()
    assert body["recovered"] is False
    assert body["recovered_at"] is None
    assert body["recovery_seconds"] is None
    assert body["not_recovered_reason"] == "not_recovered_within_horizon"
    assert body["max_temp_deviation"] == pytest.approx(10.0)


def test_recovery_endpoint_no_post_data(client):
    rows = [
        _reading("CASE-R3", "2026-09-01T07:50:00+08:00", 20.0, 50.0),
        _reading("CASE-R3", "2026-09-01T07:54:00+08:00", 20.0, 50.0),
        _reading("CASE-R3", "2026-09-01T07:58:00+08:00", 20.0, 50.0),
    ]
    _ingest(client, "CASE-R3", rows)
    r = client.get("/api/v1/cases/CASE-R3/recovery", params=_PARAMS)
    assert r.status_code == 200
    body = r.json()
    assert body["recovered"] is False
    assert body["not_recovered_reason"] == "no_post_event_data"
    assert body["post_sample_count"] == 0
    assert body["max_temp_deviation"] is None
    assert body["max_temp_deviation_ts"] is None
    assert body["max_humidity_deviation"] is None
    assert body["max_humidity_deviation_ts"] is None


def test_recovery_endpoint_insufficient_baseline_422(client):
    rows = [
        _reading("CASE-R4", "2026-09-01T07:50:00+08:00", 20.0, 50.0),
        _reading("CASE-R4", "2026-09-01T07:54:00+08:00", 20.0, 50.0),
        _reading("CASE-R4", "2026-09-01T08:05:00+08:00", 20.0, 50.0),
    ]
    _ingest(client, "CASE-R4", rows)
    r = client.get("/api/v1/cases/CASE-R4/recovery", params=_PARAMS)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["baseline_sample_count"] == 2
    assert detail["min_baseline_samples"] == 3
    assert "基线" in detail["message"]

    # 放宽要求后成功
    r2 = client.get(
        "/api/v1/cases/CASE-R4/recovery", params={**_PARAMS, "min_baseline_samples": 2}
    )
    assert r2.status_code == 200
    assert r2.json()["baseline"]["sample_count"] == 2


def test_recovery_endpoint_unknown_case_404(client):
    r = client.get("/api/v1/cases/NOPE/recovery", params=_PARAMS)
    assert r.status_code == 404


def test_recovery_endpoint_validation_errors(client):
    rows = [
        _reading("CASE-R5", "2026-09-01T07:50:00+08:00", 20.0, 50.0),
        _reading("CASE-R5", "2026-09-01T07:54:00+08:00", 20.0, 50.0),
        _reading("CASE-R5", "2026-09-01T07:58:00+08:00", 20.0, 50.0),
        _reading("CASE-R5", "2026-09-01T08:05:00+08:00", 20.0, 50.0),
    ]
    _ingest(client, "CASE-R5", rows)
    url = "/api/v1/cases/CASE-R5/recovery"

    # 缺少必填参数
    assert client.get(url, params={k: v for k, v in _PARAMS.items()
                                   if k != "event_ts"}).status_code == 422
    assert client.get(url, params={k: v for k, v in _PARAMS.items()
                                   if k != "baseline_seconds"}).status_code == 422
    assert client.get(url, params={k: v for k, v in _PARAMS.items()
                                   if k != "hold_seconds"}).status_code == 422

    # 非法时间
    assert client.get(url, params={**_PARAMS,
                                   "event_ts": "not-a-timestamp"}).status_code == 422

    # 正数参数非法（零 / 负）
    for key in ("baseline_seconds", "horizon_seconds"):
        assert client.get(url, params={**_PARAMS, key: 0}).status_code == 422
        assert client.get(url, params={**_PARAMS, key: -1}).status_code == 422
    for key in ("temp_tolerance", "humidity_tolerance", "max_gap"):
        assert client.get(url, params={**_PARAMS, key: 0}).status_code == 422
        assert client.get(url, params={**_PARAMS, key: -0.5}).status_code == 422

    # min_baseline_samples 必须为正整数
    assert client.get(url, params={**_PARAMS,
                                   "min_baseline_samples": 0}).status_code == 422

    # hold_seconds 允许 0 但不可为负
    assert client.get(url, params={**_PARAMS,
                                   "hold_seconds": -1}).status_code == 422

    # humidity_tolerance 上限 100
    assert client.get(url, params={**_PARAMS,
                                   "humidity_tolerance": 101}).status_code == 422


def test_recovery_endpoint_default_max_gap(client):
    rows = [
        _reading("CASE-R6", "2026-09-01T07:50:00+08:00", 20.0, 50.0),
        _reading("CASE-R6", "2026-09-01T07:54:00+08:00", 20.0, 50.0),
        _reading("CASE-R6", "2026-09-01T07:58:00+08:00", 20.0, 50.0),
        _reading("CASE-R6", "2026-09-01T08:05:00+08:00", 20.0, 50.0),
    ]
    _ingest(client, "CASE-R6", rows)
    body = client.get(
        "/api/v1/cases/CASE-R6/recovery",
        params={k: v for k, v in _PARAMS.items() if k != "max_gap"},
    ).json()
    assert body["max_gap_seconds"] == 600.0


def test_recovery_endpoint_read_only(client):
    rows = [
        _reading("CASE-R7", "2026-09-01T07:50:00+08:00", 20.0, 50.0),
        _reading("CASE-R7", "2026-09-01T07:54:00+08:00", 20.0, 50.0),
        _reading("CASE-R7", "2026-09-01T07:58:00+08:00", 20.0, 50.0),
        _reading("CASE-R7", "2026-09-01T08:05:00+08:00", 20.0, 50.0),
    ]
    _ingest(client, "CASE-R7", rows)
    before = client.get("/api/v1/cases").json()
    for _ in range(2):
        r = client.get("/api/v1/cases/CASE-R7/recovery", params=_PARAMS)
        assert r.status_code == 200
    after = client.get("/api/v1/cases").json()
    assert before == after


def test_recovery_endpoint_deterministic(client):
    rows = [
        _reading("CASE-R8", "2026-09-01T07:50:00+08:00", 20.0, 50.0),
        _reading("CASE-R8", "2026-09-01T07:50:30+08:00", 24.0, 58.0),
        _reading("CASE-R8", "2026-09-01T07:51:00+08:00", 28.0, 50.0),
        _reading("CASE-R8", "2026-09-01T07:51:30+08:00", 20.0, 58.0),
        _reading("CASE-R8", "2026-09-01T07:52:00+08:00", 24.0, 50.0),
        _reading("CASE-R8", "2026-09-01T07:52:30+08:00", 28.0, 58.0),
        _reading("CASE-R8", "2026-09-01T08:05:00+08:00", 20.0, 50.0),
        _reading("CASE-R8", "2026-09-01T08:10:00+08:00", 20.0, 50.0),
    ]
    _ingest(client, "CASE-R8", rows)
    first = client.get("/api/v1/cases/CASE-R8/recovery", params=_PARAMS).json()
    second = client.get("/api/v1/cases/CASE-R8/recovery", params=_PARAMS).json()
    assert first == second
