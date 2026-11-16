# 饮食管理本地运行与备份恢复

本记录面向维护人员。业务库尚未进行维护切换；本机恢复验收仅使用隔离测试Schema和新建测试DB。
代码、功能与效果状态见[实施记录](diet-management-progress-2026-11-12.md)。

## 当前本地入口

- Web：`http://127.0.0.1:5191/#diet`；API：`http://127.0.0.1:8023/docs`。
  主API/Worker/Web保持运行；临时8025聊天验收服务已完成用途，不作为日常入口。
- 数据库`nutripilot_local_diet_20261112`已迁移0007，数据持久存入PostgreSQL；
  私有图片/导出/独立删除日志由本地配置指定。此库含验收账号和测试数据。
  原业务库`nutripilot`仍0003，原API8006/Web5178保持原状。
- 实际文字`qwen-plus`、视觉`qwen3-vl-plus`；新环境MCP为in-process，
  embedding/rerank为hashing/lexical。未将原BGE/HTTP MCP服务切换过来。
- 2026-11-13按本地使用要求取消浏览器令牌：项目内
  `.runtime/local-diet/environment.json`配置`API_ACCESS_TOKENS="{}"`、
  `API_LOCAL_USER_ID="1"`。服务端统一使用账号1，刷新页面直接进入；
  忽略客户端身份覆盖并保留资源归属校验与限流。该文件权限0600，不纳入Git或报告。
  此模式只用于绑定127.0.0.1的本地服务，能够访问本地服务的程序无需令牌即可操作账号1；
  生产模式禁止此配置。恢复令牌时先移除`API_LOCAL_USER_ID`，再配置令牌映射。
  测试曾更改账号时区，实际日期以“饮食记录”中的当前时区设置为准；旧记录日期不会随设置漂移。
- 供应商密钥沿用现有`.env`，未复制到上述本地参数文件；当前没有公开账户注册/恢复体系。

2026-11-13免令牌验证：API/Worker重启后健康检查200、Worker就绪；
无令牌与旧令牌访问profile/context均200，伪造账号头仍得到账号1。
全新浏览器六尺寸只读验证通过、0写请求，
证据`.runtime/diet-ui-20261113/no-auth/report.json`。
身份/限流与饮食API专项测试、Ruff和mypy通过；本轮未重跑真实模型或后端全量。

## 本地重新启动

仅在服务退出或需要维护时执行，当前无需重启。先核对
`.runtime/local-diet/pids.json`和实际进程，确认8023/5191空闲及主队列无在途任务；
不要仅因PID文件存在就向可能已复用的进程发信号。
API和Worker必须读取同一个参数文件、队列和私有目录。
**不要直接运行旧`start_real.sh`替代以下配置，它会使用原业务环境。**

在项目根目录的独立终端分别启动API和Worker。API：

```bash
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

os.environ.update(json.loads(Path(".runtime/local-diet/environment.json").read_text()))
python = str(Path.cwd() / ".venv/bin/python")
os.execv(python, [python, "-m", "uvicorn", "apps.api.main:app",
                 "--host", "127.0.0.1", "--port", "8023"])
PY
```

Worker：

```bash
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

os.environ.update(json.loads(Path(".runtime/local-diet/environment.json").read_text()))
python = str(Path.cwd() / ".venv/bin/python")
os.execv(python, [python, "-m", "nutripilot.worker"])
PY
```

Web：

```bash
VITE_API_BASE_URL=http://127.0.0.1:8023 npm --prefix web run dev -- \
  --host 127.0.0.1 --port 5191 --strictPort
```

启动后检查`http://127.0.0.1:8023/health/ready`为200且`chat_worker_ready=true`，
再直接访问页面，无需令牌。此前功能实际运行与重启回读证据见
[真实本地报告](diet-real-local-acceptance-2026-11-12.md)，文中命令不自动执行迁移或停止原服务。

## 必要条件

- 停止API及所有共享此库/目录的Worker，检查无在途写入；`--offline`是维护人员的显式
  停写声明，不会自动停服务，也不证明其它主机没有写入。
- `DATABASE_URL`指向实际源库或空恢复库；Docker容器中能用同名数据库运行
  `pg_dump/pg_restore`。默认容器`nutripilot-postgres-1`、数据库角色`nutripilot`。
- API与Worker共享`DIET_MEDIA_ROOT`、`DIET_EXPORT_ROOT`、
  `DIET_DELETION_JOURNAL_ROOT`。目录不在Web公开目录，目录0700、文件0600，拒绝符号链接。
- 独立删除日志必须使用最新版本，保留覆盖所有仍存在的备份，不可用旧快照覆盖。
  存储的是对象ID/版本/时间和无图片正文的媒体元数据，不是食品、聊天或模型原文。
- 静态加密和加密密钥隔离、异机副本、最小权限角色、管理员访问审计及实际30天销毁
  必须由部署运维落实；代码没有自动配置云KMS/磁盘加密或远程销毁，不能宣称已完成。

## 备份

在项目根目录、应用虚拟环境及正确环境变量下运行，备份目录必须不存在，父目录已存在：

```bash
.venv/bin/python scripts/backup_diet.py backup \
  --bundle /absolute/protected-backups/diet-20261112 --offline
```

工具备份整个指定PG数据库与私有图片，写入数据库/逐文件SHA-256、备份时刻、
30天`destroy_after`；不复制临时ZIP和正在写入的tmp，不把删除日志冻结进备份。
中途失败的目录没有完整manifest，不能用作恢复包。校验和用于发现意外损坏，
不代替经过认证的加密和可信存储。

将备份包置于受控加密离线存储；按manifest日期销毁所有副本。需要保留更久必须先改变
已告知用户的留存策略，不得只改脚本的拒绝条件。独立删除日志另做当前副本保护。

## 恢复

仅恢复到空数据库和空私有图片目录。不得对当前业务库执行drop/downgrade来腾空。
准备最新独立删除日志，确认环境变量指向恢复目标而不是原业务库：

```bash
.venv/bin/python scripts/backup_diet.py restore \
  --bundle /absolute/protected-backups/diet-20261112 --offline
```

工具先写`restore_pending`并持久化，PG及图片恢复后重放独立删除日志：
清除旧快照中的对应对象、草稿和修订，恢复墓碑；检查待删除图片无权威引用后清理，
撤销旧导出。成功后才解除门禁；API/Worker启动会拒绝存在此门禁的目录。
恢复后还须进行本人/跨用户查询、旧批准重放拒绝、私图读取、任务状态及迁移版本核对，
完成检查后才能开放流量。

任何恢复/重放失败都保持停止。保留失败证据，修复后在新的空目标重新恢复；
不得直接删除`restore_pending`绕过重放，也不得在失败目标上开放访问。
COMMIT结果未知或最终阶段回滚时，独立日志可能仍保存已批准的清除意图，恢复保守清除。
应用清除不承诺立即擦除不可变备份，不能把墓碑重放说成离线备份已经物理销毁。

## 已执行证据

`tests/integration/test_diet_persistence.py::test_real_isolated_schema_dump_and_restore`
实际运行Docker中的pg_dump/pg_restore、备份/恢复私有PNG、重放当前独立日志两次，
验证记录和图片均未复活、旧确认只返回已清除回执。专项另验证日志不可写时PG清除回滚。
这不是业务库维护切换，也不代表生产加密、异机容灾或30天真实销毁已经配置。

`tests/integration/test_diet_backup_cli.py`另新建两个一次性数据库，直接运行脚本execute编排：
完整DB dump和私图备份→备份后清除→恢复到另一空DB/空媒体→重放当前独立日志→解除门禁。
验证旧记录/图片不复活，旧确认返回purged，非空目标拒绝再次恢复；只清理其新建的两个DB。
该测试实跑了脚本内Docker命令，不是正式业务停机演练，也不验证远程副本或实际30天销毁。
