# 变更记录

项目级变更按时间顺序追加。日期统一按 UTC+8；条目区分已合并实现与文档提案，不将计划写成已实现功能。

## 2026-10-27

### PR #1｜局部编辑上下文与原子恢复

- **修改内容：** 为查找结果增加受限上下文窗口；补丁成功后可返回新锚点及局部上下文，并支持同一请求重放。修复并发同步状态清理竞态；完善锚点重放上下文及原子事务恢复。
- **动机与兼容性：** 减少连续编辑时反复读取的需要。上下文为可选功能、具有硬上限；默认响应、版本及锚点校验保持兼容，写入仍要求完整版本匹配。
- **证据：** [PR #1](https://github.com/yayalee-last/vault-write-mcp/pull/1) 于 2026-10-27 10:59:21（UTC+8）合并；最终提交 `c27cc6ce3c40b4d6697e5cef5a5b4047666756f2`；最终 262 项测试、4 组矩阵检查通过。初期基准任务由 6 次降至 3 次 Store 调用，紧凑 JSON UTF-8 字节数由 2,242 降至 1,946；这是调用数与字节数，不是模型 token 测量。

### PR #2｜单篇笔记安全移动/重命名并更新引用

- **修改内容：** 增加只读移动预览、严格版本/计划守卫、受影响引用更新、可恢复事务及幂等重放；同时更新支持的传入与传出链接。
- **边界与兼容性：** 仅支持单篇笔记移动，不移动目录；对不支持的链接语法明确拒绝，不猜测式改写。
- **证据：** [PR #2](https://github.com/yayalee-last/vault-write-mcp/pull/2) 于 2026-10-27 11:00:39（UTC+8）合并；Python 3.11 与 3.13 独立验收最终 422 项测试通过。

### 基线与本次审计

- 本地树 `b8e1602ab0c2b50938ce10f1760de9ef9516b49e` 与 main 提交 `0add5a0e3a66acc0d937a1bcf55b64eba9723a60` 一致；[main CI](https://github.com/yayalee-last/vault-write-mcp/actions/runs/40441734955) 成功。项目仍为 `0.1.0` 原型；不据此推断已有正式发布。
- 本次审计为只读检查，未修改代码、未重跑全套测试。开发跨越午夜，日期与时间均按 UTC+8 记录。

### 本文档 PR｜记录规范与优化计划

- **内容：** 新增根目录 `AGENTS.md`（项目协作/变更记录约定）、本文件及 [`docs/optimization-plan-2026-10-27.md`](docs/optimization-plan-2026-10-27.md)（后续优化提案）。
- **状态：** 作为独立文档草稿 PR 提交，待审阅，尚未合并；该条目只记录文档整理，不表示计划中的代码功能已实现。已对照源码复核文档，并检查 UTF-8、内部链接及空白格式；本次未运行功能测试。

### 实施｜实际推送目标检查点

- **状态：** 实施中，待独立验收。
- **修改内容：** 普通自动 push 检查点绑定 Git 实际展开的全部 push URL 摘要及目标分支；支持多 pushurl、操作员 URL 别名、旧/损坏检查点重检，错误不回显 URL 或凭据。
- **兼容性：** 不做 SSH/HTTPS 或凭据等价推断；顺序/重复 URL 不改变身份。失败保留最后成功检查点，仍只做普通非强制推送。
- **验证：** 增加临时本地 remote 改址、多 pushurl、全局别名、分支、失败脱敏回归；`uv run --python 3.11 pytest -q tests/test_batch.py`：24 通过；改动前基线全套：422 通过。尚待最终全套与独立验收。

### 实施｜统一路径写保护

- **状态：** 实施中，待独立验收。
- **修改内容：** 新增默认不限制的 `[write_policy]` allow/deny 路径规则；NFC+casefold、目录边界匹配、拒绝优先，覆盖 write/append/patch/move 全部受影响路径及 interval flush。只读移动预检禁用 Git 可选 index 刷新。
- **兼容/安全：** 显式启用后当前规则也约束回执重放；拒绝保留原回执。旧事务在构造器 schema/锁/清理之前整组预检，拒绝需操作者恢复允许策略；不授权删除日志。不限制读、外部编辑或显式 Git sync checkout。
- **验证：** 增加工具入口、目录边界、Unicode/大小写、严格配置、移动 backlink、重放与实际进程退出后策略变更测试；`uv run --python 3.11 pytest -q tests/test_policy.py tests/test_config_messages.py tests/test_moving.py tests/test_batch.py`：147 通过。最终全套和独立验收待进行。

### 实施｜只读恢复诊断与可选重命名历史

- **状态：** 实施中，待独立验收。
- **修改内容：** 独立 `doctor --vault [--config]` CLI 不创建 Store、目录、锁，不恢复/清理/刷新 index，不联网；固定标签与数量输出，损坏或恶意日志不回显任意内容。`note_history` 新增默认关闭的 `follow_renames`，明确标记 Git 相似度启发式。
- **兼容/限制：** 默认 history 响应不变。doctor 不提供具体冲突路径或安全恢复保证；无锁快照可能不一致，immutable SQLite 不读取 WAL 最新内容，stat 缓存过旧可显示为变化，不保证 atime。全部诊断输入/输出有界。
- **验证：** 增加实际退出四阶段、所有权临时文件保留、构造/恢复禁止调用、完整目录快照、损坏/超大/符号链接/FIFO/SQLite/WAL、CLI 无令牌与相似文件历史测试；`uv run --python 3.11 pytest -q tests/test_diagnostics.py tests/test_http.py`：46 通过；policy+diagnostics 联合：52 通过；`uv run --python 3.11 python examples/doctor_demo.py` 通过。最终全套和独立验收待进行。

### 修订｜规则语法与实际路径分离

- **状态：** 实施中，待独立验收。
- **修改内容：** 独立审查发现保护策略将实际文件名误当规则校验；改为对 Store 已校验的实际路径仅作 NFC+casefold 比较，保留字面 `[]?*` 文件名及既有长度语义。doctor 同样不将实际日志路径当 glob 规则。
- **验证：** 增加三种字面文件名写入/追加回归；最终合并测试结果后追加。

### 修订｜推送 URL 分隔歧义拒绝

- **状态：** 实施中，待独立验收。
- **修改内容：** 用 NUL 分隔的配置项独立核对实际 push URL 数量，拒绝含控制符或换行歧义的目标，避免一个特殊 URL 与多个 URL 得到同一检查点身份。
- **验证：** 临时本地 remote 控制符/换行回归，失败不回显配置且保留成功检查点；待最终全套验证。

### 实施｜可选紧凑查找上下文

- **状态：** 实施中，待独立验收。
- **修改内容：** `find_note` 新增严格布尔 `compact=false`；启用时正文/锚点只出现在合并窗口一次，匹配使用 1 起始行号与 0 起始窗口/行索引定位。默认响应、匹配分页、版本与锚点不变，仍有 200 行/16 KiB 渲染行上限。
- **验证：** 增加重复命中、BOM/CRLF、长行、重叠窗口、分页、截断、边界和 MCP 参数测试；固定合成 benchmark 计量结果 JSON 与实际 ASGI MCP response body，不推算 token。待集成后重跑全套和计量。

### 补充验收｜策略恢复与预检无副作用

- **状态：** 本地测试中，待独立验收。
- **修改内容：** 补充 interval 写入/批次提交实际退出后策略拒绝、伪造遗漏受保护 backlink 的移动参数、拒绝前不清理已登记临时文件，以及 doctor 字面文件名诊断回归；未扩大功能范围。
- **验证：** Python 3.11 policy+doctor 联合 58 项通过（含真实 SQLite 热日志和权限拒绝）；最终双版本全套待记录。

### 修订｜诊断 SQLite 未完成事务

- **状态：** 实施中，待独立验收。
- **修改内容：** 独立审查发现 immutable 主数据库忽略 hot rollback journal 时不能声称观察完整；doctor 同时报告 WAL/SHM/rollback journal，任一存在或权限不足无法检查均标记不完整且不尝试恢复；旁文件检查使用不跟随符号链接的 lstat，错误不回显路径。
- **验证：** 新增 SQLite 实际进程退出留下 hot rollback journal 的全目录不变回归；Python 3.11 policy+doctor 联合 58 项通过（含真实 SQLite 热日志和权限拒绝）；最终双版本全套待记录。

### 四项集成本地验证｜待独立验收

- **状态：** 四项已实施，本地双版本全套通过；Astra 独立验收进行中，未合并或部署。原计划后续的移动性能/路径分页、模型总时限及恢复权限专项均未实施。
- **测试快照：** 代码与测试提交 `1a1d2f34c21c67c36fac734266083e1cd8ecc72f`（本条之后仅更新文档证据）；依赖仍按原 `uv.lock`，无依赖/版本号变更。
- **完整验证：** 使用 `UV_CACHE_DIR=/tmp/vault-write-uv-cache`、`UV_PYTHON_INSTALL_DIR=/tmp/vault-write-python` 隔离执行：
  - `env -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT ... uv sync --locked --extra test --python 3.11`；`... uv run --python 3.11 pytest -q`：**513 通过**（Python 3.11.17，101.62 秒）。
  - `env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=.venv313 ... uv sync --locked --extra test --python 3.13`；`... uv run --python 3.13 pytest -q`：**513 通过**（Python 3.13.5，103.34 秒）。
  - 改动前 Python 3.11 基线 422 通过；最终 `git diff --check` 通过。仅在 Linux 本地运行，macOS 与远端 CI 仍待实际运行；未模拟断电、硬件故障或生产 Vault。
- **示例：** `uv run --python 3.11 python examples/{compact_find_benchmark,editing_benchmark,move_reference_demo,doctor_demo}.py` 逐个执行均通过；doctor 示例也在 Python 3.13 通过。全部仅使用新建临时 Vault/本地 remote，无生产数据、真实模型或真实外网推送。
- **计量：** 集成后的固定合成样本结果 JSON 默认/紧凑为 **4,849 / 2,647 UTF-8 字节**；ASGI MCP HTTP response body 为 **9,871 / 5,485 字节**（不含 headers/framing/TCP/TLS，不是模型 token）。小输入不保证节省。
- **保留边界：** 策略不限制显式 Git sync checkout；doctor 不回显任意路径或定位具体冲突，不证明可恢复，无锁/immutable 观测存在 WAL/SHM/热日志/权限未知时不完整，输入上限不是严格 RSS 上限；重命名历史仅 Git 相似度启发式。详情见 README。

### 四项独立验收｜通过（Linux 本地）

- **结论：** Astra 独立验收通过最终实现快照 `ce61634f01583f2b7bbbffcc3ad34ed79c553bfe`；其 `src`/`tests` 与独立克隆测试快照 `1a1d2f34c21c67c36fac734266083e1cd8ecc72f` 树一致，差异仅为已检查的文档。本条之后仍仅更新验收记录/计划状态。
- **独立完整验证：** Python 3.11.17：**513 通过**（142.82 秒）；Python 3.13.5：**513 通过**（149.84 秒），使用锁定依赖、隔离 regular clone 及临时 Vault。
- **补充独立证据：** 每版本 **14 项**真实 Git/进程/文件系统探针及 **350 项**确定性随机紧凑响应比较通过；覆盖多 pushurl 部分失败、凭据型错误脱敏、完整受影响路径保护、实际进程退出/恢复/重放、doctor 无副作用与恶意/损坏状态、Unicode/CRLF/边界预算。独立复现紧凑计量 **4,849/2,647 JSON 字节**与 **9,871/5,485 MCP body 字节**；doctor 示例与 `git diff --check` 通过。
- **审查修订：** 已修正实际路径误套规则语法、hot rollback journal 误判观察完整、权限不足旁文件检查泄漏路径三项；无剩余阻塞。
- **限制不变：** 仅本地 Linux 双 Python 版本验收；macOS/远端 CI 尚待实际运行，未做断电、硬件故障、生产 Vault 或真实模型验证。本次未合并或部署；远端草稿 PR 及其 CI 由授权发布步骤另行核实。

### 远端 CI 调查｜同步竞态失败诊断（验收重新打开）

- **状态：** 远端 push workflow `40532996351`（Git 2.55.0、Python 3.13.16、Ubuntu）出现 512 通过/1 失败；同一代码树的 PR workflow 两个 Linux job 通过。尚未确认根因，不将重试或诊断改动描述为修复。
- **观测：** `test_push_main_race_retries_without_loss` 丢弃 sync 结果后访问空 `advanced[0]`；仅此不能证明竞态钩子未执行，也可能是钩子中的合成 main 写入先失败。
- **修改内容：** 仅强化测试诊断，保留并检查 sync stage/error、钩子阶段和合成夹具异常；增加未发布/未应用、writer/远端分支、重试后双方正文保留断言。不删除或放宽原竞态拒绝/重试/不丢历史的断言，未作推测性生产代码改动。
- **验证：** 改动前 Git 2.52.0/Python 3.13.5 的 200 次独立合成复现均通过；另以 Python 3.13.16/Git 2.52.0 运行 50 次复现全部通过，强化后的 sync 全套 43 项通过；本地已在 `/tmp` 从官方源码构建 Git 2.55.0（无 Rust/HTTP 扩展），匹配版本探针进行中。后续 CI 结果另行追加。

## 2026-10-28

### 修复｜doctor 不再触发 Git 内容过滤器或 promisor transport

- **状态：** PR #4 修复已通过 Linux 本地双版本及独立验收，待远端 Linux/macOS 矩阵验证；未合并、未部署。
- **问题证据：** 在实际 PR 提交 `8d1e7fd811cce3d80f9b89127561ab8fbe93f6b4` 的隔离临时 Vault 中，racy-clean index 会令 `diff-files --quiet` 执行 clean 内容过滤器，创建 Vault 之外的合成 marker，即使目录快照不变且结果声称 `read_only=true`。独立检查也发现缺失 promisor tree 的 `diff-index` 可启动 transport；旧 513 项套件未覆盖这两类副作用。
- **修改内容：** 完全移除 doctor 的 `diff-files`/`diff-index`，Git 仅查询根目录、HEAD/ref 元数据，防御性禁用 lazy fetch 与 replace object；不尝试逐项覆盖不可信过滤器配置。增加真实 clean/process 可执行 marker、缺失 promisor tree 的本地无网络 transport marker，以及 Git 查询范围回归。
- **兼容/安全：** 保留结果形状，`tracked_changes` 有意始终为 `unknown`，不再提供工作区/暂存区干净与否判断；HEAD 仅是格式已校验的引用 ID，不验证对象存在/完整性。README 与临时库演示同步说明此边界。其余四项功能、依赖和配置默认值不变；原始 CI 同步竞态偶发异常的根因仍未知，本修复不声称解决该异常。
- **验证：** 同一组 10 项真实 marker 回归在原实现上全部失败（clean/process 8 项、promisor transport 2 项，均覆盖直接/CLI）；修复后的 11 项新增回归全部通过。使用原锁定依赖和隔离环境执行完整 `python -m pytest -q`：Linux/Python 3.11.17 **524 通过**（158.36 秒），3.13.5 **524 通过**（163.71 秒）；独立全套分别 **524 通过**（156.76 / 160.24 秒）。独立审查及每版本额外 10 项真实 helper/完整 bytes、mode、mtime 快照探针通过，包含 fsmonitor/hooks/extdiff/textconv/alias 与缺失 promisor HEAD/tree。
- **示例/限制：** `doctor_demo.py` 本地与独立检查均在两个 Python 版本通过；本地 Python 3.11 其余三个 benchmark/move 示例、Python 3.13 `compileall`、`git diff --check` 通过。所有探针仅使用临时 Vault 与本地合成 helper，无真实网络、模型、生产 Vault 或部署；macOS/远端 CI 尚未运行本修复，发布后核实。

## 追加模板

```markdown
### YYYY-MM-DD｜变更标题（PR/提交）
- **状态：** 提案 / 实施中 / 已验收
- **修改内容：** …
- **动机与兼容性：** …
- **证据：** PR、提交、测试命令与结果；限制/未覆盖项：…
```

新增条目应追加在对应日期下；仅在实现和验证有证据后标记“已验收”，保留历史记录，不覆盖旧条目。
