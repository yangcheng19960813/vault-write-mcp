# 编码代理指南

## 项目范围与依据

- 本项目是独立运行的 Markdown 写入 HTTP MCP 服务，使用官方 MCP Python SDK 的 Streamable HTTP；不依赖 Obsidian，不承担 QMD 的检索、索引或部署。
- 支持 Python 3.11+、Linux/macOS 和本地普通 Git 仓库；双分支同步需要 Git 2.38+。不要声称支持 Windows、NFS、linked worktree、bare Vault 或跨机器共享文件锁。
- 开始前阅读 `README.md`、`pyproject.toml`、`config.example.toml`、相关实现和测试；检查工作区差异，保留用户及其他代理的改动。若存在 `.agents/skills`，只读取相关技能。
- `docs/sync-plan.md` 与 `docs/optimization-plan-2026-10-27.md` 是设计/计划资料，不等于实现现状，也不构成实施、迁移、发布或部署授权；按用户当前明确范围工作。

## 代码与测试地图

| 文件 | 职责 | 主要测试 |
| --- | --- | --- |
| `src/vault_write_mcp/store.py` | 路径/版本校验、同库锁、原子写入、Git 提交、SQLite 回执与事务恢复 | `tests/test_store.py`、`tests/test_atomic_recovery.py` |
| `editing.py` | 局部读取、字面查找、行锚点补丁与上下文预算 | `tests/test_editing.py`、`tests/test_context.py` |
| `links.py`、`moving.py` | 保守解析引用、只读移动预览、版本化多文件移动事务 | `tests/test_links.py`、`tests/test_moving.py` |
| `batch.py`、`worker.py` | interval 待提交批次、flush、普通 push 和后台协调 | `tests/test_batch.py`、`tests/test_http.py` |
| `sync.py` | 独立同步锁、隔离合并、发布快照、可恢复 checkout 与只读状态 | `tests/test_sync.py` |
| `config.py`、`messages.py` | 严格配置、可选模型提交信息与失败策略 | `tests/test_config_messages.py` |
| `server.py` | CLI、鉴权、HTTP MCP 工具与结构化错误 | `tests/test_http.py` |

表中省略目录的模块均位于 `src/vault_write_mcp/`。`examples/` 是验证示例，不是正式库操作脚本。

## 开发与验证

在项目根目录使用锁定依赖与项目虚拟环境：

```sh
uv sync --locked --extra test
uv run pytest -q
```

- 若继承了其他项目的 `VIRTUAL_ENV` / `UV_PROJECT_ENVIRONMENT`，先在当前命令环境解除或明确隔离，避免安装到其他环境；不要修改用户全局 Python、Git 配置或凭据。依赖变更需说明理由，并同步维护 `pyproject.toml` 与 `uv.lock`。
- 迭代时先运行受影响测试，例如 `uv run pytest -q tests/test_editing.py tests/test_context.py`；最终按改动范围运行完整套件。不要把局部通过描述为全套通过。
- 可补充运行 `uv run python examples/editing_benchmark.py` 与 `uv run python examples/move_reference_demo.py`；两者自建并清理临时 Vault。
- HTTP 验证用 `uv run python examples/smoke_client.py --url http://127.0.0.1:<测试端口>/mcp`。必须先按 README 在全新隔离临时 Vault 启动测试服务，并核实 URL、端口、进程和测试令牌；此脚本会写入所连接的库，不能连接现有或正式服务。
- 所有测试、故障注入、示例和 Git 同步实验只能使用临时 Vault、测试 remote 与测试配置；禁止在生产 Vault 或用户真实知识库上试运行。不要为验证而开启真实模型调用或真实远端推送。
- CI 矩阵见 `.github/workflows/test.yml`（Linux/macOS、Python 3.11/3.13）；本地通过不代表远端 CI 已运行，也不代表已验证断电或硬件故障。

## 不可弱化的数据一致性约束

- 每次写入必须检查整篇原始 UTF-8 字节的 `sha256` 版本；新建使用 `missing`。不增加 blind/force 覆盖，不把整篇校验降级为局部行校验。
- 行锚点包含行号及换行敏感哈希；同一补丁的所有锚点来自修改前同一快照。任何无效、过期或重叠操作都必须整次拒绝，不能部分落盘。保留未触及字节、BOM、LF/CRLF 和末尾换行语义。
- `request_id` 在整个 Vault 共享。相同编号与完全相同参数须持久化重放原回执；参数改变须用新编号，同编号不同参数返回冲突。重放不能根据后续正文重新生成上下文，也不能重复写入、提交、移动或模型请求。
- 保持 `.git/vault-write/write.lock` 对恢复、读取、版本校验、正文、ref/index 和回执的协调；同步另用 `sync.lock`，网络与隔离合并不能无意占住写锁。
- 事务日志先于正文写入；保留同文件系统私有暂存、`fsync`、原子替换、ref 比较更新、私有 Git index 与实际进程退出恢复协议。外部正文、分支或 index 变化须安全拒绝并保留现场。
- interval 成功表示正文与回执持久保存，不表示已有提交或已发布；保持 `pending`、批次 flush、重放后提交号及同步状态的语义区别。
- 不要删除 `pending.json`、回执数据库或未知临时文件来绕过恢复；只按已有所有权登记规则清理。发现 `RECOVERY_REQUIRED` 时先诊断，不能用强制 reset 或覆盖文件“修复”。

## 移动、解析和接口兼容

- 移动必须先只读预览，再校验完整 `expected_versions`、`plan_version` 和拓扑；应用前重算，预检、版本或验证失败须在任何修改前拒绝。提交阶段中断须保留日志并按可恢复事务处理；普通文件系统读者可能观察到逐文件替换的中间状态，不承诺多文件原子可见性。预览只覆盖本地已提交快照，不保证尚未同步的远端 backlink。
- 链接解析坚持 fail closed：支持范围以 README 和测试为准；对含糊绑定、未支持语法、容器、编码或路径逃逸明确拒绝，不猜测、不静默漏改。扩展语法前补反例、字节保留和预算测试。
- 保留硬上限：笔记 1 MiB；局部读取/上下文 200 行与 16 KiB；补丁 100 操作、上下文距离 0–50；移动扫描 2,000 跟踪路径、1,000 篇笔记、16 MiB 正文、100 受影响路径，每篇 2,000/全库 20,000 引用；摘要 50 项与 16 KiB。上限以源码常量为准，调整须明确评估资源与兼容性。
- 新增可选能力保持默认关闭/原行为及响应形状兼容；严格验证未知字段和类型。当前默认认证开启、逐次本地提交、模型/自动 push/sync 关闭，不得顺手更改。
- MCP 错误须同时保留 `isError` 与 `structuredContent.ok/error.code` 契约；HTTP 200 不等于工具成功。`actor` 只是调用方审计标签，不是已认证用户身份。

## Git、隐私与交付

- 代理自身的 commit、push、PR、合并和部署遵守用户明确授权；编写方案或本地改动不自动授权发布。禁止 force push、破坏性 reset，以及自动用 ours/theirs 或模型裁决正文冲突。
- 上条不禁止应用已有的显式配置行为：启用 sync 后，服务按协议普通推送写入分支及隔离合并结果到配置的 main；测试只能用本地隔离 remote。不要把该行为误改成禁止 main 发布，也不要借它授权代理发布项目代码。
- 不读取无关私有 Vault，不擅自在日志、测试夹具、提交或回复中披露真实私有正文；允许合成测试文本，以及用户授权范围内完成任务所必需的内容。Bearer token、模型 Key 或带凭据的 remote URL 等秘密不得写入日志、夹具、提交或回复。模型 Key 仅从配置指定的环境变量读取；模型默认关闭，启用时只发送有界 diff，不扩大为整库上传。
- 修改锁、恢复、幂等、移动或同步时，补充失败/中断/并发/外部干扰回归测试，并尽量安排独立审查；不要将某次任务的特定模型分工固化为仓库要求。
- 每次代码改动同步维护 `CHANGELOG.md`：记录实际变更、兼容/安全影响和实际执行的验证。接口、配置或行为变化同步更新 README、示例及相应测试；不要覆盖他人正在编辑的文档。
- 交付先报告结果，再列实际命令与通过/失败/未运行状态、阻碍及剩余风险。没有证据不要声称已实现、已测试、已修复、已推送或已部署；计划项与完成项必须分开。
