# 文物展柜环境变点分析 API

基于 **FastAPI + SQLite + SQLAlchemy 2.0** 的展柜温湿度变点（change point）
分析服务。客户端按 **设备序号 + 时间戳** 批量上报温度、湿度读数；服务对每个
展柜独立执行**带惩罚项的确定性变点分段**，自动合并同序号重复上报，支持迟到
读数补入后的时间线重放、锚点校准与“不落库”的重放预览；并提供基于
**线性插值阈值交点**的越界剂量（excursion dose）事件查询、分维**累计剂量
预算**（exposure budget）耗尽时刻反解、基于各展柜非基线段首的**跨展柜
共模突变**（common-mode incident）检测、基于已对齐锚点参考值的
**分维偏移校正时间线**（只读），以及基于固定步长插值与正负时滞
皮尔逊相关的**跨展柜环境传播分析**（propagation，只读），以及基于
Magnus 公式露点与表面温度冷凝裕量的**冷凝风险窗口**检测
（condensation risk，只读）。

---

## 一条命令启动

```bash
docker compose up --build
```

服务在**固定端口 `8080`** 启动（首次运行自动建库，数据存于命名卷 `case-data`）：

- Swagger / OpenAPI 交互文档：<http://localhost:8080/docs>
- OpenAPI 3 描述文件：<http://localhost:8080/openapi.json>
- 健康检查：<http://localhost:8080/health>

无 Docker 环境时本地运行：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 核心行为

### 1. 确定性变点分段

对每个展柜，把温湿度先按量纲缩放、按权重合成二维段内平方误差（SSE），再求
**精确动态规划**最优分段（与一维 PELT 不做剪枝时等价）：

```
最小化   Σ 段内SSE(段) + β × 段数
```

- `β`（惩罚项）越大，分段越保守；默认 `4.0`，可全局配置或在上报/预览时覆盖。
- 结果**完全确定**：相同输入（含相同读数、锚点、惩罚项）恒得相同分段，
  代价相等时选择段数更少的切法。
- 最小段长默认 3 个读数；被锚点强制切开的小区间会自动放宽。

每段返回：`start_ts / end_ts`、样本数、温湿度均值与段内标准差、相对上一段的
`delta_temperature / delta_humidity`、缩放加权的变化幅度 `magnitude`、置信标记
`confidence`，以及是否由锚点界定（`start_anchored / end_anchored`）。

**置信标记**（段首段为 `baseline`）依据相邻两段跳变相对其局部噪声的倍数：

| 标记 | 规则 |
| --- | --- |
| `high` | 跳变 ≥ 3σ 且两段样本数均 ≥ 4，或该边界为**锚点**（人工校准的已知变点） |
| `medium` | 跳变 ≥ 1.5σ 且幅度不低于下限 |
| `low` | 不满足上述条件 |

### 2. 重复上报合并

`(device_sn, ts)` 在库内唯一。同序号同时间戳重复上报按**已有累计均值并入**：

```
new_mean = (old_mean × report_count + value) / (report_count + 1)
```

`report_count` 随之加一，合并规则可重放、结果稳定。

### 3. 迟到读数与时间线重放

读数可乱序、迟到补入。每次入库后，受影响展柜的**整条时间线重新分段**，
覆盖旧的分段快照，版本号 `version` 单调递增。预览接口还会给出
`affected_from_ts / affected_to_ts`（第一个发生变化的段起点 → 时间线末尾）。

### 4. 锚点校准

锚点是强制分段边界。登记后在容差（默认 300 秒）内对齐到最近读数；
对齐成功即成为强制边界且该变点标记为 `high`。锚点先于读数登记时保持
`aligned=false`，读数补入并重放后自动纳入。

### 5. 重放预览（绝不写库）

`POST /api/v1/replay/preview` 把**库内已有数据 + 本次待补入读数/锚点**在内存中
合并试算，返回“预览分段 + 当前分段”的差异对比。待预览读数与库内同时间戳、
或请求内多条读数时间戳重复时，同样按 `report_count` 逐条累计均值——但只在
**内存副本**上合并。整个请求只执行只读查询，不写入任何表、不推进版本号、
不产生副作用。

### 6. 越界剂量事件

`GET /api/v1/cases/{sn}/excursions` 把读数视为**分段线性**曲线，对温度、湿度
分别求出超出上下限的部分并**随时间积分**（剂量：温度 °C·秒、湿度 %RH·秒）：

- 阈值交点按相邻读数**线性插值精确求出**，不做阶梯近似；等于阈值不算越界。
- 相邻读数间隔**大于** `max_gap` 时曲线断开，未知区间**不计剂量**
  （恰好等于 `max_gap` 仍视为连续）；孤立高读数两侧曲线未知时不产生事件。
- 任一维度越界的**连续区间合并为事件**，返回起止时间、触发维度、峰值
  （越界幅度最大处的读数值，并列取最早）、持续秒数与各维累计剂量，
  事件按开始时间稳定排序。
- 至少需配置一个上下限；`temp_min > temp_max`、`humidity_min > humidity_max`、
  `start > end`、`max_gap ≤ 0` 均返回 `422`。

### 7. 累计越界剂量预算

`GET /api/v1/cases/{sn}/exposure-budget` 在与越界剂量事件**完全相同的分段
线性曲线与断档规则**上，按维度给出查询窗末的预算执行情况：

- 阈值交点仍按相邻读数**线性插值精确求出**；相邻读数间隔**大于** `max_gap`
  的区间曲线断开，未知区间**不产生剂量片、不跨断档累计面积**（恰好等于
  `max_gap` 仍视为连续）。
- 每个维度随请求同时给出“阈值上下限 + 剂量预算”组合：温度传
  `temp_min` / `temp_max`（至少一个）与 `temp_budget`（°C·秒，≥ 0），
  湿度传 `humidity_min` / `humidity_max` 与 `humidity_budget`（%RH·秒，
  ≥ 0）；至少配置一个维度。
- 每个维度返回：
  - `cumulative_dose`：窗末累计越界剂量（全部越界剂量片面积之和）；
  - `remaining`：剩余额度 `budget − cumulative_dose`（超出预算时为负）；
  - `budget_exhausted` / `exhausted_ts`：**首次达到预算的精确时刻**——
    在跨越预算的那条线段内，按越界幅度的线性变化反解
    （`e(u)=e0+Δe·u`，解 `A(u)=target` 一元二次方程），时刻落在
    交点与读数之间，不做阶梯近似；窗末仍未达到预算时为 `null`。
  - **零预算**在该维度**首次越界起点**（插值交点）即耗尽。

阈值与预算必须成对出现（只给阈值不给预算、或只给预算不给阈值）、预算为负、
`start > end`、同维下限大于上限均返回 `422`；展柜没有读数返回 `404`，
过滤后为空范围返回 `200` 与各维零累计。

### 8. 跨展柜共模突变

`GET /api/v1/incidents` 把**各展柜现有有效分段**（已落库快照；尚未重放的
展柜即时只读计算）中落在 `[start, end]` 内的**非基线段首**（`confidence`
为 `high/medium/low` 的段起点）汇总起来，检测“多个展柜在很短时间内相继
突变”的共模事件：

1. 全部段首按 `(ts, device_sn)` 全局排序，取任意一段**连续段首**作为候选窗；
2. 候选窗内最早与最晚边界之差 **不超过** `tolerance_seconds`（含等于）；
3. 同一展柜在窗内若出现多个段首，**只保留幅度最大的一条**，幅度并列取
   **时间更早**者；
4. 去重后涉及的 distinct device 数达到 `min_devices` 时候选才合法
   （`min_devices ≥ 2`）。

枚举得到全部合法候选窗后，**不做贪心链式归并**，而是在候选全集上做
**加权区间调度（动态规划）**，选出一组**互不重叠**事件（后一事件的
`start_ts` 严格晚于前一事件的 `end_ts`；同一时刻的突变簇视为一个事件，
不允许拆开）。选择按以下次序依次决定：

1. 入选事件的**设备数总和**最大；
2. 并列时**幅度总和**最大；
3. 仍并列时，按开始时间排列的**起始时间序列**逐项比较、越早越优；序列构成
   前缀并列时**更长者优先**——宁拆成更多个更早的事件，也不归并成一个大事件；
4. 物理量完全并列时再以候选成员键字典序兜底，保证结果严格确定。

响应返回每个事件的区间（成员中最早/最晚边界）、持续秒数、参与展柜数、
**各展柜边界的方向与幅度**，以及各边界幅度之和 `total_magnitude`；事件按
开始时间排序。边界方向 `up/down/flat` 以温、湿两个缩放加权跳变分量中
占主导者的符号判定（温湿并列取温度）。

`start > end`、`tolerance_seconds ≤ 0`、`min_devices < 2`（两个时间参数
必填）均返回 `422`；库内没有读数或过滤后没有合法候选时返回 `200` 与空
事件列表。

### 9. 锚点参考值校正时间线

`GET /api/v1/cases/{sn}/calibrated-timeline` 利用**已对齐锚点**登记的参考
温湿度，生成一条**只读**的校正序列（不修改读数、分段或版本号）。温度、湿度
两个维度**各自独立**计算偏移：

```
偏移 = 锚点参考值 − 锚点所对齐读数的原始读数
```

- 偏移曲线定义在**整条时间线**上：相邻有效锚点（节点）之间按时间**线性
  插值**；首节点之前、末节点之后分别**沿用最近节点的偏移**（常数外推）。
- **缺少该维参考值的锚点不参与该维曲线**（只登记 `ref_temperature` 的锚点
  不影响湿度偏移）；未对齐到读数的锚点两个维度都不参与。
- **多个锚点对齐同一读数**时，按锚点时间升序取**最后一个**有效参考值。
- 某维没有任何有效锚点时，该维偏移恒为 `0`，校正值等于原值。
- 先基于整条时间线建立曲线，**再按 `start` / `end` 过滤输出**——窗口外的
  锚点仍参与窗口内读数的偏移计算。

每个返回点包含该读数的**原值、两维偏移与校正值**
（`calibrated_* = 原值 + 偏移`）。设备没有读数返回 `404`，
`start` 晚于 `end` 返回 `422`，过滤后为空范围返回 `200` 与空序列。

### 10. 跨展柜环境传播分析

`GET /api/v1/propagation` 在两个展柜（`source_sn` / `target_sn`）的
**重叠时段**内，对指定维度（`temperature` / `humidity`）做固定步长
重采样，枚举一组正负时滞并逐档计算皮尔逊相关，用以判断环境变化从哪个
展柜传播而来、领先多少秒：

1. 分析时段取查询窗 `[start, end]` 与两柜读数覆盖范围的**交集**；
2. 在重叠时段上按 `step_seconds` 生成**等距网格**（从重叠起点起，不足
   一个步长的尾部不补点），两柜曲线各自在网格点上按相邻读数**线性
   插值**；
3. 网格点所依赖的相邻读数间隔**大于** `max_gap_seconds` 时该点判为
   **无效**（恰好等于 `max_gap` 仍连续）；落在某柜覆盖范围外的点同样
   无效；
4. 时滞按 `step_seconds` 的整数倍枚举，范围
   `[-max_lag_seconds, +max_lag_seconds]`（含 0）。**正时滞表示 source
   领先 target**：比较 source 在 `t` 与 target 在 `t + lag` 的插值；
5. 每个时滞只使用两侧**同时有效的成对点**计算皮尔逊相关：成对点数少于
   2、或任一成对序列为常量（方差为 0）时该时滞标记为**不可用**，
   `correlation` 为 `null` 并给出 `reason`
   （`no_overlap` / `insufficient_samples` / `constant_series`），
   **任何情况下都不产生 NaN**。

各时滞明细 `lags` 按时滞升序返回。最佳时滞按以下次序**依次择优**：

1. 相关系数**绝对值更大**；
2. 并列时**有效成对点数更多**；
3. 仍并列时**时滞绝对值更小**；
4. 完全并列时**时滞值更小**（正负并列取负）。

`direction` 取 `source_to_target`（最佳时滞 > 0，source 领先）、
`target_to_source`（< 0，target 领先）、`synchronous`（= 0，同步）；
没有任何可用时滞时为 `unavailable`，最佳时滞与相关系数为 `null`。

`source_sn` 与 `target_sn` 相同、`start` 晚于 `end`、`step_seconds ≤ 0`、
`max_lag_seconds < 0`、`max_gap_seconds ≤ 0`、维度非法均返回 `422`；
任一展柜没有读数返回 `404`；两柜无重叠或全部时滞不可用时返回 `200` 与
`direction=unavailable`。接口**只读**，不写库、不改版本号。

### 11. 冷凝风险

`GET /api/v1/cases/{sn}/condensation-risk` 用 **Magnus 公式**由空气温度 T
与相对湿度 RH 逐点计算露点 Td，再以请求给定的**表面温度** Ts 计算
冷凝裕量并检测风险窗口（只读，不写库、不改版本号）：

```
γ  = ln(RH/100) + 17.62·T/(243.12 + T)
Td = 243.12·γ/(17.62 − γ)
冷凝裕量 margin = Ts − Td
```

- 裕量越小越接近结露：`margin ≤ margin_threshold_c` 即风险（**含等于**）。
- 裕量曲线定义在**相邻有效点之间的线段**上并按时间**线性插值**：进入与
  离开风险阈值的时刻在跨越线段内精确反解，不做阶梯近似。
- 相邻读数间隔**大于** `max_gap_seconds` 时曲线断开（恰好等于仍连续），
  未知区间不计风险；**湿度为 0 的读数无效**（`ln(0)` 不定义），相邻线段
  任一端点湿度为 0 时该线段必须断开。**断档两侧的窗口绝不合并**。
- 仅返回持续时间 **≥** `min_duration_seconds`（含等于）的风险窗口；每个
  窗口给出 `start_ts` / `end_ts`（精确阈值交点）、`duration_seconds`，
  以及窗口内**最低裕量 `min_margin` 与其时刻 `min_margin_ts`**——裕量沿
  线段线性，最低裕量必落在某个有效读数时刻（并列取最早）。

`surface_temperature_c`（°C，−50…100）、`margin_threshold_c`（°C）为
必填；`max_gap_seconds` 默认 `600`（必须 **> 0**）、
`min_duration_seconds` 默认 `0`（必须 **≥ 0**）。`start` 晚于 `end`
返回 `422`；展柜没有读数返回 `404`；过滤后为空范围返回 `200` 与空窗口
列表（`sample_count` 含湿度为 0 的无效读数）。

---

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/readings/batch` | 批量上报，自动合并重复并重放受影响展柜 |
| GET  | `/api/v1/cases` | 展柜列表（版本、读数/锚点数） |
| GET  | `/api/v1/cases/{sn}/segments` | 查询分段，支持 `start` / `end` 时间过滤 |
| GET  | `/api/v1/cases/{sn}/timeline?max_points=...` | 时间线降采样读数，支持 `start` / `end` 时间过滤 |
| GET  | `/api/v1/cases/{sn}/excursions` | 越界剂量事件：线性插值交点、分维剂量积分、`max_gap` 断开 |
| GET  | `/api/v1/cases/{sn}/exposure-budget` | 累计越界剂量预算：窗末累计量、剩余额度与首次达到预算的精确时刻 |
| GET  | `/api/v1/cases/{sn}/calibrated-timeline` | 锚点参考值校正的只读时间线：分维偏移线性插值，支持 `start` / `end` 过滤 |
| GET  | `/api/v1/cases/{sn}/condensation-risk` | 冷凝风险：Magnus 露点 + 表面温度裕量，线性插值精确窗口、断档/零湿度断开 |
| GET  | `/api/v1/incidents` | 跨展柜共模突变：非基线段首候选窗 + 全局最优互不重叠选择 |
| GET  | `/api/v1/propagation` | 跨展柜环境传播：重叠时段固定步长插值 + 正负时滞皮尔逊相关（只读） |
| POST | `/api/v1/anchors` | 登记/更新锚点并重放 |
| GET  | `/api/v1/cases/{sn}/anchors` | 查询锚点及对齐情况 |
| DELETE | `/api/v1/cases/{sn}/anchors/{ts}` | 删除锚点并重放 |
| POST | `/api/v1/replay/preview?device_sn=...` | 无写库重放预览 |
| GET  | `/health` | 健康检查 |

### 上报读数

```bash
curl -X POST http://localhost:8080/api/v1/readings/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "penalty": 4.0,
    "readings": [
      {"device_sn": "CASE-007", "ts": "2026-09-01T08:00:00+08:00", "temperature": 20.1, "humidity": 55.0},
      {"device_sn": "CASE-007", "ts": "2026-09-01T08:10:00+08:00", "temperature": 20.2, "humidity": 55.1}
    ]
  }'
```

响应含每个展柜的 `inserted` / `merged`、前后版本号与本次重放分段（`replay`）。
若只想入库暂不重放，可传 `"replay": false`。

### 锚点校准

```bash
curl -X POST http://localhost:8080/api/v1/anchors \
  -H 'Content-Type: application/json' \
  -d '{"device_sn": "CASE-007", "ts": "2026-09-01T09:00:00+08:00",
       "label": "更换调湿剂", "ref_temperature": 21.0, "ref_humidity": 60.0}'
```

### 迟到读数预览（不写库）

```bash
curl -X POST 'http://localhost:8080/api/v1/replay/preview?device_sn=CASE-007' \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
        {"device_sn": "CASE-007", "ts": "2026-09-01T08:30:00+08:00",
         "temperature": 24.9, "humidity": 70.2}
      ]}'
```

返回 `changed`、`would_bump_version`、受影响时间范围，以及 `preview` / `current`
两份分段；执行后再查读数数量与版本号均不会变化。

### 时间线降采样

```bash
curl 'http://localhost:8080/api/v1/cases/CASE-007/timeline?max_points=200&start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z'
```

返回按时间升序排列的温湿度读数，在不超过 `max_points` 的前提下做确定性降采样：

1. **强制点**（去重，一个点可同时命中多条原因，原因随该点返回）：
   - 过滤后范围的**首点、尾点**（`range_start` / `range_end`）；
   - 范围内**温度、湿度各自的全局极小/极大值**（`temp_min` / `temp_max` /
     `humidity_min` / `humidity_max`，并列取最早出现）；
   - **已对齐锚点**所在读数（`aligned_anchor`）；
   - **分段边界**所在读数（`segment_boundary`，分段取自已落库快照，
     尚未重放的展柜即时只读计算）。
2. 强制点去重后若**已超过** `max_points`，接口返回 `422` 并在响应体给出
   `minimum_required`，**绝不静默丢点**；把 `max_points` 调到该值以上即可取全。
3. 其余名额按**相邻强制点区间内候选点数比例**分配（最大余数法），余数按分数
   降序派发，分数相同则**区间起始时间更早者优先**（稳定分派）。
4. 各区间在两个强制端点之间独立做**确定性 LTTB**（温、湿度两个量纲的三角形
   面积之和，全部并列取下标最小者）；区间配额不小于候选数时候选点全部保留，
   这些点标记为 `lttb`。

`start` / `end` 均为含端点的 ISO8601 过滤；**极值统计与名额预算都只基于过滤后
范围**。过滤后为空范围时返回 `200` 与空数组；设备没有任何读数时返回 `404`。

响应示例（节选）：

```json
{
  "device_sn": "CASE-007",
  "start": "2026-09-01T00:00:00Z",
  "end": "2026-09-02T00:00:00Z",
  "max_points": 200,
  "original_count": 1440,
  "returned_count": 200,
  "points": [
    {"ts": "2026-09-01T00:00:00Z", "temperature": 20.1, "humidity": 55.0,
     "reasons": ["range_start", "segment_boundary"]},
    {"ts": "2026-09-01T00:07:00Z", "temperature": 28.4, "humidity": 72.6,
     "reasons": ["temp_max"]}
  ]
}
```

强制点超预算时的 `422` 响应：

```json
{"detail": {"message": "强制保留点 8 个，已超过 max_points=5",
            "minimum_required": 8, "max_points": 5}}
```

### 越界剂量查询

```bash
curl 'http://localhost:8080/api/v1/cases/CASE-007/excursions?temp_max=26&humidity_max=65&max_gap=600&start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z'
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `start` / `end` | 不限 | 含端点的 ISO8601 过滤；剂量只基于过滤后范围内的分段线性曲线 |
| `temp_min` / `temp_max` | 不限 | 温度下/上限（°C，−50…100） |
| `humidity_min` / `humidity_max` | 不限 | 湿度下/上限（%RH，0…100） |
| `max_gap` | `600` | 允许的最大采样间隔（秒，0 < x ≤ 86400）；超过则断开，未知区间不计剂量 |

四个上下限至少配置一个，否则返回 `422`；同一维度的下限大于上限、
`start` 晚于 `end`、`max_gap` 非正数同样返回 `422`。设备没有读数返回 `404`，
过滤后为空范围返回 `200` 与空事件列表。

响应示例（温度、湿度越界区间重叠，合并为一个事件）：

```json
{
  "device_sn": "CASE-007",
  "start": "2026-09-01T00:00:00Z",
  "end": "2026-09-02T00:00:00Z",
  "max_gap_seconds": 600.0,
  "temp_min": null, "temp_max": 26.0,
  "humidity_min": null, "humidity_max": 65.0,
  "sample_count": 4,
  "event_count": 1,
  "events": [
    {
      "start_ts": "2026-09-01T00:00:00Z",
      "end_ts": "2026-09-01T00:30:00Z",
      "dimensions": ["temperature", "humidity"],
      "duration_seconds": 1800.0,
      "temp_dose": 6750.0,
      "humidity_dose": 13500.0,
      "peak_temperature": 30.0,
      "peak_temperature_ts": "2026-09-01T00:00:00Z",
      "peak_humidity": 70.0,
      "peak_humidity_ts": "2026-09-01T00:10:00Z"
    }
  ]
}
```

起止时间为线性插值得到的**精确阈值交点**（落在读数之间）；`temp_dose` /
`humidity_dose` 为事件内各维越界幅度对时间的累计积分；未触发某维度时对应
剂量为 `0`、峰值字段为 `null`。

### 累计越界剂量预算查询

```bash
curl 'http://localhost:8080/api/v1/cases/CASE-007/exposure-budget?temp_max=26&temp_budget=2000&humidity_max=65&humidity_budget=5000&max_gap=600&start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z'
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `start` / `end` | 不限 | 含端点的 ISO8601 过滤；剂量只基于过滤后范围内的分段线性曲线 |
| `temp_min` / `temp_max` | 不限 | 温度下/上限（°C，−50…100）；配置温度维度时至少给一个 |
| `humidity_min` / `humidity_max` | 不限 | 湿度下/上限（%RH，0…100）；配置湿度维度时至少给一个 |
| `temp_budget` | 无 | 温度剂量预算（°C·秒，**≥ 0**）；与温度阈值成对出现 |
| `humidity_budget` | 无 | 湿度剂量预算（%RH·秒，**≥ 0**）；与湿度阈值成对出现 |
| `max_gap` | `600` | 允许的最大采样间隔（秒，0 < x ≤ 86400）；超过则断开，未知区间不计剂量 |

至少配置一个“阈值 + 对应预算”组合；阈值与预算不成对（只给其一）、
预算为负、同维下限大于上限、`start` 晚于 `end`、`max_gap` 非正数均返回
`422`。设备没有读数返回 `404`，过滤后为空范围返回 `200` 与各维零累计、
预算未耗尽。

响应示例（温度预算在线段内耗尽，湿度预算尚未用尽）：

```json
{
  "device_sn": "CASE-007",
  "start": "2026-09-01T00:00:00Z",
  "end": "2026-09-02T00:00:00Z",
  "max_gap_seconds": 600.0,
  "temp_min": null, "temp_max": 26.0,
  "humidity_min": null, "humidity_max": 65.0,
  "temp_budget": 2000.0,
  "humidity_budget": 5000.0,
  "sample_count": 4,
  "dimensions": [
    {
      "dimension": "temperature",
      "budget": 2000.0,
      "cumulative_dose": 2400.0,
      "remaining": -400.0,
      "budget_exhausted": true,
      "exhausted_ts": "2026-09-01T00:12:34.618298Z"
    },
    {
      "dimension": "humidity",
      "budget": 5000.0,
      "cumulative_dose": 1350.0,
      "remaining": 3650.0,
      "budget_exhausted": false,
      "exhausted_ts": null
    }
  ]
}
```

`exhausted_ts` 在跨越预算的线段（两个相邻读数经插值切出的越界片段）内，
按越界幅度的线性变化反解，时刻精确到微秒；**零预算**在该维度首次越界
起点耗尽，故 `exhausted_ts` 即第一个阈值交点。断档两侧的剂量片彼此独立，
累计与耗尽时刻都不会跨越未知区间。

### 跨展柜共模突变查询

```bash
curl 'http://localhost:8080/api/v1/incidents?start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z&tolerance_seconds=600&min_devices=2'
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `start` / `end` | 是 | 含端点的 ISO8601 时间过滤；只有段首落在该闭区间内的非基线边界参与检测 |
| `tolerance_seconds` | 是 | 候选事件最早/最晚边界跨度上限（秒，必须 **> 0**，含等于） |
| `min_devices` | 是 | 合法候选要求的最少 distinct 展柜数（**≥ 2**） |

响应示例（三个展柜在两分钟内同向跳变，归为一个共模事件）：

```json
{
  "start": "2026-09-01T00:00:00Z",
  "end": "2026-09-02T00:00:00Z",
  "tolerance_seconds": 600.0,
  "min_devices": 2,
  "candidate_count": 4,
  "boundary_count": 3,
  "incident_count": 1,
  "incidents": [
    {
      "start_ts": "2026-09-01T00:06:00Z",
      "end_ts": "2026-09-01T00:08:00Z",
      "duration_seconds": 120.0,
      "device_count": 3,
      "total_magnitude": 7.5,
      "boundaries": [
        {"device_sn": "CASE-A", "ts": "2026-09-01T00:06:00Z",
         "direction": "up", "magnitude": 2.5,
         "delta_temperature": 4.0, "delta_humidity": 15.0},
        {"device_sn": "CASE-B", "ts": "2026-09-01T00:07:00Z",
         "direction": "up", "magnitude": 2.5,
         "delta_temperature": 4.0, "delta_humidity": 15.0},
        {"device_sn": "CASE-C", "ts": "2026-09-01T00:08:00Z",
         "direction": "up", "magnitude": 2.5,
         "delta_temperature": 4.0, "delta_humidity": 15.0}
      ]
    }
  ]
}
```

`candidate_count` 为枚举得到的全部合法连续候选窗数量（同一展柜已按幅度
去重、成员集合相同的窗去重）；`boundary_count` 为过滤后参与检测的非基线
段首总数。事件区间即成员边界的最早/最晚时刻，`boundaries` 内每个展柜
恰好一条且按 `(ts, device_sn)` 排序。

### 锚点参考值校正时间线

先登记带参考值的锚点（对齐到容差内最近读数）：

```bash
curl -X POST http://localhost:8080/api/v1/anchors \
  -H 'Content-Type: application/json' \
  -d '{"device_sn": "CASE-007", "ts": "2026-09-01T08:00:00+08:00",
       "label": "标准源校准", "ref_temperature": 22.0, "ref_humidity": 52.0}'
curl -X POST http://localhost:8080/api/v1/anchors \
  -H 'Content-Type: application/json' \
  -d '{"device_sn": "CASE-007", "ts": "2026-09-01T08:50:00+08:00",
       "label": "复测", "ref_temperature": 24.0, "ref_humidity": 58.0}'
```

再查询校正时间线（`start` / `end` 可选，含端点）：

```bash
curl 'http://localhost:8080/api/v1/cases/CASE-007/calibrated-timeline?start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z'
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `start` / `end` | 不限 | 含端点的 ISO8601 过滤；偏移曲线先基于整条时间线建立，再按窗口过滤输出 |

响应示例（两个锚点之间偏移按时间线性插值，首尾区间沿用最近偏移）：

```json
{
  "device_sn": "CASE-007",
  "start": "2026-09-01T00:00:00Z",
  "end": "2026-09-02T00:00:00Z",
  "sample_count": 3,
  "aligned_anchor_count": 2,
  "points": [
    {"ts": "2026-09-01T00:00:00Z", "temperature": 20.0, "humidity": 50.0,
     "temperature_offset": 2.0, "humidity_offset": 2.0,
     "calibrated_temperature": 22.0, "calibrated_humidity": 52.0},
    {"ts": "2026-09-01T00:25:00Z", "temperature": 21.5, "humidity": 51.5,
     "temperature_offset": 1.5, "humidity_offset": 3.5,
     "calibrated_temperature": 23.0, "calibrated_humidity": 55.0},
    {"ts": "2026-09-01T00:50:00Z", "temperature": 23.0, "humidity": 53.0,
     "temperature_offset": 1.0, "humidity_offset": 5.0,
     "calibrated_temperature": 24.0, "calibrated_humidity": 58.0}
  ]
}
```

温度、湿度两维**各自独立**成曲线：`偏移 = 锚点参考值 − 对齐读数原值`，
相邻有效锚点之间按时间线性插值，首节点之前、末节点之后沿用最近偏移；
缺少某维参考值的锚点不参与该维曲线，该维没有有效锚点时偏移恒为 `0`。
多个锚点对齐同一读数时按锚点时间升序取最后一个有效参考值。接口**只读**：
不修改读数、分段或版本号。设备没有读数返回 `404`，`start` 晚于 `end`
返回 `422`，过滤后为空范围返回 `200` 与空序列。

### 跨展柜环境传播分析

```bash
curl 'http://localhost:8080/api/v1/propagation?source_sn=CASE-A&target_sn=CASE-B&dimension=temperature&start=2026-09-01T00:00:00Z&end=2026-09-01T01:00:00Z&step_seconds=60&max_lag_seconds=300&max_gap_seconds=600'
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `source_sn` / `target_sn` | 是 | 源 / 目标展柜序号；二者相同返回 `422`，任一无读数返回 `404` |
| `dimension` | 是 | `temperature` 或 `humidity` |
| `start` / `end` | 是 | 含端点的 ISO8601 时间窗；实际网格取它与两柜覆盖范围的交集 |
| `step_seconds` | 是 | 固定插值网格步长（秒，**> 0**）；时滞按其整数倍枚举 |
| `max_lag_seconds` | 是 | 枚举时滞的最大绝对值（秒，**≥ 0**）；正时滞表示 source 领先 target |
| `max_gap_seconds` | 是 | 允许的最大相邻读数间隔（秒，**> 0**）；超过则该插值点无效 |

响应示例（target 的温度曲线整体落后 source 2 分钟，最佳时滞 `+120s`，
方向 `source_to_target`）：

```json
{
  "source_sn": "CASE-A",
  "target_sn": "CASE-B",
  "dimension": "temperature",
  "start": "2026-09-01T00:00:00Z",
  "end": "2026-09-01T01:00:00Z",
  "overlap_start": "2026-09-01T00:02:00Z",
  "overlap_end": "2026-09-01T00:11:00Z",
  "step_seconds": 60.0,
  "max_lag_seconds": 300.0,
  "max_gap_seconds": 600.0,
  "grid_point_count": 10,
  "source_valid_count": 10,
  "target_valid_count": 10,
  "lags": [
    {"lag_seconds": -300.0, "available": true, "pair_count": 5, "correlation": -0.7613188819880644, "reason": null},
    {"lag_seconds": 0.0, "available": true, "pair_count": 10, "correlation": 0.31556038781189183, "reason": null},
    {"lag_seconds": 60.0, "available": true, "pair_count": 9, "correlation": 0.49753210985780466, "reason": null},
    {"lag_seconds": 120.0, "available": true, "pair_count": 8, "correlation": 1.0, "reason": null}
  ],
  "best_lag_seconds": 120.0,
  "best_correlation": 1.0,
  "direction": "source_to_target"
}
```

`lags` 仅节录；实际按时滞升序给出从 `-max_lag_seconds` 到
`+max_lag_seconds` 的全部档位。不可用档位形如
`{"lag_seconds": 300.0, "available": false, "pair_count": 1,
"correlation": null, "reason": "insufficient_samples"}`；
两柜无重叠时所有档位的 `reason` 为 `no_overlap`，且
`grid_point_count=0`、`direction="unavailable"`。

### 冷凝风险查询

```bash
curl 'http://localhost:8080/api/v1/cases/CASE-007/condensation-risk?surface_temperature_c=10&margin_threshold_c=0&max_gap_seconds=600&min_duration_seconds=300&start=2026-09-01T00:00:00Z&end=2026-09-02T00:00:00Z'
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `surface_temperature_c` | 是 | — | 给定的展柜表面温度（°C，−50…100）；冷凝裕量 = 表面温度 − 露点 |
| `margin_threshold_c` | 是 | — | 冷凝裕量风险阈值（°C）：裕量 ≤ 该值视为风险（**含等于**） |
| `start` / `end` | 否 | 不限 | 含端点的 ISO8601 过滤；只基于过滤后范围内的分段线性曲线 |
| `max_gap_seconds` | 否 | `600` | 允许的最大采样间隔（秒，0 < x ≤ 86400）；超过则断开，未知区间不计风险 |
| `min_duration_seconds` | 否 | `0` | 风险窗口最短持续（秒，**≥ 0**，含等于）；更短的窗口不返回 |

露点由 Magnus 公式（`γ=ln(RH/100)+17.62T/(243.12+T)`，
`Td=243.12γ/(17.62−γ)`）逐点计算；**湿度为 0 的读数无效**，其相邻
线段强制断开。响应示例（空气温度 20→0→20°C、RH=100%，表面 10°C、
阈值 0，风险发生在裕量 ≤ 0 的两段，交点恰为各段中点）：

```json
{
  "device_sn": "CASE-007",
  "start": null,
  "end": null,
  "surface_temperature_c": 10.0,
  "margin_threshold_c": 0.0,
  "max_gap_seconds": 600.0,
  "min_duration_seconds": 300.0,
  "sample_count": 3,
  "window_count": 2,
  "windows": [
    {
      "start_ts": "2026-09-01T00:00:00Z",
      "end_ts": "2026-09-01T00:05:00Z",
      "duration_seconds": 300.0,
      "min_margin": -10.0,
      "min_margin_ts": "2026-09-01T00:00:00Z"
    },
    {
      "start_ts": "2026-09-01T00:15:00Z",
      "end_ts": "2026-09-01T00:20:00Z",
      "duration_seconds": 300.0,
      "min_margin": -10.0,
      "min_margin_ts": "2026-09-01T00:20:00Z"
    }
  ]
}
```

`start_ts` / `end_ts` 为裕量曲线进入/离开阈值的**精确插值时刻**；
`min_margin_ts` 必为某个有效读数时刻（裕量沿线段线性，并列取最早）。
采样间隔超限或湿度为 0 的区间断开，断档两侧窗口不合并；缺少必填参数、
`max_gap_seconds ≤ 0`、`min_duration_seconds < 0`、`start` 晚于 `end`
均返回 `422`，展柜没有读数返回 `404`。

---

## 配置（环境变量）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CASE_PORT` | `8080` | 监听端口（镜像内与宿主映射均固定 8080） |
| `CASE_DB_URL` | `sqlite:///./data/cases.db` | SQLAlchemy 数据库 URL |
| `CASE_DATA_DIR` | `./data` | SQLite 文件目录 |
| `CASE_PENALTY` | `4.0` | 默认惩罚项 β |
| `CASE_MIN_SEGMENT_SIZE` | `3` | 最小段长（锚点小区间自动放宽） |
| `CASE_ANCHOR_TOLERANCE` | `300` | 锚点对齐容差（秒） |
| `CASE_TEMP_SCALE` / `CASE_HUMIDITY_SCALE` | `2.0` / `5.0` | 温/湿度特征缩放 |
| `CASE_TEMP_WEIGHT` / `CASE_HUMIDITY_WEIGHT` | `1.0` / `1.0` | 联合代价权重 |
| `CASE_CONF_HIGH_SIGMAS` / `CASE_CONF_MEDIUM_SIGMAS` | `3.0` / `1.5` | 高/中置信 σ 阈值 |
| `CASE_CONF_MIN_MAGNITUDE` | `0.5` | 显著变化最小幅度（缩放空间） |

---

## 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

测试覆盖：变点检出与置信标记、惩罚项单调性、确定性、重复合并、迟到重放、
锚点强制边界、时间范围查询、**预览不写库**（含待预览读数与库内重复、
请求内时间戳重复时在内存副本上按 `report_count` 累计均值的回归用例），
**时间线降采样**（比例配额与余数稳定分派、强制点去重超预算返回 422 与
`minimum_required`、确定性 LTTB、`start` / `end` 过滤后的极值与预算），以及
**越界剂量**（线性插值交点精确性、三角/矩形剂量积分、`max_gap` 断开与未知
区间剔除、跨维度事件合并、参数校验 422 与空范围/未知设备），以及
**累计剂量预算**（窗末累计量与剩余额度、在线段内按越界幅度线性变化反解
耗尽时刻的三角/梯形闭式解、恰好在片段端点耗尽、零预算在首次越界起点耗尽、
断档不跨区间累计、阈值与预算成对提供等 422 组合、404 与空范围），以及
**跨展柜共模突变**（连续候选窗枚举、容差含等于、同展柜保留最大幅度/并列
取更早、`min_devices` 过滤、动态规划全局最优相对贪心链式归并的差异、
起始时间序列并列规则、严格互不重叠、方向判定、参数校验 422 与空库/空范围），以及
**锚点参考值校正时间线**（分维偏移独立成曲线、相邻锚点线性插值与首尾常数外推、
缺维参考值锚点不参与该维、同读数多锚点取最后一个有效参考值、先建曲线再按
start/end 过滤、无有效锚点偏移为 0、未对齐锚点忽略、只读不改库、404/422/空范围），以及
**跨展柜环境传播分析**（重叠时段固定步长网格与尾部不补点、相邻读数线性插值、
`max_gap` 断档点无效与等于连续、正负时滞枚举与时滞符号约定、只数成对有效点、
样本不足/常量序列标不可用且不产生 NaN、按 |r|→成对点数→|lag|→lag 择优、
方向判定、无重叠 200 unavailable、422/404 参数校验、确定性），以及
**冷凝风险**（Magnus 露点表值与单调性、裕量线性插值的进入/离开交点
精确性、阈值含等于、相接子区间合并与最低裕量（并列取最早）、`max_gap`
断档与等于连续、湿度为 0 强制断开且不计入露点、`min_duration` 含等于
过滤、断档两侧窗口不合并、参数校验 422、空范围 200、未知展柜 404、
确定性）。
