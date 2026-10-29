# Fullish 专属 BSC 钱包与 MCP

Fullish 拥有独立的普通私钥钱包，默认使用 BSC 主网（chain ID 56）。可查询 BNB 和指定 BEP-20 代币余额、自主发送 BNB 或代币，并查询自己签名交易的状态；无需逐笔人工确认。自主决定发生在现有聊天工具轮次内，不新增无人触发的后台资金任务。

## 调用方式

```text
QQ 真实事件 → 群权限与参数校验 → MCP client → stdio MCP wallet server → BSC RPC
                                                   ↓
                                         加密密钥与持久交易记录
```

采用官方 Python MCP SDK。Fullish 使用 `tools/list` 发现工具定义，转换为当前聊天 API 的 `tool_calls`，通过 `tools/call` 执行。MCP 与模型调用各承担一层协议职责。当前支持运营者控制的本地 stdio 钱包服务；尚未提供任意远程 MCP URL、OAuth 或第三方服务器配置入口。既有搜索和沙箱继续使用原来的调用方式。

签名仅在钱包子进程中进行。stdio 不监听网络端口；启动钱包进程时只额外传入钱包配置，不传递 CPA、QQ 或搜索密钥。子进程与 bot 使用相同系统用户，属于进程分离，不能防御 bot 进程或宿主机被完全控制；强隔离需要另行部署为不同身份或远程签名服务。

## 创建与保管

先安装锁定依赖，再手工创建一次：

```sh
uv sync --locked
uv run --locked python -m fullish.wallet create --directory .state/wallet
uv run --locked python -m fullish.wallet address --directory .state/wallet
```

创建命令只输出公开地址和网络，生成以下私有文件：

| 文件 | 用途 |
| --- | --- |
| `keystore.json` | 标准加密 keystore，包含加密后的私钥 |
| `unlock.secret` | 随机解锁密码，用于无人值守签名 |
| `wallet.json` | 公开地址与网络信息 |
| `transfers.sqlite3` | 服务首次启动时创建；记录签名交易、平台事件和广播状态 |

Unix 目录权限为 `0700`，文件为 `0600`；Windows 创建目录时限制 NTFS ACL 为当前用户和 SYSTEM。密码与 keystore 同在受保护目录，能够读取整个目录的人具有钱包控制权。整个目录应做私有备份，尤其在付款后需要保留交易数据库。`.state/` 已被 Git 忽略，Docker 构建只接收白名单源码，钱包不进入仓库或镜像。

创建命令拒绝覆盖非空目录。启动服务只解锁现有钱包，缺失、损坏或密码错误会失败，不自动生成新地址。部署时必须迁移原来的钱包目录，避免为同一个 Fullish 创建第二个钱包。

## 启用与部署

配置现有机器人运行环境：

```dotenv
WALLET_ENABLED=true
WALLET_ALLOWED_GROUPS=*
WALLET_DIR=.state/wallet
WALLET_RPC_URL=https://bsc-dataseed.bnbchain.org
```

钱包需要 AI 后端；QQ群必须同时允许聊天。`WALLET_ALLOWED_GROUPS=*` 向所有已允许聊天的群开放钱包；也可以填写逗号分隔的群 OpenID，仅开放指定群。钱包群清单独立于搜索和沙箱权限，不接受模型参数中的群、发送人或消息身份。这些群的成员都可能通过对话影响 Fullish 的支付决定；这是群范围的授权，不是只允许开发者转账。当前没有单笔、每日金额上限或收款人白名单，也没有任意合约调用、代币授权、私钥导出工具。

生产部署先用新版本 `scripts/server-setup.sh` 更新服务器 Compose 和目录。将已经创建的**原钱包目录内容**经私有渠道迁移到 `/var/lib/fullish-wallet`，文件属主为 `10001:10001`，目录 `0700`、文件 `0600`。不要在 GitHub Secrets、PR、聊天或公开日志中传递私钥和解锁文件。设置服务器 `/opt/fullish/runtime.env` 的启用开关和授权群，再按项目既有流程发布镜像；只更新应用镜像不能安装新挂载。

Compose 把该宿主机目录挂载为 `/var/lib/fullish/wallet` 并固定 `WALLET_DIR`。更新、重建容器和回滚镜像均保留钱包与交易记录；沙箱不挂载该目录。`/health/dependencies` 仅显示是否启用和公开地址，不联网探测余额，也不证明钱包已完成真实链上转账。

部署脚本在替换旧服务前，使用候选镜像和实际 Compose 挂载运行 `python -m fullish.wallet_check`，检查密钥解锁、MCP 工具发现、BSC chain ID 与余额读取；启动新服务后再检查一次。该检查只读链上数据，不发送资产；失败会阻止发布或触发原有回滚。钱包子进程串行运行，以控制小服务器上的解锁内存开销。

显式 `--rollback` 允许退回尚未支持钱包的旧镜像，因此跳过钱包验收；原钱包文件和配置继续保留，但旧版本不能使用钱包。

其他受信任 MCP host 可用以下 stdio 启动方式连接：

```json
{
  "mcpServers": {
    "fullish-wallet": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "fullish.wallet_mcp"],
      "env": {
        "WALLET_ENABLED": "true",
        "WALLET_DIR": "/absolute/path/to/private/wallet",
        "WALLET_ALLOWED_GROUPS": "configured-group-openid"
      }
    }
  }
}
```

钱包工具为 `wallet_info`、`wallet_transfer`、`wallet_transaction`。转账的 host 必须在 MCP `_meta` 中提供 `fullish/group_id`（配置中的群）和 `fullish/event_key`（64 位十六进制幂等标识）。Fullish 从真实 QQ 群、成员与消息 ID 计算该标识，不把这些字段暴露给模型。其他 host 的接入者需要实现这一小段上下文适配；MCP 协议本身不验证 QQ 身份，stdio host 是受信任方。

## 付款语义与故障恢复

每条真实 QQ 消息最多一笔付款。转账数量采用十进制字符串和整数运算，BEP-20 精度从合约读取；签名前检查 chain ID、代币余额、BNB 手续费余额、预估 gas，并模拟代币 `transfer`。转账调用只构建原生币转账或固定的 BEP-20 `transfer(address,uint256)`，不接受模型传入任意 calldata、链 ID、nonce 或 RPC 地址。

签名后的原始交易与哈希先提交到 SQLite，再广播；相同事件、相同参数重试只重发相同字节。不同参数被拒绝；存在未入块交易时暂停创建下一笔付款。数据库绑定钱包地址，恢复时不能混用其他钱包的交易记录。

- `submitted`：节点接受广播，尚未证明到账。
- `broadcast_unknown`：广播超时或响应不确定；保存的交易可能已被节点接受。查询原哈希，不能换一个消息再次付款。
- `pending`：暂未找到收据，不代表失败。
- `confirmed`：收据显示已入块且执行成功；仍可能受链重组影响，不等同于不可逆最终性。
- `reverted`：已入块但执行失败，可能消耗手续费。

进程取消或连接中断后，先使用 `wallet_info` 找到最近的交易哈希，再用 `wallet_transaction` 回查。相同事件重试可以广播原交易；当前不实现加价替换、取消卡住的交易或自动更换 RPC。手续费、特殊转账税、恶意代币及代币合约自身行为不能仅靠普通转账模拟完全保证，收据成功也不保证收款人实际收到与输入完全相同的代币数量。

测试使用真实本地签名、MCP 协议和模拟 RPC，不使用真实资产。创建空钱包不需要 BNB；正常从 BSC 钱包转出资产需要 BNB 手续费。
