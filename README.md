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

### 可选写入路径保护

默认 `[write_policy] allow_paths=[] / deny_paths=[]` 不增加限制。显式配置后同时保护 `write_note`、`append_note`、`patch_note`、移动预览与执行（源、目标和所有改写引用笔记），以及已有 interval 批次的 flush。例：

```toml
[write_policy]
allow_paths = ["notes", "inbox.md"]
deny_paths = ["notes/private"]
```

规则为相对可见 POSIX 路径，匹配自身或以 `/` 分隔的后代，不是 glob；`notes` 不匹配 `notes-other`。可有一个尾部 `/`，拒绝空段、点段、反斜杠、控制符和 glob 字符；每组最多 256 条、每条最多 1,024 字符。比较使用 Unicode NFC 与 casefold（保守覆盖 macOS 大小写/Unicode 文件系统别名），但不改实际文件名。空 allow 表示全部，deny 始终优先。拒绝返回 `PATH_PROTECTED`，完整受影响集合预检后才可修改正文、ref、index、回执或日志；读取不受限。`actor` 不是认证身份，不能绕过规则。

**重试/恢复：** 当前策略也检查已完成回执的重放；新策略拒绝时保留原回执，不重新执行或生成结果，恢复允许策略后可原样重放。未完成事务的全部受影响路径在初始化创建 schema/锁和恢复清理之前检查；策略变严则整次阻塞，保留现场，先运行只读诊断并由操作者恢复允许策略后再启动恢复。已有 interval 待提交笔记变为受保护也会阻塞 flush。不要删除日志/数据库绕过策略。规则只约束上述服务写入入口，不是操作系统权限或外部 Git/文件编辑限制，也**不阻断显式启用的双分支同步 checkout**；如需保护文件不被远端同步修改，应停用 sync 或在服务之外配置访问边界。

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


成功推送的检查点保存在 `.git/vault-write/push.json`，重启后沿用。检查点包含所有实际 push URL（含 `pushurl` 与操作员 Git URL 别名展开）的排序去重 SHA-256 摘要，不保存 URL/凭据；同名 remote 改址或目标分支改变会重新普通推送。不同 SSH/HTTPS 拼写或凭据不猜测等价，宁可多检查一次；仅 fetch URL 改变而独立 push URL 不变时不会重推。旧格式/损坏检查点会安全重检。含控制符或换行分隔歧义的 URL 会拒绝推送，不猜测目标。Git 推送错误只返回通用说明，不回显远端输出。推送失败不会撤销本地提交，也不会重新调用模型生成信息；后台在下个周期重试。API Key 不参与 Git 认证。

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
| `find_note` | `path`, `text`, `start_line=1`, `max_matches=10`, `context_before=0`, `context_after=0`, `compact=false` | 在一篇笔记内做区分大小写的字面查找，返回命中行和整篇版本；可选返回合并的局部上下文窗口 |
| `write_note` | `path`, `content`, `expected_version`, `request_id`, `actor` | 创建或整篇替换；版本必须匹配 |
| `append_note` | 同上 | 在版本检查和同一把锁内读取、拼接、写回；原样追加，不自动补换行 |
| `patch_note` | `path`, `edits`, `expected_version`, `request_id`, `actor`, `return_context=false`, `context_before=0`, `context_after=0` | 根据行锚点原子替换、删除、插入；可选返回带新锚点的编辑后局部上下文 |
| `preview_move_note` | `source`, `destination` | 只读预览移动方案、受影响路径、有限的逐链接目标摘要与版本前置条件 |
| `move_note` | `source`, `destination`, `expected_versions`, `plan_version`, `request_id`, `actor` | 校验预览后以一个可恢复的多文件事务移动笔记并更新支持的引用；完成后创建一个 Git 提交 |
| `note_history` | `path`, `limit=20`, `follow_renames=false` | 返回修改该笔记的 Git 提交，最多 100 条；可选 Git 重命名启发式 |
| `sync_status` | 无 | 只读同步状态、当前写入提交与待提交数，不触发同步 |

现有笔记的版本形如 `sha256:<64 位小写十六进制>`，按原始 UTF-8 字节计算。创建新笔记也必须显式提交 `expected_version: "missing"`；它只能匹配真正不存在的文件。服务不提供 `blind`、强制覆盖或关闭版本校验的参数。BOM、CRLF 和末尾换行会原样保留。

逐次模式成功写入返回 `version`、`commit`、`changed`、`request_id` 和 `replayed`。正文没有变化时 `changed: false`，不会额外创建空提交。该模式的提交只包含该笔记，正文记录 `VaultWrite-Actor` 和 `VaultWrite-Request`；定时模式的批次提交记录涉及的 actor 和请求编号。

`actor` 是调用方提供的审计标签，不是经过独立认证的用户身份。默认使用共享 bearer token；私网可按上述配置关闭认证。

### 局部读取与锚点修改

优先用 `find_note` 定位关键词，按需读取附近行。两者仅返回局部内容和**整篇**内容版本，不需要把整篇笔记放进模型上下文。`lines`/`matches` 中每项格式为 `行号:12位哈希|正文`；哈希包含该行的换行符。返回 `next_line` 时，使用它作为下一次的 `start_line` 继续读取/查找；`null` 表示本次请求范围内已读完。超出文件尾返回空列表。

`find_note` 的 `context_before` 和 `context_after` 可选指定每个命中行前后要带回的行数，默认为 `0`，范围为严格整数 `0–50`。非零时除原有 `matches` 外，还返回 `windows`：每个窗口含 `start_line`、`end_line` 和按行号顺序排列的 `lines`；重叠或相邻的窗口会合并，并增加 `context_truncated` 布尔值。匹配项的分页仍只按 `matches` 计算，上下文不会消耗或跳过后续命中。上下文最多返回 200 行；`matches` 与窗口中的渲染行条目（含重复条目）合计受 16 KiB 上限约束。上下文尽力返回；超大上下文行可能被略过并将 `context_truncated` 设为 `true`，而过长的匹配行仍按原有规则返回 `LINE_TOO_LONG`。默认值为零时，响应仍保持原有形状。

例如，下面的查找会带回每个命中行前 1 行、后 3 行的上下文窗口，仍使用原来的 `matches` 锚点及整篇版本：

```json
{"path":"notes/demo.md","text":"旧标题","context_before":1,"context_after":3}
```

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

若要立即继续编辑邻近行，可在第一次补丁中请求编辑后上下文；下面锚点和版本均为占位值。第一次补丁使用刚才读取/查找结果中的锚点与版本；后续补丁再使用本次 `result_context` 中的新锚点和本次返回的 `version`：

```json
{
  "path": "notes/demo.md",
  "edits": [{"op": "SWAP", "anchor": "20:0123456789ab", "lines": ["修订后的标题"]}],
  "expected_version": "sha256:<读取结果中的完整版本>",
  "request_id": "<本次操作的唯一编号>",
  "actor": "codex",
  "return_context": true,
  "context_before": 1,
  "context_after": 3
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

补丁仍要求整篇版本完全匹配：其他 agent 即使只改了另一行，旧版本也返回 `VERSION_CONFLICT`。版本相符但锚点不符返回 `ANCHOR_CONFLICT`；重新读取受影响片段并整合后，用新编号重试。

需要继续编辑时，可在 `patch_note` 传入 `return_context: true` 和 `context_before`/`context_after`（各为严格整数 `0–50`，默认均为 `0`）；只传非零上下文数值而未启用 `return_context` 会被拒绝。成功响应会增加 `result_context`，包含 `total_lines`、编辑后内容的局部窗口、新锚点和 `truncated` 布尔值；即使前后上下文均为零，也会返回本次编辑后跨度的新锚点（删除整篇笔记时为空窗口）。窗口行号是补丁后的行号。窗口合并、200 行及 16 KiB 限制沿用 `find_note` 的上下文规则。可从新窗口中复制后续目标的锚点，并携带本次返回的 `version` 发起下一次补丁。默认不请求上下文时，响应仍保持原有形状。

带上下文的补丁结果与写入回执一起持久保存。若传输失败，使用完全相同的 `request_id` 和参数重放，服务会返回首次操作时保存的结果及其 `result_context`，不会基于之后的笔记版本重新生成上下文；重放标记为 `replayed: true`。上下文是辅助定位信息，不会放宽整篇版本校验：下一次写入仍须提交当前返回的完整版本，且任何期间的其他写入都会按原样触发版本冲突。

可在本地隔离临时库中复现调用数和返回数据量的对比：

```sh
uv run python examples/editing_benchmark.py
```

脚本对相同的两个连续编辑任务比较默认 `find_note`→`read_note_range`→`patch_note` 流程与上下文查找、编辑后锚点复用流程，并验证两者得到相同正文。它统计直接调用 Store 方法的次数及结果字典的紧凑 UTF-8 JSON 字节数，不含建库、验证、MCP/HTTP 封装、模型 token 或费用估算；所有数据只写入自动清理的临时库。

#### 可选紧凑查找上下文

`find_note(..., compact=true)` 将每条原始渲染行（`行号:哈希|原文`）只放在 `windows` 中一次，`matches` 改为 `{"line": 3, "window_index": 0, "line_index": 2}` 引用；两个 index 都从 **0** 开始，`line` 为 **1** 起始行号。定位示例：

```python
window = result["windows"][match["window_index"]]
rendered_line = window["lines"][match["line_index"]]
assert window["start_line"] + match["line_index"] == match["line"]
anchor, text = rendered_line.split("|", 1)
```

默认 `compact=false` 的响应形状和内容不变。紧凑模式即使 before/after 都为 0 也返回只包含命中行的窗口；无命中则空窗口。每页匹配、`next_line`、整篇版本及锚点与默认模式一致，不跳过命中、不做有损摘要。先保证本页命中行，再加入可容纳的上下文，合并相邻/重叠窗口；原样保留可见原文和换行敏感锚点，仍限制 200 行与 16 KiB 渲染行预算（JSON 元数据/传输封装不计入这个既有上限）。超预算邻行可省略并设 `context_truncated=true`；单条命中超过上限仍为 `LINE_TOO_LONG`。

`uv run python examples/compact_find_benchmark.py` 自建固定合成临时 Vault，通过 in-process ASGI HTTP transport 计量序列化结果 JSON 和 MCP 实际响应 body 字节；不含 HTTP 头/分块/TCP/TLS 开销，也不是模型 token。该样本默认/紧凑 JSON 为 **4,849 / 2,647 字节**，MCP response body 为 **9,871 / 5,485 字节**；小行或无上下文时元数据可使紧凑模式更大，不保证所有输入都节省。

### 安全移动笔记并更新引用

移动不是无条件的 Git rename。先调用只读 `preview_move_note(source, destination)`，检查返回计划，再把同一次预览的 `plan_version` 和完整 `expected_versions` 原样交给 `move_note`。预览返回 `affected_paths`、实际改变的链接目标总数 `rewritten_links`、扫描的 Markdown 笔记数 `scan_count`、移动后的 `versions_after`，以及有限的 `link_changes` 摘要。每条摘要包含原路径 `path`、移动后的笔记路径 `result_path`、旧行号 `line` 和实际替换跨度的 `old_target`/`new_target`，不返回整篇正文。Wiki 摘要只包含路径跨度，`#fragment` 和别名留在跨度之外；Markdown 摘要按实际目的地跨度记录，可能含保留的片段。摘要最多 50 项及 16 KiB；`link_changes_truncated` 标明摘要是否截断，但 `rewritten_links` 始终是精确总数。版本映射包含源笔记、必须仍不存在的目标路径和确实需要改写的引用文件；移动后源路径版本为 `missing`，目标及被改写笔记返回新版本。

预览摘要示意（真实响应还含计划版本、版本映射和路径列表）：

```json
{
  "rewritten_links": 1,
  "link_changes": [{
    "path": "home/index.md",
    "result_path": "home/index.md",
    "line": 18,
    "old_target": "../guide/old.md#install",
    "new_target": "../archive/guide.md#install"
  }],
  "link_changes_truncated": false
}
```

预览只读取当前 Vault 的本地已提交目录树，不会 fetch 远端分支或联网。使用双分支同步时，先按既有流程同步到所需快照，再预览和移动；预览之后才从远端合入的新引用不属于该计划，需同步后重新检查。系统不保证覆盖尚未同步到本地快照的远端 backlink。

示例调用：

```json
{"source":"guide/v1/old-title.md","destination":"archive/reference/new-title.md"}
```

从结果复制 `plan_version` 和完整 `expected_versions`：

```json
{
  "source":"guide/v1/old-title.md",
  "destination":"archive/reference/new-title.md",
  "expected_versions":{
    "guide/v1/old-title.md":"sha256:<预览中的源版本>",
    "archive/reference/new-title.md":"missing",
    "home/index.md":"sha256:<预览中的引用笔记版本>"
  },
  "plan_version":"sha256:<预览中的计划版本>",
  "request_id":"<本次移动的唯一编号>",
  "actor":"codex"
}
```

以上字段值只是示例；实际调用必须复制预览中的整个 `expected_versions` 对象，不能省略受影响的文件，也不能用示例哈希。应用时服务会重新计算计划并核对整篇版本：内容版本变化返回 `VERSION_CONFLICT`，Vault 拓扑或计划快照变化返回 `PLAN_CONFLICT`，应重新预览。源/目标须为可见的相对 POSIX `.md` 路径；名称不能包含用于解析引用的 `# | ^ : % ? [ ] ( ) < >` 或反引号。目标必须不存在，源必须是已提交笔记；Git 工作区与暂存区须干净，且不得有未跟踪或被忽略的可见 `.md` 文件，也不能有待提交的 interval 批次。若有待提交批次，先通过 `flush` 完成，移动不会隐式 flush。

当前支持保守的本地引用子集：wiki 链接 `[[path]]`、带片段/别名的 `[[path#heading|label]]` 及嵌入 `![[path]]`；Markdown 行内链接和图片，如 `[label](relative/path.md#heading)`、`![alt](relative/image.png#fragment)`。Wiki 名称可按精确路径、引用笔记相对路径、以 `/` 开头的 Vault 根相对路径或唯一 basename/suffix 解析；这些路径形式均可尝试补 `.md` 后缀。Markdown 路径相对其所在笔记；所有相对写法（包括 `./`、`../` 和含点的 basename）在未解析时也会尝试 `.md`。若 `.md` 笔记与无扩展名附件同时匹配，预览以 `AMBIGUOUS_LINK` 拒绝，不猜测。Markdown 目的地可使用 UTF-8 百分号编码；包含 HTML entity 的本地目标不支持。移动后会更新指向源笔记的引用，并重定位源笔记内部的相对链接，以尽量保持其原目标；片段、别名、链接标签和图片标签保留。

外部 scheme 链接（如 `https:`）、协议相对 URL（`//...`）、纯 `#anchor` 及正常的围栏代码、缩进代码和行内代码不改写。引用式 Markdown（`[label][id]` / `[id]: ...`）、本地 HTML `href`/`src`、front matter 本地文件引用（包括引号或列表形式）、嵌套/跨行/不支持的链接形式（包括 wiki 查询/转义语法）、含 HTML entity 的目标、畸形 URL 编码和逃出 Vault 根目录的路径不做猜测。行内/围栏代码按常规范围识别；反引号转义不会把后续活动链接隐藏成代码。存在 blockquote 起始、list-fence、含链接的 list/段落续行、raw HTML/code block 边界、Obsidian `%%` 注释或 math 容器时，预览保守返回 `UNSUPPORTED_LINK`，不对结构含糊的区域做替换。只支持 LF/CRLF 的 UTF-8 笔记（含可选 BOM）；仅使用 CR 的文件也拒绝处理。若扫描到无法安全处理的潜在引用，预览返回 `UNSUPPORTED_LINK`。同名候选不唯一、含糊的裸 wiki 目标，或新目标会截获原本不相关的链接时，返回 `AMBIGUOUS_LINK`。预览失败不会应用部分改写。

扫描有硬上限：最多 2,000 个 Git 跟踪路径、1,000 篇可见 Markdown 笔记、16 MiB Markdown 正文和 100 个受影响路径；每篇最多 2,000 个可解析引用、全库最多 20,000 个，超限返回 `MOVE_TOO_LARGE`。摘要最多 50 项、16 KiB；超过摘要上限不丢失总数，但设置 `link_changes_truncated`。超过 4,096 字符的复杂括号结构也拒绝处理。预览只读。成功移动在一个可恢复事务中写入目标和引用文件，最后移除源，并创建一个包含这些变化的 Git 提交；服务重启/后续操作会恢复未完成事务，然后保存请求回执。相同 request id 与相同参数重试会重放原结果，不重复移动；修改参数需使用新 id。该保证适用于通过服务访问和恢复，普通文件系统读者仍可能在多文件应用期间观察到部分替换。

可运行一个只使用自动清理临时 Vault 的完整示例：

```sh
uv run python examples/move_reference_demo.py
```

它演示 wiki 和 Markdown 入链更新、移动笔记中的出链相对路径重定位，以及相同请求的幂等重放；不会读取或修改现有 Vault。

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

服务使用私有临时 Git index 构建提交，不运行 commit hooks，不依赖机器上的 Git 用户配置。持久化事务日志先于正文写入；正文通过与 Vault 同一文件系统上的私有暂存文件、`fsync` 和原子替换落盘，Git 分支通过比较旧提交的方式更新。若笔记目录与 `.git/vault-write` 不在同一文件系统，服务返回 `UNSUPPORTED_FILESYSTEM`，不会退化为非原子复制。

- Git 提交准备失败：笔记保持原样。
- 更新分支前失败且能安全撤销：恢复旧正文，返回错误，同一个请求可以重试。
- 进程在事务中退出：下次启动或操作会依据 `pending.json` 完成该事务并保存回执，再接受新操作。未收到成功响应的请求也可能已完成，必须按相同 request_id 重试。
- 正文原子替换使用 `.git/vault-write/atomic-*.tmp` 私有暂存文件。重启后的启动/操作恢复只清理注册表中名称、设备号与 inode 都匹配的普通单链接文件；未注册的临时文件、已被替换的路径、符号链接或硬链接不会被猜测性删除，需人工检查。旧版可见目录中的 `.vault-write-*.tmp` 也不会自动删除，因为服务无法证明所有权；未知私有临时文件不会进入 Git 提交或同步。该清理不删除 `pending.json` 或请求回执；不要删除未完成的事务日志来强行继续。
- 事务期间发现分支、正文或目标暂存内容被外部修改：拒绝自动恢复，保留日志和文件供检查。
- Git 分支已更新，但后续 index 或回执写入仍失败：保留事务并返回 `RECOVERY_REQUIRED`，修复底层错误后重试。

恢复依赖 `.git/vault-write/`；备份时需连同 `.git` 一起保存。不要删除未完成的 `pending.json` 来强行继续。测试覆盖实际进程退出后的恢复；未模拟断电或磁盘硬件故障。

查看或取回历史正文：

```sh
git -C /tmp/vault-write-demo log --oneline -- notes/example.md
git -C /tmp/vault-write-demo show <commit>:notes/example.md
```

要恢复旧内容，取出历史正文，再读取当前版本并通过 `write_note` 写回，形成新的历史记录。

### 只读恢复诊断与可选历史关联

```sh
uv run vault-write-mcp doctor --vault /path/to/vault --config config.toml
```

`doctor` 不需要 MCP 令牌，不构造 `VaultStore`，不触发初始化/恢复、清理原子临时文件、创建目录/锁、写 SQLite 或刷新 Git index；不联网。输出为有界 JSON：正规 Git 状态、有效 HEAD、日志种类/受影响数量、HEAD/ref 是否符合日志、当前策略是否阻塞，以及人工下一步。仅输出固定标签、数量和已校验对象 ID，不回显日志任意字段、正文、凭据、URL、路径或 Git 错误；因此**不定位具体冲突文件**，也不证明日志/正文/index 可安全恢复。损坏、过大、symlink/FIFO 状态会给出不可安全检查的标签，不自动删除或修改任何现场。

这是无需锁的实时尽力快照；并发写入可能让观察不一致，必要时先停写再诊断。Git plumbing 不刷新 stat 缓存，`tracked_changes=changed_or_stat_stale` 也可能只是缓存过旧，且不检查未跟踪文件。SQLite 使用只读 immutable 主数据库快照，不创建 WAL/SHM；存在 WAL、SHM 或 rollback journal 任一 SQLite 旁文件时 `observation_complete=false`，主数据库快照可能不完整/包含未提交页，计数不能代表最终事务状态；旁文件无法安全检查时标记 `null`（未知）且观察不完整，不得视为无待处理事务。诊断有输入上限（日志 64 MiB、数据库 128 MiB、路径 2,000、计数 100,000 后标记封顶），不会保证读取访问时间 atime 不变；这些是输入/输出界限，不是严格进程内存/RSS 上限，JSON 解析仍可能有内存放大，不承诺抵御恶意资源耗尽。`no_journal` 仅表示本次未观察到日志，不等于健康、已提交或已发布。

先停止服务和外部写入者、完整备份 Vault 与 `.git`，保留 `pending.json`/回执/未知临时文件。若 `policy_blocked`，由操作者恢复允许原事务的策略；若有外部冲突，人工检查并保留现场，不自动选 ours/theirs/reset。恢复仍由正常初始化执行既有完整校验；准备好后用原配置重启，并以完全相同参数和 request_id 重试。

`note_history(..., follow_renames=true)` 可使用 Git `--follow` 追踪当前单一路径以前的名称，结果额外标记 `rename_following=git_similarity_heuristic`。默认响应和行为不变。相似文件、复制、重写或复杂合并可能漏关联或误关联；这不是笔记身份或来源的证明，不用于授权、版本校验或恢复决策。演示：`uv run python examples/doctor_demo.py`（自建临时 Vault）。

## 当前边界

- 只管理相对 POSIX 路径下、可见目录中的 `.md` 文件；单篇及追加后的结果最多 1 MiB。拒绝目录穿越、隐藏路径、符号链接和嵌套仓库。
- Vault 必须是本地普通 Git 仓库，有命名分支和至少一条提交。`init` 只接受全新或空目录。已有 Vault 应先完整备份，并在其隔离副本上测试。
- 已有暂存修改会拒写；目标笔记有服务之外的未提交改动或尚未跟踪时也会拒写，保留外部编辑。定时模式只接纳本服务已登记的待提交内容。其他文件未暂存的修改和未跟踪文件会保留，不纳入服务提交。
- 不支持 Git 内容过滤器或工作区编码转换、linked worktree、bare 仓库、Windows、NFS 或多机器直接共享文件锁。
- **并发保证只覆盖统一通过本服务的写入。** Shell、编辑器、Obsidian、其他 MCP、Git pull/reset 和同步程序直接改同一副本会绕过锁。正式接管前需统一写入口并协调现有同步任务。
- 自动 push 和双分支同步可按上述配置启用；没有正式库迁移、跨请求的批量原子写接口、单独的删除写入 API 和服务常驻配置。移动/重命名仅通过上述预览后、校验版本并安全更新引用的流程完成。

## 验证

```sh
uv run pytest -q
```

测试使用临时隔离 Git 库：版本校验、局部读取上限、锚点修改与整次拒绝、跨进程并发与冲突重试、不同文件并发、去重回执、Git 失败回滚、实际进程中断恢复、中文路径与字节保留、路径限制，以及真实 HTTP MCP 握手、鉴权和两个独立客户端。

项目附带 Linux/macOS 的 GitHub Actions 工作流；本地通过不代表远端 CI 已运行。
