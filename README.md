# VaultWrite MCP

独立运行的 Markdown 写入服务。多个 agent 连接同一个 HTTP MCP，强制检查内容版本；默认逐次生成本地 Git 提交，也可配置定时批量提交、模型生成提交信息和自动 push。无需运行 Obsidian。

这是可测试的 0.1 原型。支持 Linux/macOS、Python 3.11+ 和 Git，使用[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)的 Streamable HTTP。

## 快速测试

在本项目目录安装依赖，然后创建一个全新的隔离 Vault：

```sh
uv sync --locked --extra test
uv run vault-write-mcp init --vault /tmp/vault-write-demo
export VAULT_WRITE_TOKEN="$(openssl rand -hex 32)"
uv run vault-write-mcp serve --vault /tmp/vault-write-demo
```

默认端点是 `http://127.0.0.1:18182/mcp`。默认启用认证，客户端携带 `Authorization: Bearer <VAULT_WRITE_TOKEN>`；可信私网可按下节配置关闭认证。服务启动时就会检查和恢复未完成事务。

在另一个终端设置相同的 `VAULT_WRITE_TOKEN`，运行官方 SDK 客户端示例：

```sh
uv run python examples/smoke_client.py
```

示例创建一篇名称唯一的笔记，通过两个独立 MCP 客户端同时追加，验证只有一方成功，另一方重读重试后双方内容都保留；最后验证重复请求不重复追加。

Ubuntu 上可把 `--host` 设为本机实际的 Tailscale IP，例如：

```sh
uv run vault-write-mcp serve --vault /path/to/test-vault --host 100.x.y.z --port 18182
```

用真实 IP 替换示例。客户端随后连接 `http://<该 IP>:18182/mcp`。Host 校验使用这个明确的地址；反向代理、浏览器跨域和公网 TLS 部署尚未配置。先使用隔离库测试。

## 私网无令牌连接

在 TOML 中设置：

```toml
[auth]
enabled = false
```

然后用 `serve --config /path/to/vault-write.toml --host <实际的 Tailscale IPv4>` 启动。客户端只配置 MCP URL，无需 Authorization 请求头；服务不再要求 `VAULT_WRITE_TOKEN`。这种模式仅允许回环地址或 `100.64.0.0/10` 地址监听；可访问该私网端点的设备均可调用读写工具。版本前置条件、文件锁、请求去重和 Git 行为保持不变。

`auth.enabled` 默认为 `true`，原有令牌部署无需修改。模型 API Key 与 MCP 访问认证独立，关闭访问认证不会启用模型调用。

## 可配置的提交与模型调用

不传 `--config` 时保持默认行为：每次有效写入立即提交 Git，模型调用和自动 push 均关闭。完整字段与默认值见 [`config.example.toml`](config.example.toml)。配置文件使用 Python 标准库读取，不引入 Agent SDK；HTTP 请求复用已有的 httpx。

```sh
uv run vault-write-mcp serve --vault /path/to/test-vault --config /path/to/vault-write.toml
```

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `auth.enabled` | `true` | MCP bearer 认证；可信私网可设为 `false`，客户端仅需 URL |
| `commit.mode` | `immediate` | `immediate` 逐次提交；`interval` 定时汇总提交 |
| `commit.interval_seconds` | `300` | 批次提交与自动 push 的检查间隔，1–86400 秒 |
| `model.enabled` | `false` | 开启模型生成 commit 信息 |
| `model.api_url` / `model.model` | 空 | 完整 Chat Completions URL 与提供商模型名称 |
| `model.api_key_env` | `VAULT_WRITE_MODEL_API_KEY` | 存放模型 API Key 的环境变量名称 |
| `model.timeout_seconds` | `10` | HTTP 超时秒数 |
| `model.max_diff_chars` | `12000` | 发给模型的 diff 字符上限，超出部分明确标记截断 |
| `model.max_output_tokens` | `256` | 模型输出 token 上限 |
| `model.token_limit_field` | `max_completion_tokens` | 旧兼容接口可使用 `max_tokens` |
| `model.failure_policy` | `fallback` | `fallback` 使用默认信息；`defer` 失败后保留改动，等待下次尝试 |
| `push.enabled` / `push.remote` | `false` / `origin` | 自动推送开关与已配置的 Git remote 名称 |
| `push.branch` | 空 | 空值跟随当前分支，也可指定远端目标分支 |
| `sync.enabled` / `sync.main_branch` | `false` / `main` | 开启双分支同步；要求 push.enabled，当前写入分支与 main 不同 |

模型功能启用时，从配置指定的环境变量读取 Key；TOML 不接受明文 `api_key` 字段。服务的 `VAULT_WRITE_TOKEN` 与模型 API Key 是两份独立凭据。配置加载失败或开启模型但缺少 Key 时，启动会报错。修改配置后需重启服务。

模型接口采用 [Chat Completions 格式](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)：只发送本次提交涉及的 diff，不发送整个知识库；每次有变化的提交尝试至多发一次 HTTP 请求，没有工具调用和自动 HTTP 重试。OpenAI 已将 `max_tokens` 标为弃用，默认使用 `max_completion_tokens`；其他兼容提供商按其支持情况配置。HTTP 重定向不会携带凭据继续访问。

### 定时模式的确认与恢复

将 `commit.mode` 设为 `interval`，可在一个周期内将多次修改汇总成一条提交。没有待提交变化或内容最终恢复原样时，不调用模型，也不创建空提交。若选择 `immediate` 并启用模型，每次有效写入都会调用一次模型。

定时模式的写入仍在同库锁内强制校验版本、原子落盘并保存请求回执。写入成功返回 `pending: true, commit: null`，表示已持久保存，尚未生成 Git 提交；批次提交完成后，重放原请求会得到 `pending: false` 和批次提交号。请求最初返回的内容版本保留，批次 Git 历史只保存最终快照，周期内的中间版本不会各自形成提交。

未提交数据与去重回执在重启后仍然保留；事务日志分别处理待提交写入和批次 Git 提交的中断。外部直接修改笔记、分支或暂存区会导致安全拒绝。切回逐次提交前须先 flush 待提交批次。可手动执行：

```sh
uv run vault-write-mcp flush --vault /path/to/test-vault --config /path/to/vault-write.toml
```

`flush` 无需 MCP 令牌；它提交批次并按配置 push，启用 sync 时协调完整同步。后台只在 serve 期间运行；批次提交和模型请求仍持有同库锁，双分支同步的网络与隔离合并不持有写锁。


`failure_policy = "defer"` 时，模型失败不创建提交、不清除待提交批次，后台在下个周期再尝试；逐次模式则拒绝本次写入，文件保持原样。`fallback` 会使用默认信息继续提交。模型返回的内容只作为单行提交主题使用，不能执行 Git 或文件操作。

### 自动 push

未启用 sync 时，服务只做普通 push，不 force、不 pull，不自动解决远端分歧。先在测试 Vault 配好 remote 与 Git 认证；首次推送当前 HEAD 及历史，之后仅 HEAD 或目标变化时推送。检查周期复用 commit.interval_seconds。


成功推送的检查点保存在 `.git/vault-write/push.json`，重启后沿用。推送失败不会撤销本地提交，也不会重新调用模型生成信息；后台在下个周期重试。API Key 不参与 Git 认证。

### 双分支同步（默认关闭）

需要 Git 2.38+（`merge-tree --write-tree`）。本地人工编辑 main，服务独占另一个普通 clone 的 ubuntu 分支；QMD 使用独立 main 只读副本。本功能不创建或切换分支，不部署正式环境。先在隔离测试远端准备副本：

```sh
git clone --branch main /path/to/test-remote.git /tmp/vault-write-ubuntu
git -C /tmp/vault-write-ubuntu switch -c ubuntu
```

配置 `[push] enabled=true`、`remote="origin"`、`branch=""`；配置 `[sync] enabled=true`、`main_branch="main"`。当前写入分支必须与 main 不同，push.branch 只能为空或等于写入分支。同步周期沿用 commit.interval_seconds；CLI 可直接执行：

```sh
uv run vault-write-mcp sync --vault /tmp/vault-write-ubuntu --config /path/to/vault-write.toml
uv run vault-write-mcp sync-status --vault /tmp/vault-write-ubuntu --config /path/to/vault-write.toml
```

每轮提交 interval 批次，将固定写入快照普通推送到 ubuntu，再获取 main，在临时 bare 库合并，普通推送结果到 main。网络及隔离合并不占写锁，多进程同步由独立 sync.lock 串行协调。应用前重新核对写入分支、HEAD、待提交批次及外部改动；期间出现新写入则保留新内容，下轮接续。

冲突只阻塞同步，写入、提交及 ubuntu 推送继续；真实笔记不出现合并冲突标记。用户在自己的 main 副本 fetch ubuntu，merge 后人工解决并正常 push main；服务下一轮重新合并最新双方历史，保留冲突期间新增提交。不会 force push、自动选择 ours/theirs 或请求模型裁决正文。

写入成功仅表示已保存。只读 MCP sync_status 与 CLI 状态显示最后一轮 last_state、快照、published/applied、target、completed_at（UTC），以及当前 writer_commit、pending_count、current。上轮成功后有新写入时 state 为 pending、current 为 false；目标配置变化回到 idle。current 仅确认已知发布快照与当前写入提交相符，不代表刚查询过远端。冲突提供最多 50 个路径及总数，错误保存阶段与错误码，不保存可能含凭据的 transport stderr。状态读取不触发网络或模型请求。

应用使用 sync_checkout 持久事务，逐文件落盘后更新 ref/index；进程退出后沿 pending.json 恢复，外部正文或索引变化会保留现场并拒绝覆盖。同步支持普通 Git 文件，包括隐藏配置和二进制附件，保持原始字节及权限；拒绝符号链接、gitlink、路径逃逸、大小写/Unicode 规范化碰撞和文件/目录拓扑转换。外部暂存、未跟踪及未提交修改会阻塞应用；MCP 写入 API 仍只接受可见 UTF-8 Markdown。


## 工具

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `read_note` | `path` | 返回正文、`exists` 和当前 `version`；不存在时版本为 `missing` |
| `read_note_range` | `path`, `start_line=1`, `max_lines=80` | 返回带锚点的局部行和整篇版本；最多 200 行、16 KiB |
| `find_note` | `path`, `text`, `start_line=1`, `max_matches=10` | 在一篇笔记内做区分大小写的字面查找，返回命中行和整篇版本；最多 50 个命中、16 KiB |
| `write_note` | `path`, `content`, `expected_version`, `request_id`, `actor` | 创建或整篇替换；版本必须匹配 |
| `append_note` | 同上 | 在版本检查和同一把锁内读取、拼接、写回；原样追加，不自动补换行 |
| `patch_note` | `path`, `edits`, `expected_version`, `request_id`, `actor` | 根据行锚点原子替换、删除、插入；所有操作成功才落盘 |
| `note_history` | `path`, `limit=20` | 返回修改该笔记的 Git 提交，最多 100 条 |
| `sync_status` | 无 | 只读同步状态、当前写入提交与待提交数，不触发同步 |

现有笔记的版本形如 `sha256:<64 位小写十六进制>`，按原始 UTF-8 字节计算。创建新笔记也必须显式提交 `expected_version: "missing"`；它只能匹配真正不存在的文件。服务不提供 `blind`、强制覆盖或关闭版本校验的参数。BOM、CRLF 和末尾换行会原样保留。

逐次模式成功写入返回 `version`、`commit`、`changed`、`request_id` 和 `replayed`。正文没有变化时 `changed: false`，不会额外创建空提交。该模式的提交只包含该笔记，正文记录 `VaultWrite-Actor` 和 `VaultWrite-Request`；定时模式的批次提交记录涉及的 actor 和请求编号。

`actor` 是调用方提供的审计标签，不是经过独立认证的用户身份。默认使用共享 bearer token；私网可按上述配置关闭认证。

### 局部读取与锚点修改

优先用 `find_note` 定位关键词，再用 `read_note_range` 读取附近行。两者仅返回局部内容和**整篇**内容版本，不需要把整篇笔记放进模型上下文。`lines`/`matches` 中每项格式为 `行号:12位哈希|正文`；哈希包含该行的换行符。返回 `next_line` 时，使用它作为下一次的 `start_line` 继续读取/查找；`null` 表示本次请求范围内已读完。超出文件尾返回空列表。

例如先读取 `{"path":"notes/demo.md","start_line":20,"max_lines":5}`，从返回行 `20:0123456789ab|旧标题` 复制竖线前的锚点，并携带该次返回的真实版本：

```json
{
  "path": "notes/demo.md",
  "expected_version": "sha256:<读取结果中的完整版本>",
  "request_id": "<本次操作的唯一编号>",
  "actor": "codex",
  "edits": [
    {"op": "SWAP", "anchor": "20:0123456789ab", "lines": ["新标题"]}
  ]
}
```

示例中的哈希和版本是占位值，不能直接用于真实调用。这是 Hashline 风格的结构化接口，不与 Hashline 的补丁字符串或锚点哈希互通；不需要启动另一个 MCP。

| `op` | 行为 | 其他字段 |
| --- | --- | --- |
| `SWAP` | 替换单行或包含两端的行区间 | `lines` 为新行数组；可选 `end_anchor` |
| `DEL` | 删除单行或包含两端的行区间 | 不传 `lines`；可选 `end_anchor` |
| `INS.PRE` | 在锚点行前插入 | 只传单个 `anchor` 与 `lines` |
| `INS.POST` | 在锚点行后插入 | 只传单个 `anchor` 与 `lines` |

`lines` 每项不含 CR/LF，空字符串表示空白行；插入和替换要求至少一行。每次接受 1–100 个操作，全部锚点指向**修改前同一个快照**，不用随着前面的插入重新计算行号。区间重叠、两个操作复用同一锚点行、未知字段、错误哈希均拒绝整次请求，失败前不写入任何一部分。

新行沿用目标位置的 LF/CRLF；未触及的字节和 UTF-8 BOM 保留，替换及尾部插入保留原文件的末尾换行状态。空文件、仅含 BOM 的文件和不存在的文件没有行锚点，使用 `write_note` 新建内容。单行超过 16 KiB 读取上限时明确返回 `LINE_TOO_LONG`，不截断后生成不完整锚点；这类笔记可使用原有 `read_note`/`write_note`。

补丁仍要求整篇版本完全匹配：其他 agent 即使只改了另一行，旧版本也返回 `VERSION_CONFLICT`。版本相符但锚点不符返回 `ANCHOR_CONFLICT`；重新读取受影响片段并整合后，用新编号重试。成功结果只返回版本、提交/待提交状态等信息，不回传整篇正文；重放、批次提交、原子落盘与 Git 恢复沿用已有事务。

## 冲突与重试

```text
A、B 读取同一个 v1
A 携带 v1 写入成功，产生 v2 和 Git 提交
B 携带 v1 写入 → VERSION_CONFLICT，不落盘、不提交
B 重读 v2，合并自己的内容，用新 request_id 重试
```

每次逻辑操作生成一个唯一 `request_id`，建议使用 UUID：

- 网络超时或响应丢失：用**完全相同的 request_id 和参数**重试。已成功的操作返回持久化回执，`replayed: true`，不重复写入或提交。
- 版本冲突：先重读和合并；修改参数后使用新的 request_id。
- 已成功的 request_id 携带不同参数：返回 `IDEMPOTENCY_CONFLICT`。

MCP 工具失败会同时设置 `isError: true` 和 `structuredContent.ok: false`，具体错误位于 `structuredContent.error.code`。客户端必须检查工具结果，不能只看 HTTP 200。

请求编号是整个 Vault 共享的，不按 agent 分区。回执保存在 `.git/vault-write/receipts.sqlite3`，重启后仍然生效；首版不会自动清理回执。

## Git 与故障恢复

同一 Vault 的所有服务进程使用 `.git/vault-write/write.lock` 的操作系统文件锁。锁覆盖恢复、读取、版本检查、写入、Git ref/index 更新和请求回执。不同文件的写入也会串行完成 Git 事务。

服务使用私有临时 Git index 构建提交，不运行 commit hooks，不依赖机器上的 Git 用户配置。持久化事务日志先于正文写入；正文通过同目录临时文件、`fsync` 和原子替换落盘，Git 分支通过比较旧提交的方式更新。

- Git 提交准备失败：笔记保持原样。
- 更新分支前失败且能安全撤销：恢复旧正文，返回错误，同一个请求可以重试。
- 进程在事务中退出：下次启动或操作会依据 `pending.json` 完成该事务并保存回执，再接受新操作。未收到成功响应的请求也可能已完成，必须按相同 request_id 重试。
- 事务期间发现分支、正文或目标暂存内容被外部修改：拒绝自动恢复，保留日志和文件供检查。
- Git 分支已更新，但后续 index 或回执写入仍失败：保留事务并返回 `RECOVERY_REQUIRED`，修复底层错误后重试。

恢复依赖 `.git/vault-write/`；备份时需连同 `.git` 一起保存。不要删除未完成的 `pending.json` 来强行继续。测试覆盖实际进程退出后的恢复；未模拟断电或磁盘硬件故障。

查看或取回历史正文：

```sh
git -C /tmp/vault-write-demo log --oneline -- notes/example.md
git -C /tmp/vault-write-demo show <commit>:notes/example.md
```

要恢复旧内容，取出历史正文，再读取当前版本并通过 `write_note` 写回，形成新的历史记录。

## 当前边界

- 只管理相对 POSIX 路径下、可见目录中的 `.md` 文件；单篇及追加后的结果最多 1 MiB。拒绝目录穿越、隐藏路径、符号链接和嵌套仓库。
- Vault 必须是本地普通 Git 仓库，有命名分支和至少一条提交。`init` 只接受全新或空目录。已有 Vault 应先完整备份，并在其隔离副本上测试。
- 已有暂存修改会拒写；目标笔记有服务之外的未提交改动或尚未跟踪时也会拒写，保留外部编辑。定时模式只接纳本服务已登记的待提交内容。其他文件未暂存的修改和未跟踪文件会保留，不纳入服务提交。
- 不支持 Git 内容过滤器或工作区编码转换、linked worktree、bare 仓库、Windows、NFS 或多机器直接共享文件锁。
- **并发保证只覆盖统一通过本服务的写入。** Shell、编辑器、Obsidian、其他 MCP、Git pull/reset 和同步程序直接改同一副本会绕过锁。正式接管前需统一写入口并协调现有同步任务。
- 自动 push 和双分支同步可按上述配置启用；没有正式库迁移、跨请求的批量原子写接口、删除/重命名写入 API 和服务常驻配置。QMD 继续负责检索。

## 验证

```sh
uv run pytest -q
```

测试使用临时隔离 Git 库：版本校验、局部读取上限、锚点修改与整次拒绝、跨进程并发与冲突重试、不同文件并发、去重回执、Git 失败回滚、实际进程中断恢复、中文路径与字节保留、路径限制，以及真实 HTTP MCP 握手、鉴权和两个独立客户端。

项目附带 Linux/macOS 的 GitHub Actions 工作流；本地通过不代表远端 CI 已运行。
