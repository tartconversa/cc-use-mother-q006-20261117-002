# CycleFlow 机器人样机迭代数据服务

CycleFlow 面向样机研发团队，接收设计修订、部件批次、生产工单和质量试验等独立来源的事件。SQLite 同时保存原始事件身份和派生的业务状态。报表将设计版本、工单、工序耗时、物料批次、试验结果和质量问题连接起来，供团队解释每次迭代的交付效率。

## 运行

依赖版本锁定在 `requirements.txt`。在项目目录运行 `python -m unittest discover -s tests -v` 执行业务测试，`python -m compileall -q cycleflow` 检查源码，`python -m cycleflow.demo` 运行合成数据验收。安装所列依赖后，可设置 `CYCLEFLOW_DB` 为可写的数据库路径，再运行 `uvicorn cycleflow.api:app` 启动 HTTP 服务。命令行帮助为 `python -m cycleflow --db cycleflow.sqlite3 --help`。

演示仅使用临时数据库和本地合成产品。它分两次导入同一清单，验证试验良率，冻结月报，并审计存储的状态链；随后运行迭代治理场景：跨周迟到试验、失败尝试与返工组成的尝试链、窗口边界重定义的影响分析和目标口径漂移。正常运行无需访问外部服务。

## 事件协议

向 `/events` 提交 `source`、`external_id`、`kind`、带时区的 `occurred_at` 和对象类型的 `payload`。相同事件重放返回既有编号；相同身份提交不同内容返回冲突。缺少前置对象或暂时缺料的事件进入待处理状态，可调用 `/events/{id}/retry` 补齐后重试。其他业务无效事件仍保留拒绝原因。批量接口每次使用稳定的批次编号和完整事件数组；系统核对清单指纹，从已提交的行继续。

事件类型包括 `design.release`、`design.retire`、`lot.receive`、`lot.quarantine`、`job.open`、`job.stage_start`、`job.stage_finish`、`job.scrap`、`plan.create` 和 `test.record`。设计包含产品、版本、部件数量及可选的父版本。部件批次包含料号、供应商和正整数数量。工单绑定一个设计和产量，成功预留物料后依次经过加工、装配和集成。试验计划绑定精确的产品版本，受测数量必须覆盖该工单全部单位。

治理事件 `goal.define`、`window.define` 和 `window.redefine` 与业务事件走同一账本，因此窗口和目标的每次变更都有来源事件编号、可重放、可审计。

## 迭代窗口与目标口径

周度复盘的分歧（同一工单在周报和完工报表里落入不同周、返工被算成新交付、迟到试验改写已发周报）通过三条仅追加的版本链解决：

- **目标口径（goal）**：`goal.define` 建立产品级目标（标题、最低良率、可选绑定的试验计划）。同一 `goal_id` 再次提交即生成新版本，旧版本永久保留；报表按窗口校准的目标版本解释，事后收紧阈值不会改写任何已冻结的周报。
- **迭代窗口（window）**：`window.define` 登记团队选定的 `[window_start, window_end)` 边界并校准一个目标版本；同产品活动窗口不得重叠。`window.redefine` 带变更原因生成新版本，原边界仍可按版本号取回。
- **归属规则只有一条**：工单按 `opened_at` 落入哪个窗口就归属哪个窗口。完工和迟到试验永不移动工单，而是进入 `cross_week`：周五集成、下周一试验表现为 `carried_over`/`incoming` 记录，跨周等待在同一窗口内可解释。

`GET /iterations/{product}/weekly` 返回按窗口组织的周度视图。返工关系以根工单聚成**尝试链（attempt chain）**：失败的原始尝试与后续返工是同一条链的多次 `attempts`，`rework_attempts` 单列，`distinct_qualified_deliveries` 每条根链最多计一次，返工不会再被当成新的独立交付。视图还给出 `pending_quality`（仍待试验或无法下质量结论的工单）和 `trace` 溯源块，可从周报追到窗口/目标的治理事件、设计、工单与各工序和试验的来源事件编号。

`GET /iterations/windows/{window_id}/impact?from_version=...` 在边界变更时重放新旧两个边界，明确返回 `moved_in`/`moved_out`/`stayed` 工单（含新归属窗口或 unassigned）、受影响工单上仍待确认的质量结论，以及匹配旧边界的已冻结发布；冻结发布只被列出，原报告和指纹不变，按新口径重新发布须使用新的 `release_id`。`GET /iterations/{product}/goal-drift` 对照每个窗口校准的目标版本与当前目标版本，标记 `calibration_stale` 并列出该窗口下的冻结发布。其他端点：`GET /iterations/{product}/windows`、`GET /iterations/windows/{window_id}?version=`、`GET /iterations/{product}/goals`、`GET /iterations/goals/{goal_id}?version=`。CLI 对应 `iterations`、`windows`、`impact`、`goal-drift` 子命令。

## 报表与发布

请求 `/reports/{product}?window_start=...&window_end=...` 获取当前视图。时间窗含起点、不含终点，以工单创建时间决定归属。报表提供按版本汇总、良率、工序时长、等待时长、来源事件编号和质量问题。周度、返工分组、瓶颈和供应商视图使用相同的已接受事实。供应商试验归因可以重叠，不能跨供应商直接求和。

向 `/releases` 提交发布编号、产品、时间窗和预览返回的 `expected_fingerprint`。未完成工单或窗口内未解决事件阻止发布。发布后保留原报告和来源事件头；`/releases/{id}/freshness` 比较当前视图与冻结视图，不改写历史。`/quality/audit` 检查事件身份、设计版本链、物料预留、工序顺序、试验记录和发布摘要。
