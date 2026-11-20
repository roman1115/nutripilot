# 饮食管理实施基线

日期：2026-11-11。负责人：xerina。状态：**2026-11-12 D01–D09/W01–W08本地功能验收完成**。
本轮不部署；真实模型效果按冻结样本及粗粒度主食物口径报告，不等同总体准确率。
最终证据与限制见[验收矩阵](diet-management-acceptance-2026-11-12.md)。

本文件是本模块后续编码、迁移、评审和验收的唯一实施依据。
[早期完整方案](diet-management-complete-design-2026-11-11.md)和
[会话契约讨论](diet-conversation-tool-contract-2026-11-11.md)保留为设计过程记录；
有差异以本文件及其[工具输入Schema](specs/diet-management.schema.json)为准。
改变范围、持久化语义、工具或确认规则时，先更新本基线及对应测试，再改代码。
当前实际实现与历史验收仍以[状态页](../CURRENT_STATUS.md)为准。

个人下一餐推荐扩展按[推荐联动方案](diet-aware-recommendation-design-2026-11-16.md)
实施。原十个工具及日分析、预览确认语义保持兼容。新增只读
`get_recommendation_context`，身份与时区锚点由后端注入，推荐摘要仅作为本轮模型输入。
推荐结果中的配方比例、匹配生熟状态的克重和有依据的份数复用本基线的 `FoodRef` 与
`Quantity`。推荐选项的重算和餐单预览由 HTTP 按本人会话中的选项引用读取来源。
确认时校验相关日期的有效记录、目标、餐次和选中食物版本。详细契约与测试见
[下一餐推荐联动方案](diet-aware-recommendation-design-2026-11-16.md)。

## 1. 交付目标与范围

将现有“推荐菜品、保存整菜餐单”扩展为可持续使用的
“安排饮食 → 记录个人实际食用 → 核对营养 → 对比目标 → 更正与管理数据”。
新增“饮食记录”主视图，同时完善餐单、画像/目标与聊天，并非只新增一张记录页面。
参考 CookHero 的产品完整度，不声称已逐项核验、复刻其当前实现。

以下全部纳入同一次完整交付；工作包是开发依赖顺序，不是削减范围的产品版本。

| ID | 必须交付 | 完成条件 |
| --- | --- | --- |
| D01 | 手动实际记录、个人食物与标签资料 | 多食物、个人份量、未知数据均可表达，来源可查 |
| D02 | 独立文字/图片记餐 | 持久解析、可编辑草稿、失败恢复、明确确认；零自动入账 |
| D03 | 完整周餐单 | 按日期四餐增删改、复制单餐/整周、份量与备注、批量预览 |
| D04 | 计划转实际 | 实际日期/份量可改，重复点击不重复记，明确“再记一次”可新增 |
| D05 | 更正与撤销 | 条目、日期、餐次可改；撤销已吃、软删除、恢复、彻底清除 |
| D06 | 按生效日期的四项目标 | 热量、蛋白质、脂肪、碳水；未来设置、历史更正、停用 |
| D07 | 日/周分析 | 计划/实际/目标分开，区间、完整度、偏差、记录覆盖天数 |
| D08 | 聊天闭环 | 自动填写、必要追问、按问题查询、预览确认、多轮更正 |
| D09 | 数据与隐私管理 | 本人隔离、私有媒体、导出、留存清理、故障恢复与可观测 |

个人食物资料维护属于D01；完整私人食谱创作、社区、联网研究、自定义Agent/MCP、
图片生成属于其他模块。本模块不提供疾病诊断、治疗或处方，不自动生成医疗营养目标。
账户注册、登录、注销与账户恢复需另行完整交付；公开产品上线前必须完成。
本模块固定使用认证上下文的稳定`user_id`，受控令牌可用于开发验收，不能代替公开账户体系。

2026-11-12 xerina明确当前只做本地完整功能，不部署。生产静态加密、异机备份、
30天实际销毁、公开账户及正式维护切换仍是未来上线条件，不作为本轮本地可用性验收阻塞；
本地真实模型、持久化、Worker联调、身份隔离、幂等及恢复要求保留。

## 2. 已核对的代码与改动边界

| 已有实现 | 当前约束 | 实施决定 |
| --- | --- | --- |
| [目录计算](../src/nutripilot/retrieval/catalog.py)、[营养模型](../src/nutripilot/retrieval/models.py) | 菜谱按投料重计算；旧营养标量采用区间上限 | 保留检索兼容，新摄入模型单独支持未知与区间 |
| [本地模型](../src/nutripilot/tools/local/models.py)、[餐单API](../src/nutripilot/api/app.py) | 条目ID由位置拼接，当前目录变化会影响旧餐单展示 | 新增稳定ID、份量、快照；旧接口改为只读投影/兼容适配 |
| [PostgreSQL存储](../src/nutripilot/infrastructure/postgres.py) | 原业务主要用JSONB；已有画像、餐单 | 保留原表，新业务归属/日期/版本/关联显式列，快照用有类型JSONB |
| [执行器](../src/nutripilot/tools/executor.py) | 写工具有确认和Redis幂等；缓存键不含参数摘要 | 新工具限定草稿权限，数据库幂等；见第9节，不能直接回放旧缓存 |
| [图路由](../src/nutripilot/agent/routing.py)、[确认节点](../src/nutripilot/agent/nodes/confirmation.py) | 确认后执行待处理工具；Trace记录完整参数 | 新增受信提交分支和脱敏投影，保留旧工具确认分支 |
| [任务存储](../src/nutripilot/infrastructure/chat_tasks.py)、[Worker](../src/nutripilot/application/chat_worker.py) | 已有持久队列、单队列Worker所有者、预算及恢复 | 同一Worker调度独立类型任务，共用并发额度，不伪造聊天会话 |
| [配置](../src/nutripilot/config.py)、[网关](../src/nutripilot/model_gateway/base.py) | 已有文本模型，默认时区America/Los_Angeles；尚无视觉输入 | 增加类型化视觉输入和用户时区；不能假定默认中文用户在北京时间 |
| [迁移目录](../migrations/versions) | 当前最后一版为0003 | 新迁移接在0003之后，演练后切换，不运行时静默补表替代迁移 |

保持单应用、单PostgreSQL、Redis、现有MCP与React技术栈。
不新增微服务、事件总线、向量化私人记录或独立通用工作流引擎。
领域规则放`domain/diet.py`，应用编排放`application/diet_service.py`和
`application/intake_service.py`，存储端口放`tools/local/ports.py`的独立`DietStore`协议；
内存和PostgreSQL适配保持现有风格。以上新增路径均待创建。

## 3. 共用类型与数值口径

### 3.1 ID、版本、日期与身份

- 新对象ID为服务端生成UUID；旧餐单ID保留TEXT，不强制把历史非UUID改名。
  对外新餐单使用UUID；旧ID经兼容映射查找。条目排序用`position`，不用下标充当ID。
- 日声明没有独立UUID，对象引用使用`{kind:"day_status",id:"YYYY-MM-DD",version}`，
  日期在本人事务内唯一；其修订同样使用自然日期，不能用预览UUID冒充日声明ID。
- 所有可修改对象使用正整数`version`，每次有效修改加1；另有用户级`diet_epoch`，
  每次影响计划、记录、目标、食物资料或完整度的正式事务加1。
  它是并发一致性版本，不是模型上下文。
- 归属只来自服务端认证，不接受请求体、工具参数中的`user_id`。
  新接口统一认证依赖；生产模式拒绝无认证，不使用`X-User-ID`覆盖已认证身份。
  demo/test仅允许服务端配置的固定测试身份。
- 2026-11-13按本地使用要求，非生产环境可显式配置`API_LOCAL_USER_ID`作为服务端固定身份，
  同时清空`API_ACCESS_TOKENS`，无需浏览器令牌且忽略客户端身份覆盖，保留归属校验和限流。
  本地API仅绑定127.0.0.1；生产模式及与令牌同时配置均拒绝启动。
- 本地日期用DATE；可选时间用TIME；UTC时间用TIMESTAMPTZ。
  记录保存`timezone`（IANA）、`local_date`、可空`local_time`、可空`utc_offset_minutes`。
  无时间时`occurred_at`为空，不伪造中午12点。
- 同时有时间与时区时校验偏移；夏令时重复时刻须选择偏移，不存在时刻拒绝。
  旅行后旧记录仍属于原本地日期；改日期/时区属于显式记录更正。
- 每次消息提交保存`submitted_at/timezone/anchor_local_date`，排队和恢复沿用该锚点。
  周固定周一至周日；未指定查询日期只查锚点当地当天，并在结果说明。

### 3.2 营养、份量和来源

API及JSONB中的营养与重量统一十进制字符串，最多9位整数、6位小数；
Python使用`Decimal`，目标列使用`NUMERIC(15,6)`。禁止NaN、Infinity、负摄入量。
汇总使用未展示舍入的值；显示热量整数、克1位、钠毫克整数，区间下界向下、上界向上舍入。
旧浮点输入仅在兼容适配器用`Decimal(str(value))`转入，新接口不接收浮点营养值。

固定六字段：`calories_kcal/protein_g/fat_g/carbohydrates_g/fiber_g/sodium_mg`。
前四项可设目标；每项独立未知。单项快照：

```json
{
  "value": {"lower": "120", "upper": "140"},
  "basis": "calculated_estimate",
  "evidence_ids": ["source-1"],
  "missing_reasons": []
}
```

`value=null`表示未知，不是0。`basis`仅
`catalog_value/label_reported/user_reported/calculated_estimate/model_estimate/unknown`。
这些是来源类别，不设置会被用户确认自动升级的`verified=true`。
来源对象固定含`evidence_id/source_kind/source_ref/source_version/retrieved_at/food_state/
basis_quantity/nutrients/original_excerpt/calculation_note`；
模型来源另含模型与提示版本，标签来源含本人media_id或明确的手填说明。
引用菜谱继续保留已有来源与授权状态，外部资料未知授权不得因可访问就再发布完整内容；
本模块不抓取外部菜谱扩充种子库。
过敏原另存`known_present/known_absent/unknown`及依据，不能从用户过敏画像推断食品过敏原。

每个食物条目保存`food_ref/name_snapshot/quantity/evidence_snapshot/nutrition_snapshot/
calculation_version/field_provenance`。历史更改数量默认使用原证据重算；
换食物或用户选择“重新匹配资料”才加载新证据，展示新旧差异并生成新预览。
服务端重建并校验快照，绝不接受客户端或模型直接提交最终营养合计。
目录版本以资料版本或规范化内容SHA-256标识；历史快照不依赖当前目录仍存在。

| 数量类型 | 必需字段/语义 | 计算 |
| --- | --- | --- |
| `unknown` | 保留“半盘”等原始描述 | 营养未知；不得默认为一份 |
| `mass` | grams、food_state | 仅资料状态与可食部口径匹配时，按每100克缩放 |
| `mass_range` | grams_lower、grams_upper、food_state | 正数且下界≤上界；按非负区间乘法 |
| `servings` | count、服务端可验证的serving_evidence_id | 有每份克数或每份营养依据才可算 |
| `recipe_fraction` | batch_multiplier、share | `0<share≤1`；整批投料营养×批量×本人占比 |
| `cooked_mass` | batch_multiplier、consumed_grams、batch_cooked_grams | 本人熟重≤整批熟重；以本人/整批熟重分配整批营养 |

原料重不能冒充成菜重；“半盘”不等于参考菜谱的一半。
菜谱可用`recipe_fraction/cooked_mass`；食材不能用菜谱比例。
没有熟重产出时，不能用菜谱原料总重反推每100克熟菜营养。
重量/份数上限分别100000克/1000份、批量上限1000；超限返回校验错误，不自动截断。
支持标签kJ转kcal（除以4.184）、g/mg换算；ml需密度证据，否则不转克。
解析可提议有来源说明的估算重量区间；用户确认后仍标估算。
文字录入没有资料时不由模型补造营养。2026-11-12按xerina澄清，普通餐照允许视觉模型
根据可见食物与常见做法提议份量和每100克营养的粗略估算，不要求称重、完整配方或目录匹配，
不设置置信度/营养精度保存门槛。模型估算保留实际模型、提示版本、图片引用和估算说明，
仍使用`model_estimate`来源；确认不升级为已验证。无法估计的字段可未知，不阻止其余条目保存。

### 3.3 汇总与比较

逐营养字段返回`known_subtotal`、`complete`、`known_item_count/total_item_count`和缺失原因。
总计只在全部有效条目有该字段时给值；否则`total=null`，保留已知小计。
一天没有记录默认`no_records`，不得说摄入为0。
日完整度声明为`open/complete/no_intake`；`no_intake`必须无有效记录，
新增记录时自动回到`open`。`complete`是用户说“已记录完整”，不使未知营养变已知。
删除/更正记录使相关日期声明回到`open`，避免沿用过时的完整结论。

日/周同时提供记录覆盖、完整声明覆盖、营养字段覆盖；周均值明确分母，
区分已记录日平均、完整日平均、七个自然日的完整可算平均，不用空白日补0。
周汇总按周一分桶，查询截断周返回实际覆盖日期。
目标达成只对可比且完整的数据判定：摄入区间全落目标内为`within`，
全低/全高为`below/above`，交叉为`overlap`；不完整为`not_comparable`。
差值区间为`[actual.lower-target.upper, actual.upper-target.lower]`，允许负数。
不把区间中点、已知小计或未知目标算成确定达成率。
无目标、未知计划份量、未声明完整均给原因，不输出误导性“超标/达标”。

计划与实际对比含两种口径：全部计划与全部实际的日期营养对比；
来源关联的执行对比（未记录/已有记录次数/数量差异）。
手动记录不因同名食物自动关联计划；一餐多次实际不按菜名去重。

## 4. 数据库契约

### 4.1 共用约束

下表未标“可空”的字段必填；业务可修改实体共用
`id/user_id/version/created_at/updated_at`，`version>=1`。
带`deleted_at`实体的7天恢复窗从软删除时刻算，恢复置空。
UUID在PostgreSQL用UUID，旧餐单ID用TEXT；用户ID继续BIGINT。
JSONB必须对应固定Pydantic模型，`extra=forbid`，不作任意patch容器。
跨实体关联使用`(user_id,id)`唯一键和复合外键，应用同时校验归属；
审计多态引用由应用校验，不能伪称拥有数据库外键。
所有查询有`user_id`条件，不提供无归属的通用仓储读取。

### 4.2 正式业务表

| 表 | 字段与约束 |
| --- | --- |
| `diet_user_state`（新增） | `user_id PK, timezone, diet_epoch BIGINT>=0, target_epoch BIGINT>=0, updated_at`；用户修改事务首先锁该行，首次访问原子创建 |
| `meal_plans`（扩展原表） | 原id/user/idempotency保留；新增`title,start_date,version,deleted_at?,migrated_at?`；原payload冻结为历史备份，切换后不再作为数据源 |
| `planned_meals`（新增） | 共用字段，`plan_id,local_date,meal_type,note?,deleted_at?,intake_generation BIGINT>=1,legacy_item_refs JSONB`；refs为旧位置映射数组；同一计划同日同餐最多一个未删除餐次，多个菜放items |
| `planned_meal_items`（新增） | 共用字段，`planned_meal_id,position>=0,body JSONB,deleted_at?`；body为第3.2节条目，餐次内有效position唯一 |
| `diet_logs`（新增） | 共用字段，`local_date,local_time?,timezone,utc_offset_minutes?,occurred_at?,meal_type,channel,note?,source_planned_meal_id?,source_event_kind?,source_event_key?,deleted_at?`；channel为manual/text/image/chat/plan；source_event_kind为default_plan/explicit_repeat/copy或空 |
| `diet_log_items`（新增） | 共用字段，`diet_log_id,position,body JSONB,source_planned_item_id?,deleted_at?`；原始source计划删除时保留，不级联删实际 |
| `nutrition_targets`（新增） | 共用字段，`effective_date,calories_lower/upper?,protein_lower/upper?,fat_lower/upper?,carbohydrates_lower/upper?,source,source_ref?`；同用户同生效日唯一；每对同为空或`0≤lower≤upper`，热量非空时lower>0 |
| `personal_foods`（新增） | 共用字段，`name,brand?,basis_type,basis_amount,basis_unit,food_state,nutrients JSONB,serving_evidence JSONB,allergens JSONB,source JSONB,deleted_at?`；basis_type为per_100g/per_serving，来源含标签/手填说明，不接任意网址抓取 |
| `diet_day_status`（新增） | `(user_id,local_date) PK,version,status,declared_at?,updated_at`；status为open/complete/no_intake |
| `diet_revisions`（新增） | `id,user_id,entity_type,entity_id,entity_version,operation,before?,after?,receipt_id,actor_kind,created_at`；版本唯一；包含原证据用于恢复/审计，按第13节清除 |

索引：所有按日实体`(user_id,local_date,id)`；条目`(user_id,parent_id,position)`；
记录有效源`(user_id,source_planned_meal_id,deleted_at)`；
目标`(user_id,effective_date)`唯一；修订`(user_id,entity_type,entity_id,entity_version)`唯一。
对未空`source_event_key`建`(user_id,source_event_key)`唯一约束，防同一食用事件重入账。
`legacy_item_refs`只保存旧`plan_id:index`到新item_id的稳定映射，迁移后不重算。

### 4.3 草稿、批准与幂等

| 表 | 字段与约束 |
| --- | --- |
| `intake_drafts`（新增） | 共用字段，`thread_id?,source_turn_id?,channel,state,body JSONB,field_locks JSONB,field_locks_version>=1,preview_hash?,anchor JSONB,source_refs JSONB,expires_at,receipt_id?`；body含提取字段、条目、证据、缺失项；thread/turn不作为跨库强制FK |
| `diet_change_previews`（新增） | 共用字段，`thread_id?,source_turn_id?,operation,payload JSONB,base_refs JSONB,base_target_epoch?,content_hash,calculation_version,state,expires_at,receipt_id?` |
| `operation_receipts`（新增） | `id,user_id,operation,idempotency_key,request_hash,preview_id?,preview_version?,result_refs JSONB,result_status,created_at,expires_at?`；`(user_id,operation,idempotency_key)`唯一；回执不保存完整食品正文 |

草稿state：`needs_input/ready/committed/discarded/expired`。
解析任务进行中独立挂job引用，不用task状态覆盖用户编辑状态。
变更预览state：`ready/committed/discarded/expired`；无法准备完整时返回澄清，不建可批准预览。
草稿从第一次创建起24小时绝对到期，编辑不无限续期；到期可明确复制为新草稿。
每次修改版本增加，原hash失效。准备草稿只改草稿，不增加diet_epoch。
field_locks_version只在锁定范围改变时增加；普通内容更正仍增加草稿version。
正式确认的幂等键由预览ID+版本派生，不能靠客户端每次新键绕过一次确认约束。
另对同一preview_id/version建立唯一提交约束；同键异内容返回409。
prepare请求幂等键为`call_id`/API Idempotency-Key，保存参数哈希；
重试返回当前草稿引用和当前状态，绝不重新创建已过期或已放弃草稿。

### 4.4 媒体和后台任务

| 表 | 字段与约束 |
| --- | --- |
| `media_assets`（新增） | 共用字段，`kind,storage_key,content_sha256,mime,bytes,width?,height?,state,owner_refs JSONB,expires_at?,purged_at?`；kind为intake_image/label_image，key服务端生成，无用户路径；state为pending/ready/purge_pending/purged |
| `background_jobs`（新增） | `id,user_id,queue,kind,state,payload JSONB,request_hash,idempotency_key,attempt,lease_owner?,lease_version,heartbeat_at?,available_at,created_at,finished_at?,budget JSONB,usage JSONB,result_refs JSONB,error_code?,artifact JSONB?`；kind为parse_text/parse_image/export/purge；状态queued/running/succeeded/failed/cancelled |
| `background_job_events`（新增） | `job_id,user_id,sequence,event_type,safe_payload JSONB,created_at`；`(job_id,sequence)`唯一，只含进度、引用与安全错误 |

job的`payload`只存正文引用、media_id、预期draft_version和字段锁版本；
不复制整张base64图片。文字正文留草稿，后台加载。
export的`artifact`固定含私有key/checksum/bytes/expires_at，不复用图片接口返回ZIP。
查询索引`(queue,state,available_at,created_at)`、`(user_id,state)`；
任务幂等唯一键`(user_id,kind,idempotency_key)`。
媒体owner_refs仅是索引投影，释放前查询权威草稿/记录/食物引用，防止误删共享标签图。

## 5. 目标时间线

采用“生效点”表，不存互相重叠的结束日期：
一条目标的范围为`[effective_date, next_effective_date)`，最后一条无结束日。
四项均为空的点表示该期间停用目标。

- `set`：以该日原有效四项目标为底，合并明确给出的字段；缺字段保留，null清除，
  不自动推荐数值。在已有同日点时更新该点，否则插入；预览冻结所有受影响版本。
- `correct`：指定target_id/version，可改该点数值或生效日期；日期移到已有点时返回409，
  不隐式合并；展示改动前后影响的日期区间。
- `stop`：指定当前目标及版本，从effective_date开始插入/更新全空点；
  不删除历史，不能早于被引用点，不能越过另一已生效点修改错误对象。
  后续已安排生效点仍保留，预览明确停用到哪一天。
- 四项全部清空等同停用；没有任何字段变化为`NO_CHANGE`，不创建假修订。
  普通设置不回写未来点；历史更正不改历史摄入。
- 每次目标写入先锁用户行、比对base_target_epoch和对象版本，再原子更新并增加两种epoch。
  保证两个标签页不能各自基于旧时间线静默覆盖。

合理技术上限：热量100000kcal/日，其余三项各10000g/日；这不是健康建议。
范围仅验证非负、顺序和技术上限，不以宏量换算强行改写用户设置。
旧画像热量/蛋白质只投影当天有效目标；不保留第二份可独立写入的权威值。
旧画像更新若包含目标，必须先生成目标预览；偏好等非目标字段继续原流程。
包含两类更新的旧请求应整体拒绝并提示分别提交，不能部分成功后报失败。

## 6. 预览、事务与重复事件

### 6.1 共用预览

所有最终写入先生成服务端预览，手动页面也使用同一规则。
预览含`id/kind/version/state/content_hash/expires_at/base_refs/operation/
before/after/blocking_fields/optional_unknowns/warnings`；
before/after只由应用服务产生。新记录使用intake_drafts本体，不重复建第二份预览。
hash按规范化JSON（键排序、固定十进制、无展示文本）计算，含操作、版本、证据、
数量、日期、字段锁和calculation_version；不含动态倒计时。
客户端不能提交before/after/hash以替换服务端内容。

只读与草稿无需额外授权弹窗；最终预览确认一次。
“已吃/删除/恢复/设置目标”等按钮先打开含具体影响的确认区，
最后的“确认保存/确认删除”才批准，不使用浏览器原生无内容confirm。
取消只放弃预览；已经保存的记录要用新删除/更正预览。

### 6.2 单次正式提交的事务顺序

1. 校验身份及操作权限；查已提交同preview/version回执，存在则返回当前可见引用状态。
2. 在一个数据库事务中锁用户行，再锁草稿/预览和涉及业务行（按ID排序）；
   重新查回执，核对归属、版本、有效期、hash、未放弃状态、目标epoch和来源对象版本。
3. 检查资料未被撤回/禁用；目录有更新但旧资料仍有效时使用预览冻结快照。
   校验数据、人工锁、数量、日期及完整度，重算结果必须与批准内容一致。
4. 写正式记录/计划/目标/日声明及条目，写diet_revisions，增加版本和epoch；
   同事务写operation_receipts并把预览标committed、绑定receipt_id。
5. COMMIT成功后才返回“已保存”；超时且提交结果未知时通过同预览回执重查，
   不先报失败又用新键再写一次。所有错误回滚，不留下部分条目或半周餐单。

数据库事务与LangGraph Checkpoint不能合成一个事务；
用业务回执完成恢复补交，图在提交后崩溃时下次读回执，不再执行实际写入。
执行归属fence继续生效；后台失去lease时禁止写结果。
Redis不是业务成功的权威；数据库回执写失败必须连业务修改一起回滚。

### 6.3 计划标记已吃、再次记录、撤销

普通“标记已吃”使用用户+planned_meal_id+当前食用代次的稳定source_event_key。
代次存planned_meals.intake_generation，预览冻结该值；撤销普通关联事件时在同事务加1，
明确“再记一次”使用独立事件UUID，不改变普通代次。旧代次的回执永不重新创建记录。
同一代次两个草稿并发确认由唯一约束收敛为原记录；内容不同返回冲突并展示已有记录，
不能把第二份内容悄悄覆盖到第一份记录。
普通标记先查有效default_plan记录，已有则展示原记录，不因旧记录恢复后代次不同再新增。
明确“再记一次”由服务端创建新事件UUID，展示前一份和本次新份量；
record_again=true仍需校验当前消息/表单的明确操作证据，模型不能自授该语义。

撤销已吃是关联记录软删除预览，不修改计划；记录多次时先选哪一次或明确批量范围。
删除使旧提交回执返回`record_deleted`，旧确认不复活记录；
恢复走独立预览。用户重新点击“标记已吃”需新预览、明确新代次，不能使用旧批准。
计划已吃状态从有效关联记录推导；删计划只归档计划，不删实际。
计划改量不反向改实际；记录改量不反向改计划。

### 6.4 批量计划与并发编辑

一次计划create支持1–28个餐次，最多100个食物条目。
同parent内date/meal_type重复时准备阶段合并条目，稳定ID由服务端分配；
重复client_entry_key/client_item_key报错，不靠JSON顺序决定覆盖。
已有同日同餐时要求明确追加/替换，通过update预览处理，不静默覆盖。
批量复制整周沿用来源快照，按目标周平移日期，已有餐次先显示冲突选择。
批量事务全部成功或全部失败。
更改任一子条目同时增加餐次和餐单version；对批量/整周操作锁餐单版本，
单餐更新也检查所属餐单未删除。页面取得新版本后才能继续编辑。

## 7. 会话自动填写与模型输入

### 7.1 提取与多轮承接

新增独立`IntakeExtraction`，不挤进推荐IntentSlots。
固定字段：`actuality/subject/temporal/items/evidence/blocking_fields/optional_unknowns`。
actuality为actual/planned/hypothetical/unknown，subject为self/third_party/unknown。
服务端验证消息引用属于当前用户/已授权会话、原文片段确实存在；
原文存在不代表语义正确，仍需解析用例和确认预览。

每个字段保留`source_kind/source_ref/original_text/operation/status`；
status为explicit/derived/proposed/unresolved；operation为set/clear/keep。
批量计划证据绑定entry_key，食物及数量绑定item_key。
客户端/模型不生成有效业务ID、来源版本或授权身份。
source_kind只是声明；user_form必须对应服务端接收的本人表单提交，
catalog/label必须由目录/媒体仓储回验，active_draft必须匹配当前版本。
模型不得通过把来源标成user_form或label，获得人工锁或标签数据可信度。

用户明确本人、已吃语义、日期、餐次、有名称的至少一食物才可确认实际；
份量、匹配资料及营养允许明确未知。
日期未提及可用提交当天作为可见默认，餐次不能按钟点猜。
实际记录日期不能晚于提交锚点按所选时区换算的当地日期；
明确未来食用归计划，冲突先澄清，不把未来摄入计入实际。
未来“想吃”归计划，做法/假设不记餐，第三方不记本人；
否定/更正指向既有草稿或原记录，不生成负份量。
混合实际、未来计划、目标变更拆为独立预览，不用含糊的一句确认批量批准。

多轮修改必须定位具体draft/item；同名多项或多活动草稿先选择。
2026-11-12明确的新记餐复用独立文字解析契约，再由服务端调用同一准备工具；
它只产生草稿、保留原话来源与未知资料，不因目录候选不唯一反复要求补重量。
查询、更正、餐单与目标仍用按意图开放的模型工具规划。
新一餐不继承上一餐的食物、数量、日期与餐次；
“同上一餐”需查询本人原记录、展示复制范围并新建草稿。
人工字段写入field_locks，迟到解析不得覆盖；明确“重新识别该字段”才解锁。
闲聊、换页和刷新保留未过期草稿，重新加载核对版本。
active draft只在已选定或唯一明确指代时自动装配；跨会话承接先列本人候选。

### 7.2 上下文与查询一致性

图状态只保存`active_draft_ref/pending_diet_preview_ref/selected_event_ref/
clarification_fields/diet_query_refs`；数据库保正文与状态。
新增饮食分支的`diet_tools`节点在一次节点调用内完成模型选工具和受限执行，
不在二者之间把含私人参数的pending_tool_calls写入Checkpoint。
新工具结果只把查询描述/草稿引用、版本、状态写回图；下一步调用前从仓储临时加载投影。
新写入工具call_id固定为run_id+逻辑step，节点重试先查数据库回执；
若同位置已准备成功，沿用该结果，不重新让模型生成不同参数绕过幂等。
原推荐/菜谱工具分支仍沿用既有Planner和ToolNode，不作无关重构。
摘要装配时将已分类的饮食业务轮替换为对象引用和“待确认/已保存/已更正”状态，
同轮模型答复也整体替换后再送摘要模型；不依赖正则删除数字或提示词作为隐私保护。
原始聊天独立保留规则见第13节，不宣称Checkpoint完全没有用户自行输入的食物文字。
模型输入是当前问题、必要近期上下文、相关画像、活动草稿有界投影、意图工具白名单
及本轮查询结果；不启动时加载全部历史，不拿摘要当营养依据。
对已软删/清除的引用，每次投影校验失效，不重新注入旧工具正文。

查询一次事务以一致快照读出数据及diet_epoch；结果带
`scope/as_of/diet_epoch/record_refs/versions/completeness/truncated/next_cursor`。
工具间epoch不一致而需合并比较时重查相关范围；持续变化则返回
`DATA_CHANGED`和已读取范围，不编造“全量最新”结论。
游标签名绑定用户、筛选、排序键和epoch；期间数据变化返回409，重新翻页。
输出超过预算先缩小范围/分页，显示遗漏范围，不静默删除未知项后声称完整。

“今天晚餐怎么安排”可读当日实际和目标，不默认读几个月原文。
食品备注、图片文字和工具输出只当资料，不能改变工具权限和批准规则。
新饮食数值回答必须由查询/快照渲染或引用校验；自由生成不能补无来源数字。

## 8. 模型可见工具与返回契约

工具输入以[严格Schema](specs/diet-management.schema.json)为准；
下表正好6读+4准备，原画像/菜谱等工具继续存在，每种意图只开放所需子集。
Schema限制结构；日期范围、归属、证据、数量顺序、目标范围等仍须服务端语义校验。
Pydantic实现必须通过同一[正反样例](specs/diet-management.schema-cases.json)，
以免“文档允许、运行时拒绝”或相反。

| 工具 | 参数模型 | `ToolResult.data`业务内容 |
| --- | --- | --- |
| `search_foods` | SearchFoods | 公共/本人食品候选、FoodRef、名称、适用生熟/计量、来源、可用营养字段、serving_evidence_id |
| `get_meal_plans` | GetMealPlans | scope按ID或日期互斥；餐单/餐次/条目/份量快照、版本、有效已吃记录引用 |
| `get_diet_logs` | GetDietLogs | 本人实际事件、条目、快照、完整度；include_deleted仅在恢复/更正意图授权 |
| `get_nutrition_summary` | GetNutritionSummary | 第3.3节日/周确定性汇总，不重复全部原文 |
| `get_nutrition_targets` | GetNutritionTargets | 范围内有效目标生效点、派生结束日、缺失期间、版本 |
| `get_diet_deviation` | GetDietDeviation | 计划/实际/目标对比、可比性、原因、参与对象版本 |
| `prepare_intake_draft` | PrepareIntakeDraft | create/revise/discard；当前草稿引用、澄清/预览投影、解析任务引用 |
| `prepare_diet_log_change` | PrepareDietLogChange | correct/delete/restore预览，准确record_id和base_version |
| `prepare_meal_plan_change` | PrepareMealPlanChange | create批量餐次；update/delete/copy指定餐次，返回一份操作预览 |
| `prepare_target_change` | PrepareTargetChange | set/correct/stop目标预览、影响日期与版本 |

沿用现有`ToolResult`外壳，不再建立第二套成功/错误包。
所有data固定包一层：
`{kind,scope,as_of,diet_epoch,items,next_cursor,truncated,warnings,preview,job}`。
查询时preview/job为空，准备时items可为空，search无个人数据变更时diet_epoch可空。
`kind`为food_hits/plans/logs/summary/targets/deviation/draft/change_preview；
每种items为对应有类型模型，禁止Any透传，API复用这些资源类型。
warning为`{code,field?,message}`，不含堆栈或完整用户原文。
准备返回success仅表示草稿/预览已保存，不能据此回答“已记入摄入”。

范围：明细/计划最多31个自然日，默认每页20、最多50；
汇总/目标/偏差最多366天；食品候选默认10、最多20。
按ID读取不需日期，仍校验归属；返回计划子项仍分页，不靠ID绕过体积限制。
get_meal_plans的scope.id指餐单ID，get_diet_logs的scope.id指实际记录ID，
不尝试将同一UUID依次当成不同实体查找。
工具结果序列化后上限16KiB，先减少items并返回游标；一个对象超限只给引用和必要投影，
提示通过页面查看完整内容。32KiB以上模型工具输入拒绝，页面批量API上限256KiB。
继续保留现有每轮8步、相同调用2次、90秒及24000tokens上限；不足时不伪称读完。
每周计划只需一次prepare批量调用，不为此增加28次工具调用。

## 9. 工具权限、确认分支与幂等接线

准备工具注册为`ToolAccess.WRITE`、`required_permission=diet:draft`、
`requires_confirmation=false`；只读为diet:read。
准备handler只注入DietDraftService受限端口，没有正式提交方法；
正式提交服务仅暴露给受信API/图节点，不出现在ToolRegistry。
准备继续要求身份、幂等键、执行fence；不能将其假装成READ来绕开写保护。

对四个新准备工具设置显式`idempotency_backend=database`元数据，
ToolExecutor跳过通用Redis成功结果读取/写入，进入数据库参数哈希与当前状态检查。
对database模式单次handler传入的context.idempotency_key固定为受信call_id，
request_hash按已校验参数规范化计算；不能复用整轮同一键导致多份草稿互相覆盖。
旧工具默认仍用现有缓存；不能删除全局防护。
此新增枚举只允许redis/database，database须注册支持协议的handler，
错误配置启动时失败，防止默默无幂等。

确认分三条受信入口，最终共用第6.2节服务：

1. 页面向`/diet/previews/{id}/confirm`提交版本/hash及明确approve。
2. 聊天卡片向既有`/threads/{id}/confirm`提交diet预览引用；
   扩展`RunConfirmationRequest`增加可空preview_version/content_hash，
   diet分支两项必填，旧call_id分支保持兼容。
3. 用户自然语言明确“确认保存/确认删除”等，服务端从当前会话唯一已呈现的、
   未改动预览绑定id/version/hash；“好/继续/知道了”、多候选、过期、已修改均不批准。
   同句含修改时只生成新预览，本句不能同时批准修改后的内容。

新确认ID用`diet:<uuid>:<version>`命名空间；
pending_diet_preview_ref含`id/version/hash/presented_at`，不复用pending_tool_calls存正文。
`ChatRunService.submit`和inline `ChatService._confirm`均分支校验归属、会话、有效性，
把确定的decision放入持久任务；不能仅改API而漏掉worker恢复。
LangGraph新增确定性`diet_commit`节点，路由确认到该节点，提交/回读回执后输出结果；
模型不能生成该节点参数，也没有commit工具。
旧确认仍由pending_tool_calls执行；原create_meal_plan兼容handler改调同一业务服务，
根据旧已展示参数构建冻结预览，旧pending变更/无法还原时要求重新预览，不能免校验。
线程归档/删除后旧确认不可执行；待草稿按留存规则放弃，正式记录不随会话删除。
所有新确认日志仅留对象引用、版本、动作、状态、耗时；不记录pending正文。

## 10. HTTP契约与错误

统一前缀`/api/v1`；以下路径除注明外为新增。
所有写请求带`Idempotency-Key`（1–100字符）；对象更改带`If-Match: "version"`。
缺版本返回428，版本/hash冲突409。body禁止user_id及未定义字段。
GET返回资源version和ETag；列表排序`local_date,meal_type,id`或创建时间+ID，
cursor不使用offset。API Decimal/日期/枚举同工具Schema。

| 方法与路径 | 请求/用途 | 返回 |
| --- | --- | --- |
| GET `/diet/context` | 本人时区、今日锚点、周起始、能力开关、epoch | 类型化上下文；不返回密钥 |
| PATCH `/diet/context` | timezone+If-Match用户状态版本（用diet_epoch） | 只影响新输入，旧记录不改日期 |
| GET `/diet/logs`、`/diet/logs/{id}` | 日期筛选/游标，include_deleted | 事件+条目投影，完整详情按ID |
| GET `/diet/summary`、`/diet/deviation` | 同工具范围 | 与工具相同计算结果 |
| GET `/diet/targets` | 日期范围 | 生效时间线 |
| GET `/diet/foods`、`/diet/foods/{id}` | 搜索或本人个人食物详情 | 食物与来源版本 |
| GET `/diet/personal-foods` | include_deleted、签名cursor、limit | 本人食物资料分页，不静默截断 |
| GET `/diet/days/{date}` | 原本地日期 | 日声明及ETag，未声明为open/version=1 |
| GET `/diet/plan-library` | 31天内餐单、include_deleted、cursor、limit | 包含无有效餐次/已归档餐单的分页目录 |
| GET `/diet/plans/{id}`、`/diet/plans/{id}/meals` | 稳定餐单ID，可为旧TEXT；餐次分页 | 本人餐单/子餐次，含关联实际引用 |
| GET `/diet/planned-meals/{id}` | 稳定餐次ID | 本人餐次完整详情，供超限引用从页面展开 |
| POST `/diet/drafts` | 下述mode判别联合、channel、来源 | 201草稿，blocking与unknown可见 |
| GET `/diet/drafts`、`/diet/drafts/{id}` | 本人未过期列表/准确ID | 草稿、字段锁、当前任务 |
| PATCH `/diet/drafts/{id}` | EntryPatch、evidence、field_lock_changes | 新版本；改动项旧hash失效 |
| POST `/diet/drafts/{id}/parse` | text/image、已授权media_ids、parse_field_keys | 202任务；job绑定草稿当前version |
| POST `/diet/drafts/{id}/discard` | 明确放弃 | 当前状态，已提交拒绝 |
| POST `/diet/previews` | 下述严格操作判别联合 | 201预览，不改正式业务 |
| GET `/diet/previews/{id}` | 本人当前预览；也识别intake_draft ID | 可确认视图或过期状态 |
| POST `/diet/previews/{id}/confirm` | approved、preview_version、content_hash | 200回执或放弃状态 |
| GET `/diet/receipts/{id}` | 本人回执 | 结果引用及当前存活状态，无完整原始正文 |
| GET `/diet/jobs/{id}`、`/diet/jobs/{id}/events` | 本人任务；事件after_sequence | 轮询及有界增量事件，刷新后可恢复 |
| POST `/diet/jobs/{id}/cancel` | 明确取消 | 202；已完成返回原状态 |
| POST `/diet/media` | multipart图片；用途/隐私授权 | 201私有media_id，校验失败无可用图 |
| GET `/diet/media/{id}/content` | 本人鉴权 | no-store私有图片；无公共路径 |
| DELETE `/diet/media/{id}` | 无有效引用时直接释放，否则先解绑预览 | 202清理任务或409引用冲突 |
| POST `/diet/exports` | 日期范围、include_media、format固定zip | 202任务，下载需再次鉴权 |
| GET `/diet/exports/{job_id}/content` | 本人已完成、未过期 | 私有ZIP，no-store |
| POST `/diet/purges` | 明确已展示的purge预览引用/hash | 202不可恢复清理任务 |

POST `/diet/previews`的`operation`为严格判别联合：
`log_correct/log_delete/log_restore`映射PrepareDietLogChange；
`plan_create/plan_update/plan_delete_meal/plan_copy_meal`映射PrepareMealPlanChange；
`target_set/target_correct/target_stop`映射PrepareTargetChange。
页面专用补充：`plan_copy_week/plan_archive/plan_restore`含准确plan_id/version、
复制目标周及冲突处理；`personal_food_create/update/delete/restore`含固定食物模型；
`day_status_set`含date/status/基版本；`purge`含entity_type/id/version或明确日期范围。
不提供字符串JSONPath、任意SQL、通用数据库字段patch。

页面专用集合列表`/drafts`、`/personal-foods`仍返回统一分页信封；scope为
`{kind:"collection",resource:"drafts"|"personal_foods"}`，kind分别为drafts/personal_foods。
`/plan-library`的kind为plan_library，按餐单开始日+ID排序；
`/plans/{id}/meals`的scope为`{kind:"plan",id:"原餐单ID"}`。
这些是HTTP页面集合类型，不改变冻结十工具的输入Schema。
跨用户/过滤条件/集合复用cursor拒绝，diet_epoch变化要求重新翻页；
过大草稿/食物/餐次返回稳定引用，由页面鉴权读取完整详情。
对应可执行样例与验收：`test_diet_api.py`、`test_diet_queries.py`，日声明回执示例：
`{"kind":"day_status","id":"2026-11-12","version":2}`。
整周冲突处理仅reject/append_items/replace_meals；replace预览列出被替换餐次。
个人食物删除只影响后续选择，不删历史快照；restore需仍在7天窗内。
大范围purge必须展示数量和影响，最终调用上表专用入口，不在模型工具中开放。

POST `/diet/drafts`支持三种严格mode：
`structured{entry:ProposedEntry,evidence,source?,media_ids?}`、
`text{text:1–4000字符}`、`image{media_ids:1–4个,caption?:最多4000字符}`。
后两种创建needs_input空骨架并自动排解析job，返回job引用，
不要求用户先填写一个虚假的食物才能上传；骨架items可为空，但永远不能直接确认。
工具create是在已提取至少一项后准备草稿，仍沿用Schema的items非空限制。
提交图片授权送模型与上传私有保存是两个明确动作；未授权时仅存草稿等待授权，不排视觉job。

旧GET `/meal-plans`维持原字段，并增加stable IDs与quantity_status；
旧单条整菜营养明确标`reference_recipe_nutrition`，不当作个人摄入。
新完整餐单GET `/diet/plans`和`/diet/plans/{id}`返回稳定餐次/items并分页；
新UI和模型用新接口。旧画像目标写法见第5节，不保留双写。

统一错误码：401身份、403能力拒绝、404不存在或他人资源（不透露存在性）、
409 `VERSION_CONFLICT/IDEMPOTENCY_CONFLICT/DATA_CHANGED/NO_CHANGE/SOURCE_CHANGED`、
410 `PREVIEW_EXPIRED/RESOURCE_PURGED/EXPORT_EXPIRED`、
413体积、415格式、422字段与语义、428缺版本、429配额、503依赖不可用。
410只对服务端已知属于本人的墓碑返回；他人ID始终404。
错误体沿用HTTP错误机制并增加`code/message/field_errors/retryable/current_version?`，
不包含SQL、路径、密钥、跨用户信息或完整图片/原文。

## 11. 文字、图片与后台执行

### 11.1 解析流水线

上传/文字提交 → 创建草稿与持久job → Worker领取 →
文本或视觉网关提取结构 → 食物候选匹配 → 确定性数量/营养计算 →
合并未锁字段 → 保存新草稿版本与job结果 → 前端编辑/确认。
未知、模糊、失败和配额不足可手填；不能把模型请求成功当记录成功。
标签识别先产生个人食物资料预览，经本人核对营养口径后保存，再引用记餐。
普通餐照提议食物、估算份量和粗略营养，优先使用可计算的已有资料，否则使用明确标注的
模型估算；不得把普通餐照估算表述为可靠营养标签。略模糊或做法不明不等于整单拒绝，
可识别部分照常提议，不要求用户提供精确克重。只有完全无法辨认/非食物等才返回空提议，
并允许手填。授权、本人隔离、文件安全、人工字段锁和最终一次保存确认不取消。

视觉输出在原`entries/clarification`外可返回`estimates`：每项绑定`entry_index`、
`client_item_key`及`name`，包含`food_state`、六营养的每100克估算区间和`rationale`。
模型名/提示版本和媒体引用仅由服务端附加；不扩展10工具写入Schema以允许客户端伪造来源。
草稿保留该结构供刷新、拆分和更正复用；改名或换食品后不沿用不匹配估算。
结构样例见[餐照估算样例](specs/diet-image-estimate.example.json)，是合成契约夹具，
不作为实际照片营养真值。标签仍使用独立`ParsedLabel`，不混入餐照推测。

网关增加`ModelImageInput{media_id,mime,bytes}`，仅应用加载本人清洗后的文件。
OpenAI兼容适配器生成供应商支持的多模态payload；Fake提供确定性结构化结果。
图片不经过任意URL抓取，不让模型指定本机路径；外部模型调用前界面明确显示送出范围，
授权记录绑定job/media集合，后台重试不扩大集合。
不支持视觉时明确不可用；不能把base64塞进原文本JSON就声称已接通视觉。

解析提交时冻结draft_version与field_locks_version。
完成时已改过的字段只列建议差异、不覆盖；草稿已确认/放弃/过期则结果不再合并，
job可标succeeded但result标`not_applied`。不能用迟到结果改正式记录。

### 11.2 持久调度与恢复

在已有ChatWorker内增加明确类型分派和BackgroundJobStore，
共用现有queue和worker advisory owner lock；API进程不另启动无持久化解析协程。
领取聊天和模型解析时，在同一个schedule事务锁内统计两表running总和，
全局共4、每用户共3，解析每用户最多1；pending总量沿用每用户20上限。
队列按入队时间公平领取，不因单用户图片阻塞所有聊天。
export/purge在同一Worker使用独立最多1个非模型维护槽，不占模型槽；
每批有限量并在批间释放，避免清理饥饿或一次清除锁住大表。
不重写聊天Checkpoint和终端收尾，只提取共享的锁/容量检查。

job事务更新`lease_owner/lease_version`，心跳10秒，30秒失联可回收；
写进度/结果前核对lease。旧Worker恢复不能越过新lease。
模型解析最多2次尝试，每任务总90秒、文本6000/图片12000tokens总预算；
每次供应商调用最长45秒且不超过剩余总时限，不做网关重试×任务重试无限叠加。
视觉输入tokens按供应商可得用量记录；无法预估图片成本时保守预算并显式标未测，
不能声称与文本计价相同。重启后保留已预留/已结算用量，耗尽就失败待人工重新发起。
如供应商不支持请求幂等，崩溃恢复可能重复收费，报告尝试与未知结算，不能承诺外部恰好一次。

导出/清理不调用模型，有限批次续跑；export生成临时文件后原子rename，
成功写artifact后才可下载；清理按墓碑和幂等批次恢复。
任务终态和事件必须持久化；事件不含完整食品/图像正文。
demo使用内存DietStore与现有测试任务存储，明确进程退出不保留；
真实恢复验收必须用PostgreSQL和私有共享目录，不拿demo通过代替。

### 11.3 私有图片与依赖

仅JPEG/PNG/WebP，最多4张、每张10MiB、单请求合计20MiB、每张最多2000万像素。
先限流式字节再识别文件头、解码、像素/帧数校验；拒绝动画、多帧、损坏及伪装格式。
按EXIF方向归正后重编码为单帧JPEG或PNG，移除EXIF/GPS；保存原始hash供校验但不留原图。
解析使用清洗后图像；缩略图同样私有且跟随生命周期。
文件写入使用随机key、临时文件、原子rename，防路径穿越/符号链接逃逸，
数据库写失败删除临时文件；孤儿扫描可恢复未完成清理。

采用Pillow做成熟图片解码/EXIF处理，不手写解析器；
当前环境未安装，后续安装前按项目约定说明来源和影响并获得同意，再锁定实际可用版本。
API与Worker共享现有runtime_data卷下私有子目录，不新增对象存储服务。
文件系统权限、API缓存头、代理体积限制与备份一起验收。

## 12. 页面与交互

沿用现有工作台布局、组件与图标库，不新做营销首页。
新`DietView`为主导航“饮食记录”，内设记录与分析、目标管理、个人食物、数据管理四个入口。
2026-11-13界面调整：桌面为日期栏+日/周切换、左侧四餐记录及右侧营养摘要；
移动端先显示四餐记录、再显示营养。草稿、时区设置、完整度操作及详细比较默认收起，
目标时间线移至目标管理，保留完整操作；长菜名/未知状态不能遮挡按钮。
前端路由状态保留日期、粒度、当前视图和草稿/预览ID，刷新按ID恢复，不仅保存在React内存。

| 位置 | 必须可用的完整操作与状态 |
| --- | --- |
| 饮食记录 | 手动添加、文字、照片、每餐多事件、编辑、软删除/恢复、查看依据、未记录与未知区分 |
| 统一草稿编辑器 | 日期/时间/餐次、多食品、单位/生熟、匹配候选、字段锁、来源、未知/估算、解析中/失败/重试 |
| 周餐单 | 稳定餐次与条目、个人份量、增删改、复制单餐/整周、冲突选择、标记已吃/再记一次、关联记录 |
| 目标设置 | 四项值/区间、生效日、时间线、历史更正与停用影响预览；画像旧字段改链接/当前投影 |
| 分析 | 每日/每周实际、计划、目标、区间和已知小计，覆盖天数、不可比原因、只读历史日期 |
| 个人食物 | 名称/品牌/每份或100克、标签核对、来源、生熟、过敏原事实、版本、删除恢复 |
| 聊天 | 服务端预览卡、澄清、多个事件选择、刷新后确认状态、冲突/过期/重复保存回执 |
| 数据管理 | 草稿箱、回收站、导出任务与下载、已引用图片提示、明确不可恢复清除 |

所有视图覆盖loading/empty/partial/error/offline/forbidden/stale版本，
长任务可离页再查，提交中禁止重复按钮但后端仍防重；失败保留用户输入。
浏览器离线不能假报已保存，不引入未要求的离线同步副本。
键盘可完成所有关键操作，输入有标签、错误指向字段，弹窗焦点管理与返回路径可用。
区间/完整度不能只靠颜色表达；照片有替代说明，不使用用户私图作装饰资源。

新增前端文件预计为`features/diet/DietView.tsx`、`IntakeEditor.tsx`、
`DietPreview.tsx`、`TargetsEditor.tsx`、`PersonalFoodsView.tsx`及共享types/api方法；
按职责拆，复用Common组件，不把全部功能堆进App.tsx。
这些路径待创建，现有`features/plans/MealPlansView.tsx`、
`features/profile/ProfileView.tsx`、`features/chat/ChatView.tsx`必须同步改调用。

## 13. 隐私、留存、导出与删除

2026-11-12续作契约：永久清除预览兼容`record_ids`，新增`food_ids/plan_ids/target_ids`，
合计1–10000个且不得重复；餐单ID保留旧TEXT格式。食物/餐单须先软删除，目标须专用
清除批准，不借普通目标修改执行。预览显示目标时间线受影响日期及清除后的前序目标引用，
确认时同时校验本人、版本、hash和diet_epoch。无正文墓碑扩至这些资源及计划餐次，
使用0007增量迁移支持旧餐单TEXT ID，旧批准/后台重试不得复活。
清餐单释放计划关联但保留独立实际记录及其营养快照、事件幂等键；清食物资料不追溯删除
实际记录/其他计划中已冻结的证据（包括共享标签图），预览明确这一边界。关联草稿、
该资源的修订和变更预览正文清除；若要移除独立记录的快照须再清除该记录。
记录、食物、餐单和单独删除餐次均按7天自动清除，目标历史不自动清除。

标签图片使用同一持久`parse_image`任务及预算/授权边界，不新增模型工具。批次不能混用
餐照与标签；标签返回六项独立未知/带单位数值、逐项原文及每100克/每份口径，单位转换
由服务端完成，ml与未知口径不推定为克。提议落原草稿`label_suggestion`，不成为摄入。
页面核对提议及原图后，走个人食物预览/确认，证据绑定本人标签media_id；外部模型提议
始终不是独立验证。关闭/刷新保留草稿，未确认不创建个人食物或实际记录。
标签转食物的HTTP预览还需`label_draft_id/label_draft_version/label_reviewed=true`；
服务端读取原提议的模型名/提示版本写入证据，不接受客户端伪造来源。确认事务核对
原草稿版本并标记已使用（discarded），不同预览竞争只能成功一次，同预览重试回原回执；
过期、已放弃、已清除的标签草稿不能重新转换。此操作不生成摄入，不扩十工具Schema。
重新解析匹配当前版本时按锁合并未锁字段，`items`集合锁保留整份条目；逐条字段锁通过
稳定client_item_key匹配，无法匹配时保留原集合并给建议，不按名称猜身份。任务提交后
版本/锁版本发生变化仍只存迟到建议，不自动合并。任何合并均使旧预览hash/版本失效。
显式时区更正在HTTP`log_correct`预览顶层传`timezone`，不扩模型工具Schema；缺省保留原
记录时区。预览展示前后时区，重算occurred_at并按新时区检查DST/未来日期，确认仍走原
版本/hash事务，不会更改用户默认时区或其他历史记录。

2026-11-12早期实施契约补充（以下“仅记录/仍待”是当时历史进度，
最新已实现范围见状态页及本节后续恢复契约，不代表W07整体验收）：
`0006_diet_privacy`增加本人范围的无正文墓碑；服务需检查此迁移后才开放饮食入口。
后台payload按`task=parse/export/purge`判别；导出和清理共用独立的一个非模型槽，
不占聊天/解析的模型槽。任务API剔除私有文件key、lease身份和请求摘要。
`POST /diet/exports`返回202持久任务，`GET /diet/exports/{id}/content`重新鉴权，
仅在成功、24小时内且diet_epoch未变化时提供ZIP，数据变化立即撤销旧下载。
`POST /diet/purge/previews`以record_ids生成影响预览；
`POST /diet/purge/previews/{id}/confirm`需明确approved、版本与hash，返回202清理任务。
普通预览确认及模型工具不能执行purge；确认事务先清正文/关联副本并写墓碑，
文件清理按持久purge_pending状态处理，重复确认补交同一清理任务。
当前仅记录清除实现中；餐单、食物、目标历史、留存扫描、联合备份删除日志重放
仍须按本节完整实现与验收，不能以这些新增入口代替D09交付。
其中24小时未确认草稿/预览及无引用媒体、24小时导出和30天任务事件清理已接Worker维护；
记录7天回收站自动清除已接线；孤儿扫描、餐单/食物留存及其他实体清除仍未完整实现。
`POST /diet/media`以认证身份、用途、原图片摘要/MIME/顺序与Idempotency-Key绑定整批上传，
同键同内容返回原媒体；异内容409，已清除410，过期未关联不重建。
全部图片安全解码通过后才在本人事务写媒体和无正文回执；坏图不留下半批数据库资产。

独立解析多事件通过`POST /diet/drafts/{id}/split`事务拆为独立持久草稿，
要求本人身份、If-Match和Idempotency-Key。保留源锚点及原24小时绝对期限，
不自动确认、不延长期限，原草稿放弃；同键重试返回相同子草稿。
子事件仍逐个核对主体/时态/日期/份量和来源，不能用拆分批准实际写入。

- 未确认草稿/预览24小时失效，之后清正文和无引用图片；只留无内容墓碑用于拒绝重放。
  未关联上传24小时清除；关联记录/个人食物的图片随对象生命周期保留。
- 记录/个人食物/餐单软删除保留7天；期间可恢复，实际统计立即排除。
  到期或用户明确彻底清除后，删正文、items、修订中的内容、草稿副本、媒体派生和查询缓存。
  软删除目标不用于时间线；目标更正保留修订，清目标历史须专用purge影响预览。
- 永久清除先事务写墓碑、增加epoch并撤销下载/预览，再后台删文件和大批正文；
  purge_pending期间读取按已清除处理，任务失败可重试，不能因文件失败恢复正文可见。
  多对象共享图片只释放自身引用，无有效引用才清文件。
- 查询结果缓存不保存私人正文到Redis；新工具消息/检查点只留引用和必要状态，
  模型使用的完整工具投影为当轮临时装配，不写回持久消息正文。
  滚动摘要不纳入具体饮食数值/草稿正文，后续分析重新查库。
- 用户自行输入的原始聊天、模型面向用户的历史答复与饮食记录是独立数据。
  删除记录不会偷偷删除整段聊天；清除预览明确说明历史聊天可能仍有原文，
  提供关联会话删除入口。再次生成时不把这类旧文字当作有效记录。
  如果将来要求选择性清除聊天片段，应另设计消息/摘要/Checkpoint迁移，不能宣称本方案已实现。
- Trace、任务事件和错误日志只含引用/数量/状态/耗时/模型用量，不含图片、标签原文、
  私人食品明细；确认节点原有完整参数日志对新路径必须覆盖。
- 成功回执引用/幂等墓碑至少保留到对应预览不再可执行；软删除/清除后只留
  preview_id/hash/operation/结果失效状态，移除可反推食品正文，旧批准不重放。
  无正文任务事件保留30天，临时导出24小时后清除；用户明确清除可提前撤销。

导出ZIP含版本化manifest、UTF-8记录/条目/目标/计划/个人食物CSV、
完整区间/来源JSON及可选已授权媒体；CSV对以`= + - @`开头文本作公式注入防护，
JSON保持原文。导出在一致快照捕获用户选择范围和epoch；流式分批，
最大10000条记录/200MiB（含媒体），超出明确提示拆分，不生成不完整而未标记的包。
下载再次鉴权，不返回公共静态地址，服务端禁止缓存。

上线门槛：数据库和私有媒体联合备份、恢复演练、加密与访问控制明确。
本方案默认运维备份保留30天、受控离线访问；应用清除不承诺即时擦除不可变备份，
到期销毁；恢复时先重放保留的删除墓碑再开放访问，防备份复活被删数据。
未实现这条恢复流程前不得声称“彻底清除覆盖备份”或通过隐私交付验收。

2026-11-12恢复契约补充：真实PG路径在永久清除事务COMMIT前，将本事务新墓碑和
媒体清除意图同步到独立`DIET_DELETION_JOURNAL_ROOT`的SQLite日志，`synchronous=FULL`。
日志无食品/聊天正文，API/Worker共享且独立于不可变备份保留；写日志失败时事务回滚。
COMMIT结果未知或最后阶段失败时日志可能保留已批准的清除意图，恢复采取保守清除，
不得用重放旧备份恢复这些对象。日志保留时间须覆盖所有仍保留的备份，本实现不自动裁剪。
离线备份工具将同一停写窗口的PG dump与私有媒体写入新目录，含校验和与30天销毁日期；
不得把备份时点的旧删除日志覆盖当前独立日志。恢复仅面向空数据库/空媒体目录，
先设置`restore_pending`门禁，校验并恢复后重放最新日志、撤销旧导出，再解除门禁。
任何失败保留门禁并禁止API/Worker启动；修复后重新演练，不直接删门禁绕过重放。
备份与日志卷的静态加密、异机副本与30天实际销毁由部署运维落实；
本机权限和恢复测试不作为已配置加密或实际执行销毁的证据。

## 14. 配置、依赖与启动检查

下表为设计时的配置约定；当前实际环境变量以`.env.example`和`Settings`为准。
2026-11-12已复用现有千问凭证，实际配置并验收`qwen3-vl-plus`；
主文字为`qwen-plus`。具体本地入口、私有参数文件与启动说明见
[操作说明](diet-operations-2026-11-12.md)，不在文档内暴露访问令牌或供应商密钥。

| 配置 | 默认/要求 |
| --- | --- |
| `DIET_IMAGE_ENABLED` | false；开启必须有视觉配置、图片依赖、可写私有目录与共享卷 |
| `VISION_MODEL_NAME` | 开启时必填，选有合法使用权限且支持本适配器图像输入的实际部署模型 |
| `VISION_MODEL_BASE_URL/API_KEY` | 默认复用文本供应商地址/密钥；允许显式独立值，启动不打印密钥 |
| `DIET_MEDIA_ROOT` | runtime_data/diet_media对应绝对路径；API/Worker一致，仓库和Web静态目录之外 |
| `DIET_EXPORT_ROOT` | runtime_data/diet_exports对应绝对路径；权限与共享要求同上 |
| `USER_TIMEZONE` | 沿用现有服务端默认，只用于未设置用户；UI首访明确选择，不静默改成Asia/Shanghai |

24小时草稿/导出、7天回收站、上传限制、任务时限在业务常量中统一，
先不增加一堆可任意调小/调大的配置；变更须更新契约与验收。
复用现有MODEL配置、并发和队列配置，不能为解析另起不受约束的模型配额。
准备工具默认5秒，仅做数据库/确定性计算；长模型解析交给job，数据库超时安全失败。
能力接口显示text/image当前是否可用及安全原因；真实环境关闭图片意味着功能未完整交付，
不能用开关掩盖D02缺项。
依赖变更同步pyproject/uv.lock、镜像、启动脚本、.env.example和部署说明；
Pillow安装与供应商模型配置属于实施门槛，不在本次文档任务中安装或付费调用。

## 15. 迁移与兼容切换

1. 先备份现有数据库/媒体并做可读恢复；记录现有餐单、画像目标和未完成确认数量。
2. 0004新增本方案表、约束、索引及meal_plans扩展列，暂不切读路径；
   演示内存模型同步实现，生产启动只检查迁移版本。
3. 用可重跑数据迁移按旧餐单ID迁移：同计划同日同餐合为一餐，
   为餐次/条目分配确定性UUIDv5，冻结namespace常量，逐个保存旧plan_id:index映射；
   保留所有旧菜品引用、备注与原排列，不因合并餐次丢项目。
4. 旧餐单没有个人份量，一律quantity=unknown，标legacy_unknown；
   若能找到当前资料，可存`captured_at_migration`参考快照，不声称是创建当时证据。
   无法匹配保留原名称/ID与未知营养，不补造。
5. 旧热量/蛋白质目标在迁移锚点当地当天建立第一条来源legacy_profile的生效点；
   历史日期目标仍未知，不追溯套用。原值留迁移备份，对外读改时间线投影。
6. 校验归属、总条目数、映射唯一、数量未知、目标转换、旧响应结构及待确认恢复；
   任何不合法旧行生成逐行报告，阻断切换，不悄悄丢弃。
7. 暂停旧Worker接新任务并完成/安全中断在途写入，执行最终增量迁移与校验；
   同一部署同时切API、Worker、新旧工具handler到新仓储。禁止新旧版本并行双写。
8. 旧read接口从新表投影，旧write适配到相同预览/事务；
   不能保留旧JSONB写入后新表看不到的入口。运行时ensure表逻辑与Alembic同步但不代替迁移。
9. 0005可在演练确认后添加已验证的NOT NULL/唯一约束或移除过渡写路径；
   具体迁移数量服从实际拆分，不为凑编号造空迁移。

有新业务数据后，回滚代码只能回到理解新表的兼容版本；
不能直接downgrade删表。需要恢复旧系统时先导出新增数据、恢复完整备份并报告停机范围。
迁移演练必须包含重跑、半途失败、旧非UUID餐单ID、重复餐次及缺失菜品。
待确认旧操作无法重建同一批准内容时明确要求重新预览，不能静默接受旧批准。

## 16. 代码、调用方、夹具与文档清单

此清单是必须触达的边界，不要求为了目录美观新建所有细碎文件。

| 工作面 | 修改/新增位置 | 不能遗漏 |
| --- | --- | --- |
| 领域与契约 | 新domain/diet.py；现domain/enums.py、tools/local/models.py及ports.py | Decimal、未知/区间、数量、日期、目标、来源、preview/action模型 |
| 应用 | 新diet_service.py/intake_service.py；现container.py/runtime.py | 统一预览/提交、查询、字段锁、时区、注入内存/PG适配与任务服务 |
| 存储 | 新infrastructure/diet_store.py与background_jobs.py；现postgres.py/memory.py | 同事务修订/回执、归属FK、快照、epoch、分页、清理 |
| 模型 | 现model_gateway/base.py、openai_compatible.py、fake.py、budget.py | 文本/视觉结构、输入许可、预算、有限重试、用量 |
| 工具 | 新tools/local/diet.py；现executor.py、models.py、registry.py、local/service.py | 10工具、严格schema、数据库幂等、旧create_meal_plan和目标字段兼容 |
| 图与会话 | 现agent/contracts.py/state.py/agents/intent.py/agents/planner.py/routing.py/graph.py、nodes/confirmation.py/output.py | 饮食意图、白名单、独立提取、确定确认、可信提交、引用投影、数值落地 |
| 持久会话 | 现application/chat_runs.py/chat_service.py/chat_worker.py、infrastructure/chat_tasks.py、memory/context.py/nodes.py | 两条确认入口、任务恢复、Checkpoint不留私人副本、清除后不复活 |
| API与运行 | 现api/access.py/app.py/schemas.py、config.py、worker.py、migrations/versions | 身份、ETag、幂等、媒体/导出、能力、预算与启动自检 |
| Web | 现App.tsx/types.ts/lib/api.ts、styles.css及第12节页面 | 所有写入口统一预览、刷新/冲突/未知、响应式和无障碍 |
| 依赖/部署 | pyproject.toml、uv.lock、Dockerfile、docker-compose.yml、.env.example、scripts相关启动 | 实际配置名以实现核对为准，共享私有卷，安装授权与版本锁 |
| 测试/文档 | 新饮食unit/integration/browser/评测夹具、README、CURRENT_STATUS、验收记录 | 新旧回归、实际证据、失败与未测，不把方案写成完成 |

已有测试须重点回归`test_tools_executor.py/test_tools_local.py/test_agent_context.py/
test_graph_confirmation.py/test_graph_resource_api.py/test_durable_chat.py/
test_durable_processes.py/test_conversation_history.py/test_external_persistence.py`。
上表短路径相对`src/nutripilot/`（Web/根文件除外）；新增路径均未实现。

## 17. 实施工作包

| 顺序 | 工作包及产物 | 依赖 | 完成门槛 |
| --- | --- | --- | --- |
| W01 | 按本基线落领域/Pydantic/Schema夹具，补服务端语义规则 | 本方案 | 所有正反样例、数值/日期边界可执行通过 |
| W02 | PG/内存仓储、迁移、快照、目标时间线、版本/幂等/事务 | W01 | 真PG约束/并发/回滚、迁移重跑和备份演练 |
| W03 | 全部查询/预览/提交API，个人食物、删除恢复、日声明 | W02 | 越权、版本冲突、未知统计、CRUD一致性通过 |
| W04 | 完整饮食/目标/食物页面与周餐单编辑/复制/转实际 | W03 | 手动全链路和新旧兼容浏览器验证 |
| W05 | 10工具、意图提取、多轮草稿、可信确认及图恢复 | W03 | C01–C24、双确认入口、数值落地与Trace隐私通过 |
| W06 | 持久任务、文本/视觉、私有媒体与预算接线 | W02、W03 | 进程中断恢复、迟到结果、图片边界、真实模型冻结集 |
| W07 | 导出/彻底清除、保留与备份恢复、数据管理页面 | W02、W03、W06 | 可用导出、越权下载拒绝、清除不复活、媒体无孤儿 |
| W08 | 全回归、负载、真实环境与产品验收，修复阻塞 | W04–W07 | 第18节全部门槛，文档与实际状态一致 |

2026-11-12当前W01–W08在已确认本地范围内**完成验收**；
963项工程回归、真实文字/照片/聊天、双尺寸页面、PG持久化与联合恢复的具体证据
见验收矩阵。生产部署及原业务库维护切换不包含在本轮完成状态中。
实际证据见[实施记录](diet-management-progress-2026-11-12.md)。
W04/W05/W06可在契约稳定后并行施工，
但不得各自建立一套记录或确认服务。每完成一个包更新状态页和测试证据；
只有W08全部通过才称本模块完整交付。
工程量与日历排期在W01/W02实际测量后估算，不虚构固定天数或已可用的外部能力。

## 18. 验收与需求追踪

以下测试文件名为原计划目标；会话C01–C24沿用
[场景矩阵](diet-conversation-tool-contract-2026-11-11.md#8-会话验收矩阵)。
2026-11-12实际证据与未过条件见[验收矩阵](diet-management-acceptance-2026-11-12.md)，
不能由本计划表直接推断已通过。
每项失败必须修复或由xerina明确改变范围，不能改名“后续版本”后宣称完整。

| 验收ID | 范围及工作包 | 计划证据与通过标准 |
| --- | --- | --- |
| A01 | D01，W01–W04 | test_diet_quantities.py：生熟、未知、区间、份数、批量/熟重、标签单位、禁止假设转换，定值断言全部通过 |
| A02 | D03/D04，W02–W04 | test_diet_plans.py：28餐批量、100条目、冲突选择、计划/实际独立、重复及再吃/撤销/恢复正确 |
| A03 | D05，W02/W03 | test_diet_transactions.py：同键异内容、双标签、确认丢响应、事务中断、COMMIT后Checkpoint前中断，只有一次有效写入 |
| A04 | D06，W01–W04 | test_diet_targets.py：生效点、同日/跨期/停用/移动日期、未来点保留、旧画像适配、并发epoch，历史不漂移 |
| A05 | D07，W01/W03/W04 | test_diet_summary.py：无记录、明确未食用、部分未知、全未知、周分母、负差值、不可比状态，前后端一致 |
| A06 | D08，W05 | test_diet_conversation.py：C01–C24全覆盖；语义歧义/否定/多事件/多草稿/明确批准/日期锚点/字段锁、旧确认兼容 |
| A07 | D02，W06 | test_diet_jobs.py：重启/取消/lease丢失/迟到/预算/限流，模型任务与聊天总并发不超配置，数据零自动提交 |
| A08 | D02/D09，W06/W07 | test_diet_media.py：伪格式/巨大像素/动画/EXIF/GPS/损坏/跨用户/符号链接/未引用清理、备份共享卷 |
| A09 | D09，W02/W03/W07 | test_diet_isolation.py与test_diet_purge_export.py：全部ID入口A/B用户交叉，CSV安全、私有下载、修订/缓存清除、恢复不复活 |
| A10 | 全范围，W02/W08 | test_diet_migration.py：老数据逐行核对、重复/缺数据/重跑/回滚策略，旧餐单/画像/确认回归 |
| A11 | D01–D09，W04–W08 | Playwright：1440×900、390×844，关键流程/刷新/键盘/冲突/失败截图；无重叠和无效按钮，图片确实可见 |
| A12 | D02/D08，W05/W06/W08 | 冻结至少60条文字、30组有权使用的照片（含标签）；固定标注匹配对象、主体/时态、数量口径和应澄清项 |
| A13 | D07/D09，W08 | 每用户10000条记录、20并发用户，CRUD与每日汇总服务端P95≤1秒、零越权/重复，记录主机/DB/索引/样本/错误率 |
| A14 | 全范围，W08 | Ruff/mypy/pytest、前端typecheck/build全部通过；真实PG/Redis/MCP/Worker/视觉、联合备份恢复实跑并留记录 |

A12统计规则：文字和图片分别报告食物匹配precision/recall，目标均≥90%；
按人工标注别名/等价匹配，错误候选不能被“随后用户改对”计作识别正确。
不支持或完全无法辨认的输入返回说明/手填入口，0自动正式写入；
普通餐照未给重量仍允许模型估算，不要求精准营养或秤重数据才可验收可用流程，
但必须保留估算标记；不能把模型提议或菜谱投料当成实测真值。
同时报告主体/时态误判、错误数量归属、来源与估算标记、失败样本及成本/延迟；
冻结后不删失败样本、不混合Fake和真实结果。阈值是验收目标，不是当前准确率。
营养误差只对确有秤重/标签真值样本评价，不能将菜谱估算当真实摄入金标准。

A13不含外部模型时延；解析另报排队、模型和整体P50/P95、超时、重试与恢复费用，
不承诺模型90秒内必然成功。测试规模代表该明确负载，不冒充无限用户容量。
安全、幂等、来源与零自动入账为硬门槛，不能用平均准确率抵消。

## 19. 设计归档与后续执行（2026-11-11原文）

本次交付的是实施基线、工具输入Schema和结构校验样例、项目指引与状态同步。
它们不是功能实现、数据库迁移或运行验收。
结构契约检查可复现运行：

```bash
.venv/bin/python docs/specs/check_diet_contract.py
.venv/bin/ruff check docs/specs/check_diet_contract.py
```

检查脚本使用现有开发环境中的jsonschema；若新环境未具备，
后续将其作为明确的开发依赖纳入锁文件，不在生产运行时依赖本检查脚本。
后续收到“按方案开始实现”时，先读本文件、Schema、CURRENT_STATUS及相关代码，
从W01/W02进入实现；不用再次讨论是否只做首版，也不自行删掉目标/图片/编辑等范围。
外部模型可用性、图片依赖安装授权、账户公开上线门槛与真实数据覆盖仍须实际解决和验收。
