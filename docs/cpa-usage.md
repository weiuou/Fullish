# CPA 订阅用量 MCP

Fullish 自带只读 stdio MCP server，可以查看同机 CLIProxyAPI 中的账号状态、订阅共享额度、已用比例与重置时间。优先使用 `/v8/management`，兼容旧版 `/v0/management`。新 server 通过[动态 MCP 注册](mcp.md)接入，保留按群授权和即时撤销。

## 提供的工具

| 工具 | 内容 |
| --- | --- |
| `cpa_subscriptions` | 提供商、稳定账号代号、可用状态和已知套餐；不返回邮箱、认证文件名或 token |
| `cpa_subscription_quota` | 按提供商或账号查询额度窗口，包含已用/剩余百分比、重置时间和查询时间；默认最多三个账号，可分页 |
| `cpa_request_usage` | 旧版 CPA 提供的进程累计请求和 token 统计；接口不存在时返回不可用 |

CPA v8 为凭证提供 `quota_plugin_id` 时，查询它的标准插件额度接口，无需逐个适配插件名称。Codex、Claude、Gemini CLI 和 Antigravity 还提供固定的上游查询适配。提供商的未知字段、未返回的额度、请求失败或不支持的账号均保持 `unknown` / `null`，不会补成 0% 或 100%。

这些额度属于订阅账号，可能同时被其他 AI 客户端使用，不代表只由 Fullish 消耗。窗口时长只在提供商明确返回时展示；不能从重置倒计时推断五小时、每周或每月配额。CPA 的路由冷却和账号状态与提供商额度分别呈现，查询不会重置冷却或恢复被禁用的账号。

## 同机部署

需要 CPA 的**管理密钥**，不是 `AI_API_KEY`。由维护者在服务器私密文件中保存，例如主机 `/var/lib/fullish-control/cpa-management.key`，文件属主 `10001:10001`、权限 `0600`。现有控制目录挂载后，容器读取 `/var/lib/fullish/control/cpa-management.key`。不要把真实密钥填进下方示例、私聊命令或版本库。

在服务器 `/opt/fullish/runtime.env` 中配置以下非密钥值：

```dotenv
MCP_CPA_BASE_URL=http://localhost
MCP_CPA_UNIX_SOCKET=/run/fullish-cpa/api.sock
MCP_CPA_MANAGEMENT_KEY_FILE=/var/lib/fullish/control/cpa-management.key
```

这复用现有的 CPA Unix socket，CPA 继续监听主机回环地址。变量注入机器人进程后，启动时会首次自动注册名为 `cpa_usage` 的内置 server，不需要额外执行服务器命令。已有同名配置和群授权不会被覆盖；从菜单删除后，后续重启也不会自动加回来。

如需手动注册或恢复已删除的注册，可执行：

```sh
docker exec fullish-bot-1 python -m fullish.integrations.cpa.server --register
```

目录迁移前已有的内置 stdio 注册在读取时使用当前 `fullish.integrations.cpa.server` 入口，数据库中的旧入口、启停状态、群授权和工具范围均不自动改写。自定义 HTTP server 不受影响；旧镜像回滚仍可读取原注册。新建注册使用新入口，若回滚到旧镜像，需按旧版文档恢复对应入口参数。

自动注册和手动注册都不授予任何群，手动命令不覆盖已有同名配置。随后在已绑定的开发者私聊中执行：

```text
MCP 检查 cpa_usage
群列表
选择 1
MCP 授权 cpa_usage
```

之后群里可以问“看看 AI 订阅还有多少额度”“Codex 的周额度什么时候重置”。模型使用工具名称 `mcp_cpa_usage__cpa_subscription_quota`；输出只包含查询结果，不提供管理入口。

也可把内置模块交给其他受信任的 MCP host，以当前 Python 执行 `-m fullish.integrations.cpa.server`，并向子进程传入 `CPA_BASE_URL`、`CPA_UNIX_SOCKET`、`CPA_MANAGEMENT_KEY_FILE`；独立远程 CPA 使用 HTTPS，可省略 Unix socket。支持用 `CPA_MANAGEMENT_KEY` 从私密环境提供管理密钥，但部署优先使用文件。

## 查询边界

工具参数不接受 URL、HTTP 方法、认证文件或管理密钥。只调用账号列表、已知额度接口和旧版累计统计接口；不导出认证文件，不读取或修改全局配置，不删除/停用账号，不重置额度，也不消费 `usage-queue`。上游只执行固定的额度查询；部分提供商用 POST 表达只读查询。

管理密钥只在适配器内用于 CPA 管理认证，上游 token 由 CPA 根据真实 `auth_index` 注入，不返回给 Fullish 模型。stdio 适配器与 bot 使用相同系统身份，私密文件也由该身份读取；这不是独立系统身份的隔离边界。群成员只能使用管理者授予的三个只读工具。

额度结果在 MCP 子进程中缓存 60 秒，响应标注 `cached` 和 `checked_at`；账号状态变更会使缓存失效。单次上游请求最多 10 秒，最多同时查询三个账号；每个账号最多显示 16 个窗口，并标注总数和是否截断。管理认证失败后停止该连接内的重复尝试，避免触发 CPA 的失败封禁。

CPA 新版已移除旧版累计统计接口时，`cpa_request_usage` 会说明不可用；不会把消费式用量队列当作只读报表。提供商额度接口随上游可能变化，失败时以未知状态显示。

接口依据：[CPA v8 管理接口](https://github.com/router-for-me/CLIProxyAPIDocs/blob/main/docs/en/management/apiv8.md)、[CPA 插件配额格式](https://github.com/router-for-me/CLIProxyAPI/blob/main/sdk/pluginapi/types.go)、[官方管理面板的提供商查询](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/tree/main/src/features/quota/providers)。
