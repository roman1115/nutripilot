# 饮食管理验收矩阵

日期：2026-11-12，2026-11-13收尾。**结论：D01–D09本地功能验收完成，W01–W08完成本地范围交付。**
依据：[实施基线](diet-management-implementation-spec-2026-11-11.md)、
[实施记录](diet-management-progress-2026-11-12.md)。不改变原范围或放宽硬门槛。

## 最新真实本地证据

最新统一证据见[真实本地验收](diet-real-local-acceptance-2026-11-12.md)。
最终工程963项、零跳过；千问文字60/60与24/24回归、照片固定30项管线、
真实聊天17/17组、真实照片与聊天双尺寸页面、新库0007及API/Worker重启回读均通过。
图片按执行前冻结的粗粒度主食物槽位评价：语义27/29、字面26/29；
不是全部配菜precision/recall或营养准确率。两处识别错误及早期失败全部保留。
生产部署、原业务库维护切换、异机备份/加密/30天实际销毁不属于本轮本地范围。

| 交付 | 当前本地验收证据 |
| --- | --- |
| D01 记录与个人食物 | A01/A11：手动多食物、未知/区间、个人资料/标签/独立过敏原依据、来源回读 |
| D02 文字与图片 | A07/A08/A12：真实文字/照片、持久任务、编辑确认、失败恢复、零自动入账 |
| D03 周餐单 | A02/A11：28餐、单餐/整周复制冲突、编辑/归档恢复，真实聊天创建/查询 |
| D04 计划转实际 | A02/A03：稳定事件幂等、明确再记、撤销及两尺寸页面 |
| D05 更正与撤销 | A03/A09：字段更正、日期/时区差异、软删恢复/永久清除及重复批准 |
| D06 目标 | A04：生效时间线、历史/未来/停用、旧入口适配，真实聊天确认/查询 |
| D07 分析 | A05/A13：日周区间/未知/分母/偏差、覆盖天数，真实汇总/偏差查询 |
| D08 聊天 | A06：十工具、C01–C24工程覆盖、17组真实子集、多轮草稿及两种明确确认 |
| D09 数据管理 | A08–A10/A14：隔离、私图、导出、留存/清除、PG与媒体/删除日志联合恢复 |

## 餐照调整与本地范围（早期历史）

xerina明确普通餐照为粗略识图估算，不要求精确重量/营养或置信度阈值，且当前不部署。
对应基线第1/3/11/17节已同步；本节保留此前证据，生产环境条件不再计入当前
本地功能阻塞。当时真实识图/多轮语义与本地持久链路仍未全部验收，已由上方最终证据补齐。

- 新增照片估算链路后全量**932 passed, 1 warning in 99.94s，零跳过**，外部服务、
  恢复和负载均启用。之后只补一项回归，最新照片专项**6 passed, 1 warning in 3.15s**。
- `test_diet_image_estimates`验证无目录匹配/称重数据仍保存、营养未知可保存、份量更正重算、
  一次确认/重放不重复、本人隔离、模型来源不升级、拆分按事件关联、采用建议及改名失效。
  读取[合成输出样例](specs/diet-image-estimate.example.json)，不作为真实营养标注。
- Ruff/mypy131文件/build通过，JS372.39kB/gzip110.04kB；工具Schema仍29接受/21拒绝。
- `verify_diet_extended_web.py`两尺寸完整通过，新增照片估算的编辑/预览/保存/刷新；
  已查看390编辑和390/1440预览截图，文字无重叠，确认前清楚说明为粗略估算。
  证据`.runtime/diet-acceptance-20261112/image-estimates/`，宿主使用Fake和白图，仅工程证据。
- 当时未执行真实千问视觉或HowToCook照片验收，不能用该阶段Fake结果声称模型效果通过。

## 最终工程检查

```bash
NUTRIPILOT_TEST_EXTERNAL=1 NUTRIPILOT_TEST_BACKUP=1 \
NUTRIPILOT_TEST_DIET_LOAD=1 \
NUTRIPILOT_TEST_DATABASE_URL=postgresql+asyncpg://nutripilot:nutripilot@127.0.0.1:5432/nutripilot_acceptance_20261112 \
.venv/bin/pytest -q -o addopts='--strict-markers --disable-warnings'
# 963 passed, 1 warning in 106.20s；无跳过。

.venv/bin/ruff check .
.venv/bin/mypy src/nutripilot scripts/verify_diet_web.py scripts/verify_diet_plan_web.py \
  scripts/verify_diet_privacy_web.py scripts/verify_diet_extended_web.py \
  scripts/verify_diet_chat_web.py scripts/verify_diet_text.py scripts/verify_diet_images.py \
  scripts/verify_diet_real_chat.py scripts/verify_diet_real_web.py scripts/backup_diet.py
# Success: no issues found in 134 source files.
npm --prefix web run build
.venv/bin/python docs/specs/check_diet_contract.py
```

963项包含原功能回归，不是963个新增饮食场景。1条Starlette TestClient依赖弃用警告。
PG随机Schema和备份命令临时DB均独立创建/清理；不升级原业务库，不重启原8006/5178。
隔离验收库迁移0007已运行，原业务库只读核对仍0003。

最终前端build：JS372.43kB/gzip110.05kB、CSS57.39kB。以下为此前页面收尾过程：
修复草稿更新提示被全局grid覆盖的竖排，extended两尺寸完整复跑通过，
新增提示宽高断言并查看390/1440截图；数据管理加载状态、刷新代次和未读分页提示修复后，
privacy两尺寸下载/翻页/清除复跑通过。最后build通过（JS372.01kB/gzip109.89kB，CSS57.39kB），
Ruff再次通过；927项为该阶段历史，后续后端修改已包含在最终963项回归中。

## A01–A14

| ID | 实际证据 | 状态与剩余条件 |
| --- | --- | --- |
| A01 | test_diet_quantities、test_diet_summary、test_diet_labels；固定Decimal/区间/标签换算 | 工程通过；不声称视觉识别数值正确 |
| A02 | test_diet_plans、test_diet_api；28餐、复制冲突、计划转实际幂等/再吃/撤销 | 工程通过 |
| A03 | test_diet_transactions、test_diet_persistence；回执失败回滚、12并发确认；durable/SIGKILL | 工程通过；故障夹具不代表每种生产故障都覆盖 |
| A04 | test_diet_targets、test_graph_resource_api、PG epoch检查；真实目标准备/确认/查询 | 本地通过；原业务最终切换未执行 |
| A05 | test_diet_summary、test_diet_quantities；无记录/未知/周分母/偏差 | 工程通过，页面同日流程有证据 |
| A06 | 下方C矩阵、最终963工程回归、chat-matrix-v10真实17组、真实双确认浏览器 | 本地通过；故障夹具与真实语义子集分开报告 |
| A07 | test_diet_jobs、test_durable_processes；租约/取消/迟到/绝对期限/保留预算 | 工程通过；真实供应商中断成本未核验 |
| A08 | test_diet_media、purge_export、export_streaming；格式/像素/EXIF/越权/孤儿；30项真实视觉 | 本地通过；API/Worker共用私有目录，部署卷配置不在本轮范围 |
| A09 | API/PG隔离、purge_export/resource_purge、联合恢复/独立日志 | 本地通过；不代表生产加密、异机副本或实际30天销毁 |
| A10 | test_diet_persistence迁移预检/重跑/非法旧行、新库实际0004–0007与旧入口回归 | 本地演练通过；原业务停写备份及最终切换未执行 |
| A11 | 两尺寸手动/计划/隐私/extended；最终真实browser-v3与chat-browser-final-v2 | 通过；照片可见、编辑/刷新、双确认不重复、冲突/断网保留输入 |
| A12 | 固定60文字+独立24回归；固定HowToCook30项，原来源/哈希/失败保留 | 本地粗估口径通过：文字P/R1.0；29主食物槽位P/R27/29，非全配菜指标；包装未知处理通过 |
| A13 | 20用户×10000记录，20并发，10操作各100次API样本，跨用户404 | 通过；最终P95约62–665ms，零错误，不含网络/模型 |
| A14 | Ruff/mypy134文件/pytest963/TypeScript/build；真实PG/Redis/MCP/Worker/视觉/联合恢复 | 本地通过；新环境MCP in-process、检索hashing/lexical，不冒充BGE/外部HTTP部署联验 |

## C01–C24

“工程覆盖”表示指定夹具证明业务规则；真实子集固定15个语义场景和两个生命周期组，
不是24类场景全部以真实故障运行。冻结文字解析不能代替会话证据。

| ID | 已有可定位证据 | 当前结论/未测 |
| --- | --- | --- |
| C01 | test_chat_prepare_unknown_quantity_and_trusted_commit_without_model；真实C01及生命周期 | 工程通过；真实新草稿/150克熟重补充/确认重放通过 |
| C02 | test_unresolved_subject_time_or_actuality_cannot_be_confirmed；真实C02/T11–20、T31–40 | 工程及真实假设/未来排除通过；不代表所有普通问答组合 |
| C03 | 第三方/unknown工程参数；真实C03/T21–30 | 工程及真实第三方排除通过；最新文字零错误本人实际提议 |
| C04 | revise_invalidates_old_preview；真实C04及删除生命周期 | 工程通过；真实撤回草稿、删除已存记录通过 |
| C05 | verify_diet_web双食物；test_diet_quantities；真实C05 | 工程及真实苹果100克/牛奶未知不串量通过 |
| C06 | missing meal、unknown quantity、锚点；真实C06 | 工程通过；真实补午餐后保留日期且ready |
| C07 | test_no_unjustified_conversion；真实C07/T41–50 | 工程及真实两人分餐不擅转克通过 |
| C08 | label_units_unknowns、unsupported basis；真实C08/I30 | 工程换算通过；真实未说生熟保持unknown、不可读标签不编值，不宣称可读标签OCR准确率 |
| C09 | manual_field_locks、reparse_merges_unlocked_fields；真实C09 | 工程锁定/迟到合并通过；真实改苹果量后删牛奶通过 |
| C10 | 活动草稿归属/字段锁/显式序号工具校验；真实C10 | 工程及真实同名歧义不擅选通过；非无限对抗覆盖 |
| C11 | submission_date_anchor_survives_midnight、真实SIGKILL保留draft/预算 | 工程覆盖；真实跨午夜排队未实等到次日 |
| C12 | 草稿/拆分/聊天刷新；真实C12、最终聊天浏览器 | 工程和真实闲聊保留草稿/刷新预览通过；不冒充所有换会话组合均实测 |
| C13 | intent与推荐槽位隔离、双方引用投影；真实C13 | 工程隔离通过；真实新晚餐不继承午餐米饭/重量通过，推荐组合由工程覆盖 |
| C14 | ambiguous_or_editing_replies、wrong_operation_word、hash/version/owner；真实C14 | 工程通过；真实“好”不批准，明确保存才提交；多预览歧义为工程覆盖 |
| C15 | late_parse_only_proposes、manual locks、extended双标签409 | 工程/浏览器覆盖；真实视觉迟到未测 |
| C16 | 并发/丢回执/durable replay；真实生命周期及两尺寸双确认 | 工程及真实PG确认重放/刷新无重复通过 |
| C17 | API/媒体/标签/游标越权、PG复合FK；真实生命周期未认证401、照片跨用户404 | 指定夹具和真实边界通过；不是完整渗透测试 |
| C18 | summary unknown/分母、signed pagination、15KiB投影；真实C18/管理组 | 工程通过；真实查询返回未知/不可比较/分母，不靠模型补造 |
| C19 | rejection_persists_discard、log_delete预览/批准/旧确认；真实生命周期 | 工程及真实已存记录删除预览/确认通过 |
| C20 | INVALID_STRUCTURED_OUTPUT真实样本；任务预算不足、取消/租约/手改保留 | 失败安全覆盖，真实模糊照片/视觉供应商故障未测 |
| C21 | multiple_events_split、28餐/目标独立预览；真实C21及管理组 | 工程和真实混句先核对/不直接保存通过；餐单/目标分别确认通过 |
| C22 | 双方引用/失败轮、history完整轮、purge/联合恢复；真实生命周期 | 工程及真实删除后查询保持删除通过；恢复墓碑不复活工程实跑 |
| C23 | Trace/任务事件/history投影、Worker异常日志回归；真实生命周期Trace检查 | 指定夹具及真实Trace通过；未做所有日志/供应商侧全面隐私审计 |
| C24 | test_graph_resource_api、test_tools_local、test_durable_chat、真实迁移夹具 | 本地兼容通过；正式新旧API/Worker同时切换不在本轮范围 |

## 真实文字结果

数据`data/eval_diet_text_20261112.json`，执行前固定60条与预期；SHA256
`b87accb3082fbed3f3a2d457a5a8652238daf39807a484ad0a5c5e9eae8397f4`。
它是合成人工指定预期，不是代表性真实用户语料或营养金标准；提示调整重复使用同一集，
后续须增加独立盲测集，不能将这批当泛化准确率。

最终报告`real-final/{text-v6,text-blind-v6}.json`：qwen-plus/diet-parse-v6，
原60/60、独立24修复后24/24，匹配precision/recall均1.0，错误本人实际提议0，
正式写入0，无重试/fallback。独立24首次22/24，后续调提示复用不再称盲测。
两组单调用P50/P95分别3.545/3.967s和3.589/4.587s，不含排队，供应商金额未知。
照片最新30项和槽位统计见真实报告及复核JSON，不与文字合并成总体准确率。

以下为同日历史失败，保留追溯，不能解释成当前结果：
`.runtime/diet-acceptance-20261112/current/text-report-v5.json`：
qwen-plus、diet-parse-v4，59/60字段与数量整体正确；食物precision100%、recall98%。
T24返回INVALID_STRUCTURED_OUTPUT而安全失败，0错误本人实际提议、0正式写入。
严格passed=false，不用正确率平均值覆盖失败。138964输入/7516输出tokens，零重试和fallback；
P50 3.655s/P95 4.403s为网关单调用，不含排队。供应商金额未核验，estimated_cost=0不代表免费。

历史报告仍保留：v2 46/60且unsafe_actual12；v3 54/60；v4 59/60但熟米饭数量口径错误。
脚本只提议不写实际记录。Fake图片/白图只证明安全管线，不作为识别或营养准确性证据。

## 操作与交接

- 当前真实本地入口Web5191/API8023，新独立PG库0007；主API/Worker/Web保留运行。
  原8006/5178及原业务库0003保持原状；临时聊天8025验收服务在无在途任务后关闭。
- 工程浏览器证据`.runtime/diet-acceptance-20261112/current/{manual,plans,privacy,extended,chat}/`；
  最新真实模型证据`real-final/{browser-v3,chat-browser-final-v2}/`。
  失败截图不删除；成功截图以脚本最后实际完成的结果为准，不能仅凭目录存在判断通过。
- 备份工具及门禁规则见[操作说明](diet-operations-2026-11-12.md)。必须真实停写，不自动停原服务。
- 当前不需要用户补充模型或照片。来源图片仅本地验收；水印/商业图权属不按仓库许可
  推定逐张已核实，不作为可再发布素材。更全面食品/配菜标注、营养真值和模型泛化是后续
  质量评测，不能用本批粗粒度槽位结果替代。
- 未来部署前另验原业务停写/备份/迁移/恢复/切换、静态加密、异机保护及30天副本销毁。
- 账户注册/登录/注销/恢复属独立上线必需模块，受控令牌开发验收不能替代它。
