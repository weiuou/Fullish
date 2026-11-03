# 动态 MCP 服务器

Fullish 可以在运行中添加、检查、停用或删除 MCP server，并把工具授权给指定群。支持远程 Streamable HTTP 和服务器上已安装的 stdio 程序；配置和群授权保存后，后续消息及工具调用读取新状态，不需要重新发布机器人。

内置[CPA 订阅用量 MCP](cpa-usage.md)可以查看当前 AI 后端的账号状态、额度和重置时间。

新 server 的工具通过 `tools/list` 发现，使用工具提供的 JSON Schema 校验参数，再通过 `tools/call` 调用。普通工具无需添加 Python 参数类或修改执行分支。模型看到的名称包含 server 前缀，并处理长名称和重名。

## 在 QQ 私聊中添加

先按[开发者菜单](group-control.md)绑定开发者。群消息和未绑定身份不能管理 MCP，也不会因为模型生成管理命令而修改配置。

例如添加 CoinGecko 官方公开 MCP 地址：

```text
MCP 添加 coingecko https://mcp.api.coingecko.com/mcp
MCP 检查 coingecko
群列表
选择 1
MCP 授权 coingecko
```

添加后默认没有任何授权群。授权群的聊天功能也必须开启；关闭聊天或撤销 MCP 授权会阻止后续调用，包括仍在本地连接队列中等待的请求。已经被外部 server 接受的操作无法撤回。

| 私聊命令 | 作用 |
| --- | --- |
| `MCP` | 显示帮助 |
| `MCP 列表` | 查看名称、传输类型、启用状态和授权群数量；不回显连接地址和凭证 |
| `MCP 检查 名称` | 连接并重新发现工具，返回当前开放的原始工具名 |
| `MCP 授权 名称` / `MCP 撤销 名称` | 授权或撤销当前选中的群 |
| `MCP 限定 名称 lookup,search` | 仅开放列出的工具，使用检查结果中的原始名称 |
| `MCP 限定 名称 *` | 开放该 server 的全部工具，仍受数量限制 |
| `MCP 停用 名称` / `MCP 启用 名称` | 全局停用或恢复该 server，保留群授权 |
| `MCP 删除 名称` | 删除注册和群授权，不删除外部 server 的数据 |

名称使用小写英文字母开头的 1–20 个字母、数字、下划线或连字符。已有名称不能通过添加命令覆盖；修改连接参数使用服务器侧导入。

## 凭证与服务器侧导入

QQ 私聊添加仅接受无查询参数的 HTTPS 地址，或 `env:MCP_变量名`。需要 URL 查询密钥时，由维护者在机器人的运行环境中预先设置完整地址，例如 `MCP_AMAP_URL`，再私聊发送：

```text
MCP 添加 amap env:MCP_AMAP_URL
```

环境引用必须以 `MCP_` 开头，不允许引用 `AI_API_KEY`、`QQ_APP_SECRET` 等现有服务密钥。配置库只保存环境变量名，列表和错误消息不回显其值。新增加的环境变量仍需按现有部署方式注入机器人进程；动态注册不会让运行中的进程自动获取后来修改的环境文件。

需要 HTTP 认证头或本地 stdio 时，在服务器上创建 JSON 配置文件，然后执行导入。例如 HTTP：

```json
{
  "name": "notes",
  "transport": "http",
  "url": "https://notes.example.com/mcp",
  "headers_env": {"Authorization": "MCP_NOTES_AUTHORIZATION"},
  "tools": ["lookup", "save"],
  "groups": []
}
```

`MCP_NOTES_AUTHORIZATION` 的值应包含完整的认证头，例如 `Bearer ...`。当前不提供 OAuth 浏览器授权流程，也不支持旧 HTTP+SSE 端点。

本地 stdio 配置示例：

```json
{
  "name": "notes_local",
  "transport": "stdio",
  "command": "/opt/mcp-notes/venv/bin/python",
  "args": ["-m", "notes_server"],
  "env": {"NOTES_TOKEN": "MCP_NOTES_TOKEN"},
  "groups": []
}
```

`command` 必须是容器内已安装程序的绝对路径；`args` 直接传给程序，不经过 Shell。`env` 将子进程变量名映射到机器人的专用 `MCP_` 环境变量，不继承整个机器人环境。部署镜像没有自动安装 Node、第三方 MCP 包或新挂载；需要这些依赖的 stdio server 先由服务器维护者安装。stdio 程序以机器人的系统身份运行，只应导入维护者信任的程序。

```sh
# 配置文件须位于容器能读取的位置。
docker exec fullish-bot-1 python -m fullish.mcp.store import /tmp/notes.json
docker exec fullish-bot-1 python -m fullish.mcp.store list
docker exec fullish-bot-1 python -m fullish.mcp.store check notes
```

导入会完整替换同名注册，包括 `groups`、`tools` 和启用状态；未填写的群授权默认为空。随后可从私聊菜单选择群并授权。配置保存为 `GROUP_CONTROL_DIR/mcp.sqlite3`，沿用现有持久挂载，文件初始权限 `0600`。

## 连接与结果

每个 server 由独立任务持有 SDK 连接，工具发现和后续调用复用会话。同一个 server 内串行，多个 server 可以同时工作；空闲 60 秒断开，需要时重新连接。连接参数变更后重新发现。外部 server 改变工具列表时，可发送 `MCP 检查 名称` 刷新。连接失败不会阻止其他 server 发现工具，也不会自动重试可能已经执行的写操作；出现结果未知时先查外部系统状态。

支持普通文本、结构化 JSON、嵌入的文本资源及资源链接；资源链接不会被自动访问。图片进入当前消息的图片资产流程，可以供模型查看和发送。音频及其他未接入的内容类型只返回类型与省略标记。工具的 `isError` 和错误正文会返回模型，不要求使用钱包的 `{ok,data}` 包装。

最多注册 8 个 server；单次工具发现最多读取 10 页、256 个定义，每个 server 最多开放 64 个工具，每轮最多提供 96 个动态工具，按工具名排序。建议使用工具限定清单，避免不需要的定义占用上下文。每个工具 schema 最大约 32 KiB，不解析外部 schema 引用；文本和结构化结果合计最大 32 KiB，图片沿用原有图片资产限制。单次连接及调用超时默认 20 秒，可在导入配置中用 `timeout` 设置为 1–30 秒；同 server 的待处理队列最多 32 项。

群授权只控制哪些群可以调用某个 server，不会自动隔离该 server 内部的数据。群知识库或记忆应由后端按可信上下文隔离，或为每个群注册使用独立数据文件的 server。当前通用工具不会自动向第三方传输 QQ 群、成员或消息身份；[钱包](wallet.md)继续使用专用适配器注入可信身份并保证付款幂等，不通过动态配置替换。
