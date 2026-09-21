"""文物展柜环境变点分析 API。"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import services
from .database import CaseState, SessionLocal, init_db, to_utc
from .excursions import Thresholds
from .recovery import InsufficientBaselineSamples
from .schemas import (
    AnchorIn,
    AnchorOut,
    BatchIngest,
    CalibratedTimelineOut,
    CaseOut,
    CondensationRiskOut,
    ExcursionsOut,
    ExposureBudgetOut,
    HealthOut,
    IncidentsOut,
    IngestResponse,
    IngestSummary,
    PropagationOut,
    ReadingIn,
    RecoveryOut,
    ReplayPreviewResponse,
    SegmentationOut,
    TimelineOut,
)
from .segmentation import Sample, segment_samples
from .timeline import ForcedPointsOverflow


class PreviewBody(BaseModel):
    """重放预览的试算输入（全部仅在内存中处理，不写库）。"""

    readings: list[ReadingIn] = Field(default_factory=list, description="待补入的（含迟到的）读数")
    anchors: list[AnchorIn] = Field(default_factory=list, description="待补入的锚点")
    penalty: float | None = Field(None, ge=0.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="文物展柜环境变点分析 API",
    description=(
        "接收带设备序号与时间戳的温湿度读数，按惩罚项对每个展柜执行"
        "确定性变点分段；合并同序号重复上报，支持迟到读数补入后的"
        "时间线重放、锚点校准与无写库重放预览；并提供基于线性插值的"
        "越界剂量（excursion dose）事件查询与分维累计剂量预算"
        "（exposure budget）耗尽时刻反解，以及基于已对齐锚点参考值的"
        "分维偏移校正时间线（只读）；并支持跨展柜共模突变检测与基于"
        "固定步长插值、正负时滞皮尔逊相关的跨展柜环境传播分析（只读）；"
        "并基于 Magnus 公式露点与表面温度的冷凝裕量提供冷凝风险窗口"
        "检测（线性插值阈值交点、断档/零湿度断开、最短持续过滤，只读）；"
        "并提供展柜扰动恢复分析：以事件前温湿度中位数为基线，事件后按分段"
        "线性曲线精确求双维进入/离开容差带交点，两维同时在带连续保持达标"
        "方判恢复（只读）。"
    ),
    version="1.7.0",
    lifespan=lifespan,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_ts(value: dt.datetime | None) -> dt.datetime | None:
    return to_utc(value) if value is not None else None


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health():
    return HealthOut(status="ok", db="sqlite")


# ---------- 读数上报 ----------


@app.post(
    "/api/v1/readings/batch",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    tags=["readings"],
    summary="批量上报温湿度读数（自动合并重复上报并重放）",
)
def ingest_readings(payload: BatchIngest, db: Session = Depends(get_db)):
    stats = services.merge_readings(db, payload.readings)

    summaries: list[IngestSummary] = []
    replayed_devices: list[str] = []
    total_inserted = total_merged = 0

    for device_sn in sorted(stats):
        inserted = stats[device_sn]["inserted"]
        merged = stats[device_sn]["merged"]
        total_inserted += inserted
        total_merged += merged

        state = db.get(CaseState, device_sn)
        before_version = state.version if state else 0

        replay_out = None
        if payload.replay and (inserted or merged):
            replay_out = services.replay_device(
                db, device_sn, penalty=payload.penalty, changed=True
            )
            replayed_devices.append(device_sn)
        after_version = replay_out.version if replay_out else before_version

        summaries.append(
            IngestSummary(
                device_sn=device_sn,
                inserted=inserted,
                merged=merged,
                affected=bool(inserted or merged),
                before_version=before_version,
                after_version=after_version,
                replay=replay_out,
            )
        )

    db.commit()
    return IngestResponse(
        accepted=len(payload.readings),
        inserted=total_inserted,
        merged=total_merged,
        replayed_devices=replayed_devices,
        summaries=summaries,
    )


# ---------- 展柜与分段查询 ----------


@app.get("/api/v1/cases", response_model=list[CaseOut], tags=["cases"], summary="展柜列表")
def list_cases(db: Session = Depends(get_db)):
    return [
        CaseOut(
            device_sn=state.device_sn,
            version=state.version,
            penalty=state.penalty,
            reading_count=state.reading_count,
            anchor_count=services._count_anchors(db, state.device_sn),
            replayed_at=to_utc(state.replayed_at) if state.replayed_at else None,
        )
        for state in services.list_cases(db)
    ]


@app.get(
    "/api/v1/cases/{device_sn}/segments",
    response_model=SegmentationOut,
    tags=["cases"],
    summary="查询某展柜的变点分段（可按时间范围过滤）",
)
def get_segments(
    device_sn: str,
    start: dt.datetime | None = Query(None, description="起始时间（含），ISO8601"),
    end: dt.datetime | None = Query(None, description="结束时间（含），ISO8601"),
    db: Session = Depends(get_db),
):
    stored = services.get_effective_segmentation(db, device_sn)
    if stored is None:
        raise HTTPException(status_code=404, detail="展柜不存在或尚无读数")

    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc or end_utc:
        lo = start_utc or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        hi = end_utc or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        if lo > hi:
            raise HTTPException(status_code=422, detail="start 不能晚于 end")
        kept = [s for s in stored.segments if s.end_ts >= lo and s.start_ts <= hi]
        stored = stored.model_copy(update={"segments": kept})
    return stored


@app.get(
    "/api/v1/cases/{device_sn}/timeline",
    response_model=TimelineOut,
    tags=["cases"],
    summary="查询时间线降采样读数（强制点 + 比例配额 + 确定性 LTTB）",
)
def get_timeline(
    device_sn: str,
    max_points: int = Query(
        ...,
        ge=1,
        le=1_000_000,
        description="返回点数上限；强制保留点超过该值时返回 422",
    ),
    start: dt.datetime | None = Query(None, description="起始时间（含），ISO8601"),
    end: dt.datetime | None = Query(None, description="结束时间（含），ISO8601"),
    db: Session = Depends(get_db),
):
    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise HTTPException(status_code=422, detail="start 不能晚于 end")

    try:
        timeline = services.build_case_timeline(
            db, device_sn, start_utc, end_utc, max_points
        )
    except ForcedPointsOverflow as exc:
        detail = {
            "message": str(exc),
            "minimum_required": int(exc.minimum_required),
            "max_points": exc.max_points,
        }
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
        )
    if timeline is None:
        raise HTTPException(status_code=404, detail="展柜不存在或尚无读数")
    return timeline


@app.get(
    "/api/v1/cases/{device_sn}/calibrated-timeline",
    response_model=CalibratedTimelineOut,
    tags=["cases"],
    summary="锚点参考值校正的只读时间线（分维偏移线性插值，不写库）",
)
def get_calibrated_timeline(
    device_sn: str,
    start: dt.datetime | None = Query(None, description="起始时间（含），ISO8601"),
    end: dt.datetime | None = Query(None, description="结束时间（含），ISO8601"),
    db: Session = Depends(get_db),
):
    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise HTTPException(status_code=422, detail="start 不能晚于 end")

    out = services.build_case_calibrated_timeline(db, device_sn, start_utc, end_utc)
    if out is None:
        raise HTTPException(status_code=404, detail="展柜不存在或尚无读数")
    return out


@app.get(
    "/api/v1/cases/{device_sn}/excursions",
    response_model=ExcursionsOut,
    tags=["cases"],
    summary="越界剂量查询：线性插值阈值交点 + 分维剂量积分 + 事件合并",
)
def get_excursions(
    device_sn: str,
    start: dt.datetime | None = Query(None, description="起始时间（含），ISO8601"),
    end: dt.datetime | None = Query(None, description="结束时间（含），ISO8601"),
    temp_min: float | None = Query(None, ge=-50.0, le=100.0, description="温度下限（°C）"),
    temp_max: float | None = Query(None, ge=-50.0, le=100.0, description="温度上限（°C）"),
    humidity_min: float | None = Query(None, ge=0.0, le=100.0, description="湿度下限（%RH）"),
    humidity_max: float | None = Query(None, ge=0.0, le=100.0, description="湿度上限（%RH）"),
    max_gap: float = Query(
        600.0,
        gt=0.0,
        le=86400.0,
        description="允许的最大采样间隔（秒）；超过则曲线断开，未知区间不计剂量",
    ),
    db: Session = Depends(get_db),
):
    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise HTTPException(status_code=422, detail="start 不能晚于 end")

    thresholds = Thresholds(
        temp_min=temp_min,
        temp_max=temp_max,
        humidity_min=humidity_min,
        humidity_max=humidity_max,
    )
    if not thresholds.configured():
        raise HTTPException(status_code=422, detail="至少配置一个温度或湿度上下限")
    if temp_min is not None and temp_max is not None and temp_min > temp_max:
        raise HTTPException(status_code=422, detail="temp_min 不能大于 temp_max")
    if humidity_min is not None and humidity_max is not None and humidity_min > humidity_max:
        raise HTTPException(status_code=422, detail="humidity_min 不能大于 humidity_max")

    out = services.build_case_excursions(
        db, device_sn, start_utc, end_utc, thresholds, max_gap
    )
    if out is None:
        raise HTTPException(status_code=404, detail="展柜不存在或尚无读数")
    return out


@app.get(
    "/api/v1/cases/{device_sn}/exposure-budget",
    response_model=ExposureBudgetOut,
    tags=["cases"],
    summary="累计越界剂量预算：分维窗末累计量、剩余额度与首次达到预算的精确时刻",
)
def get_exposure_budget(
    device_sn: str,
    start: dt.datetime | None = Query(None, description="起始时间（含），ISO8601"),
    end: dt.datetime | None = Query(None, description="结束时间（含），ISO8601"),
    temp_min: float | None = Query(None, ge=-50.0, le=100.0, description="温度下限（°C）"),
    temp_max: float | None = Query(None, ge=-50.0, le=100.0, description="温度上限（°C）"),
    humidity_min: float | None = Query(None, ge=0.0, le=100.0, description="湿度下限（%RH）"),
    humidity_max: float | None = Query(None, ge=0.0, le=100.0, description="湿度上限（%RH）"),
    temp_budget: float | None = Query(
        None,
        ge=0.0,
        description="温度越界剂量预算（°C·秒）；提供时至少配置一个温度阈值",
    ),
    humidity_budget: float | None = Query(
        None,
        ge=0.0,
        description="湿度越界剂量预算（%RH·秒）；提供时至少配置一个湿度阈值",
    ),
    max_gap: float = Query(
        600.0,
        gt=0.0,
        le=86400.0,
        description="允许的最大采样间隔（秒）；超过则曲线断开，未知区间不计剂量",
    ),
    db: Session = Depends(get_db),
):
    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise HTTPException(status_code=422, detail="start 不能晚于 end")

    thresholds = Thresholds(
        temp_min=temp_min,
        temp_max=temp_max,
        humidity_min=humidity_min,
        humidity_max=humidity_max,
    )

    temp_configured = temp_min is not None or temp_max is not None
    humidity_configured = humidity_min is not None or humidity_max is not None
    # 每个“阈值 + 对应预算”组合必须成对提供；至少配置一个维度
    if temp_budget is not None and not temp_configured:
        raise HTTPException(status_code=422, detail="提供 temp_budget 时必须至少配置一个温度阈值")
    if humidity_budget is not None and not humidity_configured:
        raise HTTPException(status_code=422, detail="提供 humidity_budget 时必须至少配置一个湿度阈值")
    if temp_configured and temp_budget is None:
        raise HTTPException(status_code=422, detail="配置温度阈值时必须提供 temp_budget")
    if humidity_configured and humidity_budget is None:
        raise HTTPException(status_code=422, detail="配置湿度阈值时必须提供 humidity_budget")
    if temp_budget is None and humidity_budget is None:
        raise HTTPException(
            status_code=422,
            detail="至少配置一个“阈值 + 对应预算”组合（temp_budget / humidity_budget）",
        )
    if temp_min is not None and temp_max is not None and temp_min > temp_max:
        raise HTTPException(status_code=422, detail="temp_min 不能大于 temp_max")
    if humidity_min is not None and humidity_max is not None and humidity_min > humidity_max:
        raise HTTPException(status_code=422, detail="humidity_min 不能大于 humidity_max")

    budgets: dict[str, float] = {}
    if temp_budget is not None:
        budgets["temperature"] = temp_budget
    if humidity_budget is not None:
        budgets["humidity"] = humidity_budget

    out = services.build_case_exposure_budget(
        db, device_sn, start_utc, end_utc, thresholds, budgets, max_gap
    )
    if out is None:
        raise HTTPException(status_code=404, detail="展柜不存在或尚无读数")
    return out


# ---------- 冷凝风险 ----------


@app.get(
    "/api/v1/cases/{device_sn}/condensation-risk",
    response_model=CondensationRiskOut,
    tags=["cases"],
    summary="冷凝风险：Magnus 露点 + 表面温度裕量，线性插值精确风险窗口",
)
def get_condensation_risk(
    device_sn: str,
    surface_temperature_c: float = Query(
        ...,
        ge=-50.0,
        le=100.0,
        description="给定的展柜表面温度（°C）；冷凝裕量 = 表面温度 − Magnus 露点",
    ),
    margin_threshold_c: float = Query(
        ...,
        description="冷凝裕量风险阈值（°C）：裕量 ≤ 该值视为风险（含等于）",
    ),
    start: dt.datetime | None = Query(None, description="起始时间（含），ISO8601"),
    end: dt.datetime | None = Query(None, description="结束时间（含），ISO8601"),
    max_gap_seconds: float = Query(
        600.0,
        gt=0.0,
        le=86400.0,
        description="允许的最大采样间隔（秒）；超过则曲线断开，未知区间不计风险",
    ),
    min_duration_seconds: float = Query(
        0.0,
        ge=0.0,
        description="风险窗口最短持续时间（秒，含等于）；更短的窗口不返回",
    ),
    db: Session = Depends(get_db),
):
    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise HTTPException(status_code=422, detail="start 不能晚于 end")

    out = services.build_case_condensation_risk(
        db,
        device_sn,
        start_utc,
        end_utc,
        surface_temperature_c,
        margin_threshold_c,
        max_gap_seconds,
        min_duration_seconds,
    )
    if out is None:
        raise HTTPException(status_code=404, detail="展柜不存在或尚无读数")
    return out


# ---------- 展柜扰动恢复分析 ----------


@app.get(
    "/api/v1/cases/{device_sn}/recovery",
    response_model=RecoveryOut,
    tags=["cases"],
    summary="扰动恢复：事件前中位数基线 + 事件后双维容差带精确进入/离开交点",
)
def get_recovery(
    device_sn: str,
    event_ts: dt.datetime = Query(..., description="扰动事件时刻（含），ISO8601；基线窗口止于该时刻"),
    baseline_seconds: float = Query(..., gt=0.0, description="事件前基线窗口长度（秒，必须为正）"),
    horizon_seconds: float = Query(..., gt=0.0, description="事件后观察窗长度（秒，必须为正）"),
    min_baseline_samples: int = Query(
        ..., ge=1, description="基线窗口内最少读数数量（含）；不足返回 422"
    ),
    temp_tolerance: float = Query(
        ..., gt=0.0, description="温度容差半宽（°C，必须为正，含边界）"
    ),
    humidity_tolerance: float = Query(
        ..., gt=0.0, description="湿度容差半宽（%RH，必须为正，含边界）"
    ),
    hold_seconds: float = Query(
        ..., gt=0.0, description="两维同时在带内所需的连续保持秒数（必须为正，含等于）"
    ),
    max_gap: float = Query(
        600.0,
        gt=0.0,
        le=86400.0,
        description="允许的最大相邻读数间隔（秒）；超过即中断保持计时（恰好等于仍连续）",
    ),
    db: Session = Depends(get_db),
):
    event_utc = _parse_ts(event_ts)

    if not services._case_has_readings(db, device_sn):
        raise HTTPException(status_code=404, detail="展柜不存在或尚无读数")

    try:
        return services.build_case_recovery(
            db,
            device_sn,
            event_ts=event_utc,
            baseline_seconds=baseline_seconds,
            horizon_seconds=horizon_seconds,
            min_baseline_samples=min_baseline_samples,
            temp_tolerance=temp_tolerance,
            humidity_tolerance=humidity_tolerance,
            hold_seconds=hold_seconds,
            max_gap_seconds=max_gap,
        )
    except InsufficientBaselineSamples as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(exc),
                "baseline_sample_count": exc.baseline_sample_count,
                "min_baseline_samples": exc.min_baseline_samples,
                "baseline_window_start": exc.window_start.isoformat(),
                "baseline_window_end": exc.window_end.isoformat(),
            },
        )


# ---------- 跨展柜共模突变 ----------


@app.get(
    "/api/v1/incidents",
    response_model=IncidentsOut,
    tags=["incidents"],
    summary="跨展柜共模突变：非基线段首候选窗 + 全局最优互不重叠选择",
)
def get_incidents(
    start: dt.datetime = Query(..., description="起始时间（含），ISO8601"),
    end: dt.datetime = Query(..., description="结束时间（含），ISO8601"),
    tolerance_seconds: float = Query(
        ...,
        gt=0.0,
        description="候选事件最早与最晚边界之差的上限（秒，含等于），必须为正",
    ),
    min_devices: int = Query(
        ...,
        ge=2,
        description="合法候选要求的最少 distinct 展柜数（至少 2）",
    ),
    db: Session = Depends(get_db),
):
    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc > end_utc:
        raise HTTPException(status_code=422, detail="start 不能晚于 end")

    return services.build_incidents(
        db,
        start_utc,
        end_utc,
        tolerance_seconds=tolerance_seconds,
        min_devices=min_devices,
    )


# ---------- 跨展柜环境传播分析 ----------


@app.get(
    "/api/v1/propagation",
    response_model=PropagationOut,
    tags=["propagation"],
    summary="跨展柜环境传播：重叠时段固定步长插值 + 正负时滞皮尔逊相关",
)
def get_propagation(
    source_sn: str = Query(
        ..., min_length=1, max_length=64, description="源展柜设备序号（领先方候选）"
    ),
    target_sn: str = Query(
        ..., min_length=1, max_length=64, description="目标展柜设备序号"
    ),
    dimension: str = Query(
        ...,
        pattern="^(temperature|humidity)$",
        description="分析维度：temperature（温度）/ humidity（湿度）",
    ),
    start: dt.datetime = Query(..., description="起始时间（含），ISO8601"),
    end: dt.datetime = Query(..., description="结束时间（含），ISO8601"),
    step_seconds: float = Query(
        ..., gt=0.0, description="固定插值网格步长（秒，必须为正）"
    ),
    max_lag_seconds: float = Query(
        ..., ge=0.0, description="枚举时滞的最大绝对值（秒，非负）；正时滞表示 source 领先 target"
    ),
    max_gap_seconds: float = Query(
        ..., gt=0.0, description="允许的最大相邻读数间隔（秒）；超过则该插值点无效"
    ),
    db: Session = Depends(get_db),
):
    if source_sn == target_sn:
        raise HTTPException(status_code=422, detail="source_sn 不能与 target_sn 相同")

    start_utc = _parse_ts(start)
    end_utc = _parse_ts(end)
    if start_utc > end_utc:
        raise HTTPException(status_code=422, detail="start 不能晚于 end")

    if not services._case_has_readings(db, source_sn):
        raise HTTPException(status_code=404, detail=f"展柜不存在或尚无读数: {source_sn}")
    if not services._case_has_readings(db, target_sn):
        raise HTTPException(status_code=404, detail=f"展柜不存在或尚无读数: {target_sn}")

    return services.build_propagation(
        db,
        source_sn=source_sn,
        target_sn=target_sn,
        dimension=dimension,
        start=start_utc,
        end=end_utc,
        step_seconds=step_seconds,
        max_lag_seconds=max_lag_seconds,
        max_gap_seconds=max_gap_seconds,
    )


# ---------- 锚点校准 ----------


@app.post(
    "/api/v1/anchors",
    response_model=AnchorOut,
    tags=["anchors"],
    summary="登记/更新锚点（强制变点边界）并重放",
)
def add_anchor(payload: AnchorIn, db: Session = Depends(get_db)):
    anchor = services.upsert_anchor(db, payload)
    services.replay_device(db, payload.device_sn, changed=True)
    db.commit()
    info = services.list_anchors(db, payload.device_sn)
    return next(item for item in info if item["ts"] == to_utc(anchor.ts))


@app.get(
    "/api/v1/cases/{device_sn}/anchors",
    response_model=list[AnchorOut],
    tags=["anchors"],
    summary="查询锚点及其对齐情况",
)
def get_anchors(device_sn: str, db: Session = Depends(get_db)):
    return services.list_anchors(db, device_sn)


@app.delete(
    "/api/v1/cases/{device_sn}/anchors/{anchor_ts}",
    response_model=SegmentationOut,
    tags=["anchors"],
    summary="删除锚点并重放该展柜",
)
def remove_anchor(
    device_sn: str,
    anchor_ts: dt.datetime,
    db: Session = Depends(get_db),
):
    anchor_ts = _parse_ts(anchor_ts)
    if not services.delete_anchor(db, device_sn, anchor_ts):
        raise HTTPException(status_code=404, detail="锚点不存在")
    out = services.replay_device(db, device_sn, changed=True)
    db.commit()
    return out


# ---------- 重放预览（严禁写库） ----------


@app.post(
    "/api/v1/replay/preview",
    response_model=ReplayPreviewResponse,
    tags=["replay"],
    summary="重放预览：基于待补入的迟到读数/锚点试算，绝不写库",
)
def replay_preview(
    body: PreviewBody,
    device_sn: str = Query(..., description="展柜设备序号"),
    db: Session = Depends(get_db),
):
    pending = [r for r in body.readings if r.device_sn == device_sn]
    pending_anchors = [a for a in body.anchors if a.device_sn == device_sn]

    # 当前已持久化状态
    current = services.get_stored_segmentation(db, device_sn)
    current_version = current.version if current else 0

    # 在内存中合并“已存储读数 + 待补入读数”，全程不加入数据库会话
    samples = services._load_samples(db, device_sn)
    merged_keys: dict[dt.datetime, Sample] = {s.ts: s for s in samples}
    added = 0
    for r in pending:
        existing = merged_keys.get(r.ts)
        if existing is None:
            merged_keys[r.ts] = Sample(ts=r.ts, temperature=r.temperature, humidity=r.humidity)
        else:
            # Sample 不可变：在内存副本上按 report_count 逐条累计均值，
            # 合并规则与 merge_readings 一致；库内读数、快照、版本号均不受影响
            total = existing.report_count + 1
            merged_keys[r.ts] = Sample(
                ts=existing.ts,
                temperature=(
                    existing.temperature * existing.report_count + r.temperature
                ) / total,
                humidity=(
                    existing.humidity * existing.report_count + r.humidity
                ) / total,
                report_count=total,
            )
        added += 1

    anchor_set = set(services._load_anchor_ts(db, device_sn))
    for a in pending_anchors:
        anchor_set.add(a.ts)

    result = segment_samples(
        device_sn,
        list(merged_keys.values()),
        list(anchor_set),
        penalty=body.penalty,
    )
    preview = services.serialize_result(
        result,
        version=current_version + 1,
        replayed=False,
        replayed_at=None,
    )

    affected_from, affected_to, changed = services.affected_window(current, preview)

    return ReplayPreviewResponse(
        device_sn=device_sn,
        current_version=current_version,
        would_bump_version=changed,
        changed=changed,
        penalty=result.penalty,
        added_readings=added,
        added_anchors=len(pending_anchors),
        affected_from_ts=affected_from,
        affected_to_ts=affected_to,
        preview=preview,
        current=current,
    )
