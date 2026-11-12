# Fullish 与 Dim 同容器及只读自检

状态：候选实现正在验证，尚未切换生产。2026-11-13。用户要求及接口、权限、迁移合同见 [Issue #50](https://github.com/weiuou/Fullish/issues/50)，当前已上线的 companion 版本见 [接入记录](agent-runtime.md)。

## 权限边界

一个容器内运行 Fullish 与真实 Dim/rutis 两个进程树，分别使用 UID 10001 与 10002，通信组为 10003。启动器只用 SETUID/SETGID 创建两个非 root 角色，随后自身降为 65534、清空环境；角色之间不继承凭证。运行时保持只读根目录、no-new-privileges、1 核、1 GiB、256 PIDs 和无 swap。两者共享容器生命周期与资源，任一角色退出都会有界停止另一角色，再由容器重启恢复；未决任务不重放。

| 内容 | Fullish | Dim |
| --- | --- | --- |
| `/opt/fullish-readonly`：同版本源码及现行文档 | 只读 | 只读 |
| `/app/.venv`：实际运行代码 | 只读 | 只读 |
| `/var/lib/fullish/control`、钱包、审计与 bot 凭证 | 所属 UID 读写 | 无私有数据访问 |
| `/var/lib/fullish-diagnostics`：脱敏部署元数据 | 写入，文件 0640 | 组只读，目录不可写 |
| Dim home、任务及临时目录 | 仅共享任务目录 | 自有目录可写 |
| RPC 与候选桥接 | 指定 UID/GID 的 0660 socket | 指定 UID/GID 的 0660 socket |

RPC 两端目录各自由所属 UID 管理，另一方只有组遍历/读取权限，不能替换对方 socket。Dim 原生 endpoint 仍位于其 0700 私有目录并保持 0600，由同 UID 的 Unix 字节代理提供 0660 共享端点；客户端同时核对文件身份和内核 peer UID/GID。旧 companion 的同 UID/0600 验证仍保留，仅显式 `DIM_DEPLOYMENT=unified` 使用固定双 UID 合同。没有 Docker socket、重启或任意宿主机命令工具。Dim 对 Fullish 的只读限制由文件系统与 Linux 身份执行，不能被群聊提示词解除。

## 群聊自检

显式开启 `DIM_INSPECTION=true` 后，本轮获准 Dim 群可调用：

- `fullish_inspect`：列目录或读取固定源码视图，每次最多 120 行、12 KB；拒绝绝对路径、越界、链接、隐藏路径及写操作。生产配置值和密钥不在源码视图中。
- `fullish_self_check`：返回真实 UID、当前群权限、只读与 capability 检查、运行版本及最近脱敏部署事件；可在图片权限下执行一次固定绘图探针。自检不创建 QQ 回复，也不能修改 Fullish。

这些工具沿用 run/session 的授权、期限与调用预算，撤权后不可再读或投递；最终仍由 `fullish_reply` 登记候选，经公共出口确认后记历史。原生隐藏 exec 未开放。旧的绘图管理方法 `visualize.probe` 继续用于部署探针。

Fullish 只导出最多 40 条部署/服务元数据，文件最多 64 KB，不包含聊天正文、diff、提示词和凭证。摘要流程记录读取 GitHub 历史与模型生成的开始、完成、错误和耗时；总超时仍为 180 秒。旧版本仅有总超时的记录不能据此追溯出当时的具体阶段。

## 镜像与部署

```sh
docker build --target unified --build-arg APP_REVISION=<源码提交> -t fullish:unified .
```

主 CI 发布此 target，默认仍是原非 root Python 单进程入口；普通 `docker build` 继续构建 slim foundation。只有显式安装 [compose.unified.yaml](../deploy/compose.unified.yaml) 才启动双角色。它不能与 `compose.agent.yaml` 同时启用。

集成配置通过宿主机回环连接 CPA，Fullish 只监听 `127.0.0.1:18080`，Dim 仅开放私有 Unix RPC。覆盖文件使用 Compose `!reset` 清空原端口映射；需要现有指南要求的 Compose 版本。

维护者保留原 `runtime.env` 和仅含 Dim 模型元数据的 `agent.env`，准备三个私有普通文件：

| 路径 | UID/GID | 模式 |
| --- | --- | --- |
| `integrated-secrets/model-key` | 10002/10003 | 0600 |
| `integrated-secrets/bridge-fullish` | 10001/10003 | 0600 |
| `integrated-secrets/bridge-dim` | 10002/10003 | 0600 |

两份 bridge token 内容相同且至少 32 字符，各由所属进程读取；不要通过放宽旧文件权限共享凭证。使用新的 integrated home/work/RPC/diagnostics 卷，保留旧 companion 卷。运行配置包含 `AGENT_BACKEND=dim`、明确的 `DIM_ALLOWED_GROUPS` 和 `DIM_VISUALIZATION=true`，其他路径由覆盖文件固定。

部署前 `compose run ... python -m fullish.agents.check --preflight` 在自己的临时 home、工作区和 RPC 中启动候选 Dim，不替换活跃实例的 socket，不调用模型或 QQ。发布后的检查和简报 CLI 用 UID 10001/GID 10003 执行。

## 首次迁移与回退

先在原 companion 配置下发布兼容的新主镜像，确认其健康。再备份环境、覆盖文件和发布状态，暂停新绘图并排空或停止在途任务；停止旧 companion，安装同容器覆盖文件和新的凭证/卷，用同一已验证镜像切换。旧 companion 保留但不并行运行。首次迁移失败时，协调恢复旧配置及 companion，再启动原健康镜像。

后续兼容镜像可沿用原部署回滚。若目标镜像缺少 `org.fullish.unified-runtime=true` 标签，部署在停止现有 sender 前拒绝；恢复旧二进制前必须同步恢复保存的 companion 配置。不要删除状态卷或自动补发不明消息来完成回退。

## 验收边界

[单容器场景](../tests/unified_agent_integration.mjs) 使用实际 Linux、双 UID、固定 Dim/rutis 与 Chromium；仅模型和 QQ 为模拟边界。它检查源码/状态/诊断/凭证和 `/proc`、进程信号、root 恢复的实际权限，以及自检、源码阅读、两次 PNG 到模型、最终字节与确认后历史；另核对独立 preflight 不改变活跃 RPC 且零模型调用。

实际检查结果以对应候选 CI 为准。生产切换、真实模型与 QQ 的证据需另外记录，不把模拟通过视为已经上线。
