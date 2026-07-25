# Vibe-Trading OKX 永续合约量化交易详细设计

状态：Phase 1-3 已完成；Phase 4 控制面已完成，真实永续执行保持关闭
目标环境：OKX Demo Trading
首批标的：`BTC-USDT-SWAP`、`ETH-USDT-SWAP`
文档日期：2026-07-25

## 1. 背景与目标

Vibe-Trading 当前已经具备行情读取、研究智能体、回测、交易连接器、mandate 风控、审计和运行器等基础模块，但 OKX 连接器的下单路径仍是现货模型：下单固定使用 `tdMode="cash"`，数量只有通用的 `quantity/notional`，没有合约面值、保证金模式、持仓方向、杠杆、减仓、原生止盈止损和成交核验语义。

本设计将项目分阶段改造成可以交易 OKX USDT 本位永续合约的量化系统。第一阶段只开放 OKX Demo，验证执行闭环；在合约风险模型、故障恢复和长期仿真达到上线门槛前，不开放真实合约自主交易。

系统最终要实现以下能力：

- 连续读取 BTC/ETH 现货、永续、资金费率、持仓量和账户状态。
- 将策略信号转换成结构化、可审计的交易计划。
- 使用确定性代码完成仓位计算、风险审批、下单、减仓、平仓和止盈止损。
- 支持全仓/逐仓、单向/双向持仓模式以及显式杠杆设置。
- 使用客户端订单号、订单状态机和启动时对账处理超时、重启和重复调用。
- Demo 和 Live 使用不同凭据与能力开关；真实交易必须经过 mandate、熔断和人工授权。

## 2. 设计原则

### 2.1 LLM 不直接决定可执行参数

智能体可以解释市场、生成候选方向和给出交易理由，但不能绕过确定性风控直接构造经纪商请求。可执行请求必须经过以下流水线：

```text
market snapshot
  -> strategy signal
  -> trade plan
  -> deterministic risk checks
  -> normalized contract order intent
  -> OKX execution adapter
  -> broker verification
  -> reconciliation and audit
```

### 2.2 所有合约单位必须显式

合约的 `size` 不允许是没有单位的裸数字。统一使用：

- `contracts`：合约张数。
- `quote_ccy`：USDT 名义价值。
- `margin`：USDT 保证金成本，名义价值约为 `margin * leverage`。

执行层必须查询 OKX 产品元数据中的 `ctVal`、`ctValCcy`、`lotSz`、`minSz` 和产品状态后再换算，禁止写死 BTC 或 ETH 的每张合约面值。

### 2.3 写操作不自动重试

网络超时不等于下单失败。所有写操作必须携带稳定的 `client_order_id`，超时后先按该 ID 查询，不得直接重发。状态未知时进入 `UNKNOWN_NEEDS_RECONCILE`，暂停该标的的新订单。

### 2.4 平仓必须具有减仓语义

- 部分平仓使用反向订单并设置 `reduceOnly=true`。
- 整仓平仓使用 OKX `close_positions` 接口。
- 熔断时默认只阻止新单并取消挂单；是否整仓平仓必须由 mandate 明确授权。
- 不因为接口错误提示自动取消止盈止损、停止交易机器人或平掉仓位。

### 2.5 Demo 先行，Live 编译关闭

所有新合约写接口都会检查 `flag == "1"`。即使配置了 `okx-live-trade`、Demo 资格达标且人工审批完成，当前构建仍因 `LIVE_SWAP_WRITES_COMPILED = False` 拒绝真实永续写入。该常量没有环境变量覆盖路径，必须经过新的审计发布才能改变。现货接口保持原有兼容行为。

## 3. 改造前代码差距（基线）

本节记录第一阶段开始前的代码基线；当前实现状态见第 13 节。

| 模块 | 当前状态 | 合约化缺口 |
|---|---|---|
| `trading/connectors/okx/sdk.py` | 支持账户、持仓、订单、行情和现货下单 | 下单固定 `cash`；无产品元数据、杠杆、减仓、平仓、TP/SL、幂等和核验 |
| `trading/service.py` | 通用 SDK 下单，Live 经过 mandate | `OrderIntent` 是现货数量模型，无法正确计算合约名义价值和杠杆风险 |
| `tools/trading_connector_tool.py` | 通用查询、下单、撤单 | 参数无法表达保证金模式、持仓方向、数量单位和原生 TP/SL |
| `live/sdk_order_gate.py` | 按 `quantity * price` 估算风险 | 合约张数必须再乘 `ctVal`；减仓订单也不能简单按买卖方向估算总敞口 |
| `live/runtime/flatten.py` | 按有符号现货数量生成反向订单 | OKX 双向持仓需要保留 `posSide/mgnMode` 并强制 `reduceOnly` |
| `api/live_routes.py` | 只为 remote MCP 构造运行器 | SDK 连接器未接入；市场触发器写死为 `us_equity` |
| 依赖 | 主项目依赖 `ccxt` | `python-okx` 未在可选依赖中声明 |

## 4. 目标架构

### 4.1 市场数据层

职责：生成带时间戳和数据质量标记的只读快照，不执行交易。

首批数据：

- `BTC-USDT-SWAP`、`ETH-USDT-SWAP` K 线和最新价。
- 标记价格、指数价格和盘口。
- 当前/预测资金费率及历史分位数。
- 持仓量及变化率。
- 产品元数据、交易状态和价格/数量精度。

数据必须包含 `source`、`exchange_ts`、`received_at` 和 `stale`。订单风控使用 OKX 最新价/标记价；研究数据可以继续使用项目现有 OKX/CCXT 回退链，但回退数据不能用于确认真实订单状态。

### 4.2 策略层

策略输出 `TradePlan`，不输出经纪商 API 参数。建议结构：

```json
{
  "strategy_id": "btc_trend_v1",
  "signal_id": "20260725T120000Z-BTC-1h",
  "instrument": "BTC-USDT-SWAP",
  "action": "open_long",
  "confidence": 0.72,
  "reference_price": 118000,
  "invalidation_price": 115500,
  "take_profit_price": 123000,
  "max_holding_seconds": 14400,
  "evidence": ["ema20_above_ema60", "oi_rising", "funding_not_extreme"]
}
```

同一个 `signal_id` 只能产生一次开仓意图。策略层必须支持 `no_trade`，且缺数据、数据过期或指标冲突时默认不交易。

LLM 研究结果只能作为 `evidence` 或策略开关输入；核心开平仓条件必须能在无 LLM 的情况下重复计算和回测。

### 4.3 组合与仓位层

仓位计算以止损风险而不是“固定 10 倍满仓”为核心：

```text
risk_budget_usdt = account_equity * risk_per_trade
stop_distance_ratio = abs(entry - stop) / entry
fee_and_slippage_buffer = configured_buffer
target_notional_usdt = risk_budget_usdt /
                       (stop_distance_ratio + fee_and_slippage_buffer)
margin_required_usdt = target_notional_usdt / leverage
```

之后再应用以下上限，取最小值：

- 单笔最大名义价值。
- 总名义敞口上限。
- 单标的敞口上限。
- 可用保证金和预留保证金比例。
- OKX 最大可下单数量和产品档位限制。
- 策略自身的容量限制。

10 倍杠杆是最大值或目标保证金效率，不是把风险预算自动乘十。没有有效止损的开仓计划直接拒绝。

### 4.4 确定性风险引擎

每次开仓前至少检查：

1. 环境是否为预期的 Demo/Live，账户 UID 是否匹配。
2. 产品是否在白名单、状态是否可交易、是否为 USDT 线性永续。
3. 行情是否新鲜，买卖价差和预估滑点是否在上限内。
4. 数量单位是否明确，换算后是否满足 `lotSz/minSz`。
5. 当前持仓模式、保证金模式、持仓方向和订单方向是否一致。
6. 真实当前杠杆是否等于交易计划确认的杠杆，且不超过策略/mandate 上限。
7. 止损方向是否正确，强平距离是否留有安全缓冲。
8. 单笔风险、总敞口、同向相关敞口、日内成交次数是否合规。
9. 当日已实现亏损、净值回撤、连续亏损是否触发熔断。
10. 同一 `client_order_id` 是否已经存在。

减仓订单仍检查产品、方向、持仓和数量，但不能因为常规开仓敞口上限而阻止风险降低操作。它必须确认 `reduceOnly=true` 且数量不超过目标仓位。

### 4.5 执行适配器

首批接口：

- `get_swap_instrument`：读取并规范化合约元数据。
- `get_account_configuration`：读取账户级别和 `posMode`。
- `get_swap_leverage`：读取实际杠杆。
- `set_swap_leverage`：设置杠杆并回读验证。
- `place_swap_order`：市价/限价开仓或 `reduceOnly` 部分平仓。
- `close_swap_position`：整仓市价平仓并回读持仓验证。
- `get_order`：按 `ordId/clOrdId` 核验订单。

第一阶段只支持：

- `BTC-USDT-SWAP`、`ETH-USDT-SWAP`。
- USDT 线性永续。
- `market`、`limit`。
- `cross`、`isolated`。
- `net_mode`、`long_short_mode`。
- 附加的 OKX 原生止盈止损，触发价格类型为 `last/index/mark`。

暂不支持追踪止损、冰山、TWAP、期权、币本位反向合约和组合保证金自主交易。

### 4.6 合约数量换算

对 `ctValCcy` 为基础币种的 USDT 线性永续：

```text
one_contract_notional_usdt = ctVal * reference_price

contracts_from_quote = floor_to_lot(
    requested_quote_usdt / one_contract_notional_usdt,
    lotSz
)

contracts_from_margin = floor_to_lot(
    requested_margin_usdt * verified_leverage /
    one_contract_notional_usdt,
    lotSz
)
```

市价单使用最新成交价作为换算参考，限价单使用限价。换算结果小于 `minSz` 时拒绝，不自动放大；显式 `contracts` 不是 `lotSz` 的整数倍时拒绝，不静默修改用户指定的张数。

返回结果同时包含：请求单位/数量、实际合约张数、换算参考价、估算实际名义价值、`ctVal` 和 `lotSz`，使审计可以复算。

### 4.7 持仓模式规则

`net_mode`：

- 调用方仍需显式确认 `position_side="net"`。
- 发送给 OKX 时省略 `posSide`。
- 部分平仓必须设置 `reduceOnly=true`。

`long_short_mode`：

- 每个订单必须显式提供 `position_side="long"` 或 `"short"`。
- 开多：`buy + long`；减多：`sell + long + reduceOnly`。
- 开空：`sell + short`；减空：`buy + short + reduceOnly`。
- 方向组合不一致时在本地拒绝，不发送请求。

杠杆设置规则：

- 单向模式使用 `position_side="net"`。
- 双向逐仓分别设置 `long` 和 `short`。
- 双向全仓使用 `position_side="both"`，因为 OKX 全仓杠杆对两侧共同生效。

### 4.8 止盈止损

开仓订单可以通过 `attachAlgoOrds` 附带交易所原生止盈止损。默认触发后用市价执行，即 `tpOrdPx/slOrdPx = -1`；触发价格类型默认 `mark`，降低短时成交价尖刺的影响。

本地校验：

- 多头：`stop_loss < reference < take_profit`。
- 空头：`take_profit < reference < stop_loss`。
- `reduceOnly` 平仓单不能再附加新的 TP/SL。
- TP/SL 的客户端算法订单号从主订单号稳定派生，仍满足 OKX 32 字符限制。

下单成功不代表附加算法单一定有效。核验结果必须回传订单详情；后续阶段还要查询 algo order 列表并确认 TP/SL 状态。

## 5. 订单状态机与幂等

```text
PROPOSED
  -> VALIDATED
  -> SUBMITTING
  -> ACCEPTED_UNVERIFIED
  -> OPEN | PARTIALLY_FILLED | FILLED
  -> CANCELLED | REJECTED

SUBMITTING or ACCEPTED_UNVERIFIED
  -> UNKNOWN_NEEDS_RECONCILE
```

规则：

- `client_order_id` 由策略运行 ID、标的、信号时间和动作散列生成，重启后不变。
- 下单前按 `client_order_id` 查询。已存在则返回原订单并标记 `idempotent_replay=true`。
- 查询接口不可用时拒绝新单，不能以“可能不存在”为由继续发送。
- OKX 接受写请求后立即查询订单。查询暂时不可见时返回“已接受但核验未知”，明确 `retry_safe=false`。
- 对账任务通过订单、成交和持仓三方恢复最终状态，禁止自动重发未知订单。

## 6. 对账、审计与故障恢复

### 6.1 启动时对账

运行器启动后、调用策略前依次读取：

1. 账户配置与 UID。
2. 非零持仓。
3. 普通挂单。
4. TP/SL 和其他算法挂单。
5. 最近成交。
6. 本地最后确认的订单/持仓状态。

任何未知成交、方向不一致、孤儿止损或提交状态不明的订单都会阻止自动交易，并要求人工处理。

### 6.2 审计记录

每次写操作记录：

- 策略、信号、运行 ID 和客户端订单号。
- 原始交易计划与规范化订单意图。
- 产品元数据快照和换算过程。
- 风控输入、命中的所有上限与最终决定。
- 发给 OKX 的请求（凭据永不记录）。
- OKX 接受响应和后续核验结果。
- 关联订单、成交、持仓和 TP/SL 标识。

### 6.3 熔断

触发条件包括数据过期、对账不安全、连续接口错误、订单未知、日亏损超限、回撤超限和人工 HALT。熔断动作顺序：

1. 阻止新开仓。
2. 保留查询和人工撤单能力。
3. 是否取消挂单由明确策略配置决定。
4. 是否平仓由 `flatten_on_halt` 明确授权；执行时使用合约专用减仓/平仓语义。

系统绝不能根据 OKX 错误信息自动取消 TP/SL、停止交易机器人或平仓。

## 7. 配置与权限

凭据继续存放在用户运行目录的 `okx.json`，Demo/Live 使用 OKX 各自独立的 API Key。建议配置扩展：

```json
{
  "profile": "paper",
  "api_key": "...",
  "api_secret": "...",
  "passphrase": "...",
  "expected_uid": "...",
  "allowed_swap_instruments": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
  "max_swap_leverage": 10
}
```

API Key 最小权限：

- Demo：Read + Trade，不授予 Withdraw。
- Live：Read + Trade，不授予 Withdraw，绑定 IP，并使用专用子账户。

Phase 2 已将 mandate 升级为 schema v2，使其显式理解：

- 合约名义价值和保证金价值。
- 开仓与减仓动作。
- `reduceOnly`、`posSide`、`mgnMode`。
- 每标的/每方向杠杆。
- 维持保证金率、强平缓冲和每日亏损上限。

永续专用 gate 只接受 schema v2，并按 `contracts * ctVal * reference_price` 计算名义价值。旧 schema v1 现金 mandate 仅保留在通用现金订单 gate 中，不可用于永续。

## 8. 智能体工具契约

新增 OKX 专用工具，不扩散通用现货工具的参数语义：

- `okx_swap_instrument`：只读产品元数据。
- `okx_swap_account_config`：只读账户模式。
- `okx_swap_leverage`：只读当前杠杆。
- `okx_swap_set_leverage`：Demo 写操作。
- `okx_swap_place_order`：Demo 下单/部分减仓。
- `okx_swap_close_position`：Demo 整仓平仓。

每个写工具都要求调用方显式提供连接配置、标的、动作所需数量及单位、保证金模式、持仓方向、杠杆和客户端订单号。下单工具还显式接收 TP/SL；不从自然语言的模糊“500U”自行猜测是名义价值还是保证金。

## 9. 量化运行器

生产运行器不应只按固定间隔向通用聊天智能体发送提示。建议新增 `CryptoStrategyRunner`，每个 tick 固定执行：

```text
heartbeat
-> halt/mandate check
-> account and order reconciliation
-> market data freshness check
-> deterministic strategy evaluation
-> portfolio/risk evaluation
-> execution (zero or one intent per instrument)
-> broker verification
-> metrics/audit
```

同一标的同一 tick 最多一个写意图。策略运行器与研究智能体通过结构化 `TradePlan` 交互，不解析自由文本交易指令。

调度需要 24x7 `crypto` 触发器，而不是当前硬编码的 `us_equity` 市场时间。维护窗口、资金费率结算窗口和 OKX 系统状态应作为额外暂停条件。

## 10. 回测与仿真要求

回测必须覆盖永续特有成本：

- maker/taker 手续费。
- 资金费率收付。
- 市价滑点和盘口深度。
- 杠杆保证金占用。
- 维持保证金和强平。
- TP/SL 触发价格类型和 K 线内先后顺序的不确定性。

上线门槛建议：

- 无前视偏差和幸存者偏差检查通过。
- 走样本外和滚动窗口验证。
- 至少 30 天 Demo 运行，无重复单和不可解释持仓。
- 网络超时、进程重启、API 限流、订单部分成交和 websocket 断线演练通过。
- 最大回撤、日亏损、资金费率和滑点压力测试符合 mandate。

## 11. 分阶段实施

### Phase 1：Demo 执行基础（已完成）

- 声明 `python-okx` 可选依赖。
- 产品元数据、账户模式和杠杆读取。
- 杠杆设置及回读核验。
- 显式单位换算、market/limit、cross/isolated、net/hedge。
- `reduceOnly` 部分平仓、整仓平仓、原生 TP/SL。
- 客户端订单号预查重和下单后核验。
- 服务层、智能体工具和模拟 SDK 单元测试。
- 所有合约写操作强制 Demo。

### Phase 2：合约风险与状态存储（已完成）

- mandate schema v2 和正确的合约名义敞口模型。
- 订单/成交/持仓/算法单的持久化状态机。
- 合约专用 reconciliation 和 halt flatten。
- 日亏损、回撤、强平缓冲和相关性上限。

### Phase 3：确定性策略运行器（已完成）

- BTC/ETH 首个可回测策略及统一 `TradePlan`。
- 24x7 crypto 调度、数据新鲜度和系统维护检测。
- 回测、shadow、Demo 使用同一策略与仓位代码。
- 监控、告警和人工操作面板。

### Phase 4：受控 Live 控制面（已完成，执行未开放）

- 专用 OKX 子账户、小额资金和 IP 白名单。
- 人工提交 mandate，逐笔确认冷启动。
- 至少 30 天 Demo、30 个活跃 UTC 日、1000 个健康 tick 和五类故障演练全部达标后，才允许记录人工审批。
- 任何未知订单或对账差异立即熔断，不自动恢复。
- 当前发布只计算资格和记录审批，`execution_enabled` 固定为 `false`。

## 12. 第一阶段验收标准

- 现有 OKX 现货下单测试继续通过。
- 未安装 `python-okx` 时返回清晰错误，不影响其它功能。
- Live 配置调用新的合约写接口会在 SDK 写调用前被拒绝。
- 非 BTC/ETH USDT 永续、反向合约、停牌产品和未知元数据被拒绝。
- 三种数量单位均可被确定性换算或明确拒绝。
- 单向/双向模式与订单方向不一致时不发送订单。
- 杠杆值不匹配时不发送订单；设置杠杆后必须回读。
- 部分平仓始终携带 `reduceOnly`；整仓平仓使用专用接口。
- 主订单可携带原生 TP/SL，方向错误会被本地拒绝。
- 相同客户端订单号不会重复发送。
- 写请求接受后有结构化核验结果，并明确标识不可安全重试的未知状态。
- 单元测试全程使用模拟 SDK，不读取真实凭据、不访问 OKX 账户、不发送订单。

## 13. 第一阶段实现与使用

### 13.1 已实现范围

第一阶段已完成以下代码路径：

- `trading/connectors/okx/swap.py`：精确 Decimal 换算、产品元数据校验、显式数量单位、持仓方向、杠杆和 TP/SL 校验。
- `trading/connectors/okx/sdk.py`：产品、账户模式和杠杆读取；Demo 杠杆设置、市价/限价、部分减仓、整仓平仓、原生 TP/SL、幂等预查与写后核验。
- `trading/service.py` 和 `tools/trading_connector_tool.py`：六个 OKX 永续专用服务/智能体工具。
- `vibe-trading connector configure okx-paper-trade`：通过隐藏输入或环境变量保存 Demo 凭据，命令行不接受 secret 参数。
- `python-okx>=0.4.3,<0.5`：作为 `okx` 可选依赖声明。

所有新合约写路径仍在 SDK 调用前强制检查 Demo 标志。`okx-live-trade` 不具备合约写能力；资格和人工审批也不会改变这一点。

### 13.2 安装与 Demo 配置

安装项目及 OKX 可选依赖：

```powershell
pip install -e ".[okx]"
```

在 OKX 创建独立的 Demo Trading API Key，只授予 Read 和 Trade，不授予 Withdraw。不要在聊天、命令参数或日志中粘贴凭据。交互配置可直接运行：

```powershell
vibe-trading connector configure okx-paper-trade `
  --swap-instruments BTC-USDT-SWAP,ETH-USDT-SWAP `
  --max-swap-leverage 10
```

非交互配置只通过当前进程环境传递 secret：

```powershell
$env:OKX_API_KEY = "<demo-api-key>"
$env:OKX_API_SECRET = "<demo-api-secret>"
$env:OKX_PASSPHRASE = "<demo-passphrase>"
vibe-trading connector configure okx-paper-trade --yes
```

配置保存在用户运行目录的 `okx.json`。建议同时设置 `--expected-uid`，把连接器固定到预期 Demo 账户。

### 13.3 受限网络代理

OKX 本身不要求本地代理，但网络无法直连 `https://www.okx.com` 时，Python SDK 需要 HTTP(S) 代理。连接器按以下优先级读取：持久化配置、`OKX_PROXY`、`HTTPS_PROXY`、`https_proxy`、`ALL_PROXY`、`all_proxy`。

Windows PowerShell 示例：

```powershell
$env:OKX_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = $env:OKX_PROXY
curl.exe -x $env:OKX_PROXY https://www.okx.com/api/v5/public/time
```

代理端口必须替换为本地代理软件实际的 HTTP/Mixed 端口。修改环境变量后需要从同一个终端重启服务。

### 13.4 连接验证

配置完成后先选择并检查 Demo profile：

```powershell
vibe-trading connector use okx-paper-trade
vibe-trading connector check okx-paper-trade
```

智能体可使用的合约工具为：

- `okx_swap_instrument`
- `okx_swap_account_config`
- `okx_swap_leverage`
- `okx_swap_set_leverage`
- `okx_swap_place_order`
- `okx_swap_close_position`

任何写调用都必须明确标的、数量及单位、保证金模式、持仓方向、杠杆和稳定的客户端订单号。USDT 数量不得省略 `quote_ccy`（名义价值）或 `margin`（保证金成本）语义。

智能体返回永续 mandate 候选卡后，选择 profile 并进入确认框。包含 `perpetual_swap` 的 profile 会强制要求填写“专用合约账户引用”，OKX Demo 应填写与连接器 `--expected-uid` 相同的 Demo UID；该值只是账户绑定标识，不是 API Key、Secret 或 Passphrase。空值时确认按钮不可用，后端核心 writer 也会在写入 mandate/consent 文件前 fail-closed，且保留 proposal 供修正后重试。非永续 mandate 的原有提交流程不受影响。

### 13.5 验证记录

- Phase 1-4 测试使用模拟 SDK 和临时运行目录。
- 验证过程不读取真实凭据、不访问 OKX 账户、不发送 Demo 或真实订单。
- 2026-07-25 后端回归：排除 Windows 不支持的 factor registry symlink 测试，并 deselect POSIX `0600` 权限断言后，`5093 passed, 11 skipped, 1 deselected`。
- 本次变更涉及的 39 个 Python 文件通过 Ruff；前端全量测试 `246 passed`，TypeScript 与 Vite 生产构建通过。
- `GET /runtime` 和 `GET /quant/okx/status` 均返回 HTTP 200；复核时 runner 已停止、breaker 未触发、`live_swap_writes_compiled=false`、`execution_enabled=false`。
- 当前执行环境没有可连接的内置浏览器实例，因此未声称完成桌面/移动端截图和控制台可视验证；页面布局由 Runtime 组件测试、类型检查、生产构建和 HTTP 健康检查覆盖。

## 14. Phase 2 实现：风险、状态和对账

### 14.1 Mandate schema v2

永续 mandate 除通用单笔、总敞口、杠杆和日内次数外，必须显式持久化：

- `max_position_notional_usd`
- `max_correlated_exposure_usd`
- `max_daily_loss_usd`
- `max_drawdown_pct`
- `min_liquidation_buffer_pct`
- `max_margin_utilization_pct`
- `max_consecutive_losses`
- `require_stop_loss=true`
- `include_symbols`

loader 会拒绝缺字段、非有限数值、错误布尔类型、broker 不匹配、无时区时间、空 symbol allowlist 和非 SHA-256 consent token 的 v2 永续 mandate。`NaN`/`Infinity` 不能进入比较路径或落盘。

### 14.2 持久化状态机

`live/runtime/swap_state.py` 在每次写调用前原子落盘 `SUBMITTING`，写后根据核验结果推进状态。任何超时或持久化不确定性进入 `UNKNOWN_NEEDS_RECONCILE`，写入 `swap_circuit_breaker.json`，且 `retry_safe=false`。

对账固定读取六个面：账户配置、余额、精确持仓、普通挂单、成交和算法单。持仓身份为 `symbol + posSide + mgnMode`，不会把双向持仓错误合并。冷启动发现未记录订单、成交、非零持仓或算法单时 fail-closed。

### 14.3 Breaker 和 flatten

breaker 不会自动恢复。人工清除前必须停止 quant runner，并完成一次新的六面只读对账；只有 `is_safe=true` 才移动 breaker 文件并记录操作者与说明。

HALT flatten 会先取消普通订单和算法单；任一取消未确认时不继续自动平仓。合约整仓平仓保留 `posSide`、`mgnMode` 和杠杆上下文，调用专用 `close_positions`，不会退化成现货反向订单。

## 15. Phase 3 实现：策略、仓位和运行器

`trading/quant/` 提供统一的 `TradePlan`、BTC/ETH EMA 趋势突破策略、成本模型、仓位计算、回测和 24x7 runner。核心路径不调用 LLM，也不解析自由文本订单参数。

每个 tick 的固定顺序为：

```text
heartbeat / HALT / breaker / mandate v2
-> six-surface reconciliation
-> OKX maintenance check
-> confirmed 15m bars and freshness check
-> deterministic strategy
-> fresh bid/ask quote and execution-level revalidation
-> shared sizing and mandate caps
-> at most one write intent per instrument
-> SDK verification, durable state, audit and metrics
```

Shadow、Demo 和回测共享相同仓位函数。Shadow 会实际走产品元数据、最新报价、风险快照和 `ctVal` 仓位换算，只跳过写调用。回测在下一根 K 线开盘成交，入场当根 K 线立即检查强平、止损和止盈；同一根 K 线多条件命中时按对策略最不利的顺序处理。手续费按双边、滑点按不利成交价、资金费按持有时间计入。

## 16. Phase 4 控制面和操作接口

### 16.1 只读状态

```text
GET /quant/okx/status
```

返回 runner、最近 tick、breaker、Demo 资格、人工审批和编译级 Live gate。前端 Runtime 页面轮询该接口，只展示状态，不提供下单、启停、breaker 清除或审批按钮。

### 16.2 Shadow / Demo runner

```text
POST /quant/okx/runner/start
{"mode":"shadow","tick_interval_seconds":60}

POST /quant/okx/runner/stop
```

启动前必须存在未过期的 OKX v2 永续 mandate，标的必须落在显式 allowlist，HALT 和 breaker 必须清除。`shadow` 不写订单；`demo` 可能向 OKX 模拟盘发送订单，因此只能在用户主动调用后运行。服务关闭时会停止内存中的 quant scheduler。

### 16.3 人工 breaker 清除

```text
POST /quant/okx/breaker/clear
{"authorized_by":"operator","note":"reconciliation reviewed"}
```

runner 运行中、只读对账失败或存在任一 issue 时返回 `409`，breaker 保持触发。写操作本身不重试。

### 16.4 Demo 资格与人工审批

```text
POST /quant/okx/drills
POST /quant/okx/promotion/approve
```

资格账本为哈希链 JSONL，并在并发追加时加进程内锁和 `fsync`。只有 `completed` 的真实 Demo tick 或显式 `healthy=true` 的内部 checkpoint 才计入 1000 tick 和活跃日；blocked、数据错误和维护暂停不能凑门槛。

不可降低的基线为：真实经过 30 天、30 个活跃 UTC 日、1000 个健康 tick、最近 tick 不超过 24 小时，以及网络超时、进程重启、API 限流、部分成交、WebSocket 断线五类演练全部通过。未知订单、重复订单、不可解释持仓、危险对账、保护单缺失或风险限额事件会使当前窗口不合格。

人工审批绑定当前 mandate consent hash、资格账本 head、专用子账户 UID、IP 白名单、禁用提现和不超过所有 mandate 名义价值上限的 Live cap。审批最多有效 7 天；mandate 到期或账本继续推进后必须重新审批。当前构建始终附加：

```text
live_swap_writes_compiled = false
execution_enabled = false
```

因此 Phase 4 的完成含义是“资格和人工控制面可审计”，不是“已开放实盘”。
