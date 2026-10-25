# VaultWrite MCP

独立运行的 Markdown 写入服务。多个 agent 连接同一个 HTTP MCP，强制检查内容版本，每次有效修改生成一条本地 Git 提交。无需运行 Obsidian。

这是可测试的 0.1 原型。支持 Linux/macOS、Python 3.11+ 和 Git，使用[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)的 Streamable HTTP。

## 快速测试

在本项目目录安装依赖，然后创建一个全新的隔离 Vault：

```sh
uv sync --locked --extra test
uv run vault-write-mcp init --vault /tmp/vault-write-demo
export VAULT_WRITE_TOKEN="$(openssl rand -hex 32)"
uv run vault-write-mcp serve --vault /tmp/vault-write-demo
```

默认端点是 `http://127.0.0.1:18182/mcp`。客户端必须携带 `Authorization: Bearer <VAULT_WRITE_TOKEN>`。服务启动时就会检查和恢复未完成事务。

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

## 工具

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `read_note` | `path` | 返回正文、`exists` 和当前 `version`；不存在时版本为 `missing` |
| `write_note` | `path`, `content`, `expected_version`, `request_id`, `actor` | 创建或整篇替换；版本必须匹配 |
| `append_note` | 同上 | 在版本检查和同一把锁内读取、拼接、写回；原样追加，不自动补换行 |
| `note_history` | `path`, `limit=20` | 返回修改该笔记的 Git 提交，最多 100 条 |

现有笔记的版本形如 `sha256:<64 位小写十六进制>`，按原始 UTF-8 字节计算。创建新笔记也必须显式提交 `expected_version: "missing"`；它只能匹配真正不存在的文件。服务不提供 `blind`、强制覆盖或关闭版本校验的参数。BOM、CRLF 和末尾换行会原样保留。

成功写入返回 `version`、`commit`、`changed`、`request_id` 和 `replayed`。正文没有变化时 `changed: false`，不会额外创建空提交。Git 提交只包含该笔记，提交正文记录 `VaultWrite-Actor` 和 `VaultWrite-Request`。

`actor` 是调用方提供的审计标签，不是经过独立认证的用户身份。首版使用共享 bearer token。

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
- 已有暂存修改会拒写；目标笔记与 HEAD 不一致或尚未跟踪时也会拒写，保留外部编辑。其他文件未暂存的修改和未跟踪文件会保留，不纳入服务提交。
- 不支持 Git 内容过滤器或工作区编码转换、linked worktree、bare 仓库、Windows、NFS 或多机器直接共享文件锁。
- **并发保证只覆盖统一通过本服务的写入。** Shell、编辑器、Obsidian、其他 MCP、Git pull/reset 和同步程序直接改同一副本会绕过锁。正式接管前需统一写入口并协调现有同步任务。
- 首版只生成本地 Git 历史；没有自动 push/pull、正式库迁移、批量事务、删除、重命名和服务常驻配置。QMD 继续负责检索，写入前须通过本服务读取最新正文。

## 验证

```sh
uv run pytest -q
```

测试使用临时隔离 Git 库：版本校验、跨进程并发与冲突重试、不同文件并发、去重回执、Git 失败回滚、实际进程中断恢复、中文路径与字节保留、路径限制，以及真实 HTTP MCP 握手、鉴权和两个独立客户端。

项目附带 Linux/macOS 的 GitHub Actions 工作流；本地通过不代表远端 CI 已运行。
