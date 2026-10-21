# 基础工具与同机常驻沙箱

Fullish 在被 @ 或被名称叫到时，可以使用 `web_search`、`web_fetch` 和 `sandbox_exec`，再通过原有 QQ 回复入口发出一条回答。不启动编码代理、长期目标或自主后台任务。工具默认关闭，不配置时继续普通聊天。

## 调用与权限

- 模型使用 OpenAI 兼容的结构化 `tool_calls`，每轮最多四次调用；整个回复共用现有 90 秒期限。纯文本中的命令不会被执行。
- 群、成员和消息 ID 从当前 QQ 事件生成，不读取模型参数或历史中的身份声明。工具只对 `TOOLS_ALLOWED_GROUPS` 中的群开放。
- 首版所有获准群成员可使用相同的低权限工具，没有生产管理、跨群访问、密钥读取或权限提升入口。沙箱内文件属于群公共资料，同一群内不提供成员之间的文件保密。
- 全局、每群、每位成员每分钟分别最多 60、20、6 次调用；单次搜索最多五个结果。限制只在当前进程内计数，重启会重置，不是账户级计费上限。
- 工具输出和网页仅作为资料，不能授予权限。日志只记工具名、结果状态和成员散列，不记命令、输出、查询或凭证。
- 暂时只返回文本结果。生成文件可留在群工作区，尚未实现 QQ 产物上传、语音转写或附件自动导入。不要把私密文件送入这个公共环境。

## DeepSeek 搜索

在服务器私有的 `/opt/fullish/runtime.env` 添加：

```dotenv
TOOLS_ALLOWED_GROUPS=<官方群OpenID>
DEEPSEEK_API_KEY=<仅在服务器填写>
```

使用官方独立 [Search / Fetch API](https://api-docs.deepseek.com/guides/search/)，不改变聊天模型。搜索凭证只发送到固定的 `https://api.deepseek.com/v1/tools/`，不发送到结果网站或沙箱。Fetch 拒绝带凭证、非标准端口、内网 IP 及解析到内网地址的 URL；实际网页和重定向由 DeepSeek 获取，机器人不带这个密钥直接访问网页。

缺少密钥时不注册网页工具。密钥不写到 GitHub、镜像或模型提示词里。配置修改需通过正常部署重新创建机器人容器。

## 常驻环境

沙箱执行器部署在同一台 Linux 机器，使用独立的 `fullish-sandbox` 用户、Rootless Docker 和 gVisor `runsc` / `systrap`。生产机器人继续使用原来的 Docker；它只挂载认证的 Unix socket `/run/fullish-sandbox/api.sock`，不持有 Docker socket。

每个允许的群在 broker 启动时预热一个容器，后续命令复用；目前最多配置两个群，所有命令串行执行，繁忙时直接返回错误，不堆积无限队列。对当前 2GB 服务器先只开启一个群。

| 限制 | 固定设置 |
| --- | --- |
| 单容器资源 | 1 核、512MiB 内存、不使用 swap、64 个进程/线程 |
| 网络 | `none`，不允许宿主机网络或公网 |
| 根文件系统 | 只读，非 root，移除所有 Linux capabilities |
| 群工作区 | `/workspace` 的 128MiB tmpfs；跨命令保留，文件超过一天在后续任务前清理 |
| 临时目录 | `/tmp` 的 32MiB tmpfs |
| 单次命令 | Bash，新进程，最长 30 秒；输出最多保留 12000 字节 |
| 后台进程 | 每次命令前后清理，脱离 Shell 的进程也清理 |

工作区是临时内存文件系统：机器人部署和 broker 重启通常复用现有容器；容器重建、主机重启或升级沙箱镜像会丢失文件，不是持久资料库。Shell 变量不跨调用保留。异常退出、包装器损坏或清理失败会丢弃容器，后续请求重新创建。网络/运行时错误可能意味着命令已开始执行，不应盲目重复有副作用的操作。

broker 固定镜像、路径、用户、网络和限制，模型不能传入 Docker 参数。启动和每次调用会检查 rootless、runsc、cgroup v2，以及宿主机实际 cgroup 的 CPU/内存/swap/进程限额；限制不可确认时拒绝执行，绝不降级到宿主机 Shell 或普通 runc。gVisor 增加隔离层，同机资源控制仍依赖宿主机，见 [安全模型](https://gvisor.dev/docs/architecture_guide/security/)。

## 首次部署

此步骤是独立基础设施变更，应用镜像发布不会自动安装运行时。先完成测试，再把机器人工具配置投入使用。

1. 按 [gVisor 官方安装说明](https://gvisor.dev/docs/user_guide/install/) 安装完整的稳定版 `runsc` 包及其配套文件。不要只复制旧教程中的单个二进制。
2. 创建专用普通用户 `fullish-sandbox`，home 设为 `/var/lib/fullish-sandbox`，UID 不得为 0 或 10001。按 [Docker Rootless 指南](https://docs.docker.com/engine/security/rootless/) 为它安装独立 daemon，配置 subordinate UID/GID、用户 systemd 和 linger。不要给它生产 `docker` 组权限。
3. 在该用户的 `~/.config/docker/daemon.json` 中注册 `runsc`，`runtimeArgs` 使用 `["--platform=systrap"]`。只重启这个用户的 daemon，保留生产 daemon 的默认运行时。确认 cgroup v2 的 CPU、memory、pids 委派可用。
4. 在这个用户的环境中，从当前仓库构建凭证为空的沙箱镜像：

```sh
docker build -f deploy/Dockerfile.sandbox -t fullish-sandbox:local .
docker image inspect --format '{{.Id}}' fullish-sandbox:local
```

基础镜像和 worker 在构建时固定。首版只有 Bash、Python 标准库和基础系统程序；额外依赖由维护者修改镜像安装，运行中不能联网安装。

5. 管理员使用上一步的完整 `sha256:…` 安装 broker：

```sh
sudo bash scripts/sandbox-setup.sh <群OpenID> sha256:<完整镜像ID>
```

脚本要求 rootless daemon 已运行；安装独立服务、生成私有 token，并在第一次安装时同步机器人环境。已有 `/etc/fullish-sandbox.env` 会保留，升级时需显式同步镜像/群配置。该脚本不会重启机器人，也不会修改生产 Docker daemon。GID 10001 用于机器人访问 socket；被其他组占用时脚本停止，需人工调整。

6. 用安装了 HTTPX 的可信管理员 Python 环境执行真实 broker 验收：

```sh
sudo /path/to/python scripts/sandbox-smoke.py
```

它读取 root 所有、0600 的 `/etc/fullish-sandbox.env`，只运行合成测试，不向 QQ 发消息；检查计算、跨任务文件复用、凭证缺失、只读镜像、断网、超时、后台清理和输出截断。broker 在这之前已校验实际 cgroup 限额。任何检查失败时不要启用工具。

7. 更新服务器 Compose 副本（新增 broker socket 目录挂载），然后通过正常 CI/CD 部署机器人，使其读取 `SANDBOX_SOCKET`、`SANDBOX_TOKEN`、`TOOLS_ALLOWED_GROUPS`。不需要开放公网端口。

## 升级、停用与验证范围

升级 worker 时以专用用户重建镜像，在 `/etc/fullish-sandbox.env` 更新固定镜像 ID，重启 `fullish-sandbox.service`；会重新创建不匹配的容器，群工作区随之清空。修改 broker 源码后重新安装服务文件；安装器保留已有配置，不会擅自替换镜像。

停用机器人沙箱工具时清空 `/opt/fullish/runtime.env` 的 `SANDBOX_SOCKET` 和 `SANDBOX_TOKEN` 后正常部署。停 broker 不会自动销毁已常驻的任务容器；完全回收时，以专用用户仅删除名称以 `fullish-sandbox-` 开头、经核对属于本功能的容器，再停专用 rootless daemon。不要操作生产 daemon。

自动测试使用合成身份和假 HTTP/Docker 接口验证工具循环、越权参数、限流、失败回退、认证和配置拒绝；CI 另在普通 Docker 的临时容器验证 worker 行为。这些测试不等于真实 gVisor 隔离验收，服务器上的 smoke 必须单独通过。
