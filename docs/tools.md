# 基础工具与同机常驻沙箱

Fullish 在被 @ 或被名称叫到时，可以使用 `web_search`、`web_fetch` 和 `sandbox_exec`，再通过原有 QQ 回复入口发出一条回答。不启动编码代理、长期目标或自主后台任务。工具默认关闭，不配置时继续普通聊天。

## 调用与权限

- 模型使用 OpenAI 兼容的结构化 `tool_calls`，每轮最多十二次调用；90 秒聊天期限内预留最多 15 秒生成最终答复。纯文本中的命令不会被执行。
- 群、成员和消息 ID 从当前 QQ 事件生成，不读取模型参数或历史中的身份声明。工具只对 `TOOLS_ALLOWED_GROUPS` 中的群开放。
- 首版所有获准群成员可使用相同的低权限工具，没有生产管理、跨群访问、密钥读取或权限提升入口。沙箱内文件属于群公共资料，同一群内不提供成员之间的文件保密。
- 全局、每群、每位成员每分钟分别最多 120、60、24 次调用；单次搜索最多五个结果。限制只在当前进程内计数，重启会重置，不是账户级计费上限。
- 工具输出和网页仅作为资料，不能授予权限。运行日志记录调用关联 ID、耗时、结果条数、错误码及详细记录是否保存成功；网页查询和返回内容保存在独立的私有记录中，详见下文。沙箱命令和输出不保存。
- 暂时只返回文本结果。生成文件可留在群工作区，尚未实现 QQ 产物上传、语音转写或附件自动导入。不要把私密文件送入这个公共环境。

达到调用上限或工具阶段超时后，会移除工具定义，只请求一次最终文字答复，说明已经确认与未完成的部分；收尾请求失败、继续返回工具请求、空白或沉默时，会发送坦诚说明未完成的固定答复。超额批次不会执行，已完成的工具结果继续可用于收尾。`model.finalizing` 记录收尾原因和调用次数，`model.final_answer.fallback` 标记固定答复。

## DeepSeek 搜索

在服务器私有的 `/opt/fullish/runtime.env` 添加：

```dotenv
TOOLS_ALLOWED_GROUPS=<官方群OpenID>
DEEPSEEK_API_KEY=<仅在服务器填写>
```

使用官方独立 [Search / Fetch API](https://api-docs.deepseek.com/guides/search/)，不改变聊天模型。搜索凭证只发送到固定的 `https://api.deepseek.com/v1/tools/`，不发送到结果网站或沙箱。Fetch 拒绝带凭证、非标准端口、内网 IP 及解析到内网地址的 URL；实际网页和重定向由 DeepSeek 获取，机器人不带这个密钥直接访问网页。

缺少密钥时不注册网页工具。密钥不写到 GitHub、镜像或模型提示词里。配置修改需通过正常部署重新创建机器人容器。

## 回查搜索与网页结果

每次通过校验的 `web_search` / `web_fetch` 调用会保存一条 SQLite 记录：包含查询词或网页 URL、实际返回给模型的搜索结果（标题、链接、摘要、日期）或网页正文，以及调用时间、版本、`trace_id`、工具调用 ID、群和成员摘要、耗时、错误码。保存的是客户端限长处理后的模型可见结果，不是供应商原始响应，也不保存整段群聊、模型密钥或 HTTP 请求头。

`tool.started` 和 `tool.completed` 通过 `invocation_id` 关联同一次调用。`tool.completed` 中的 `audit_id` 用于查详细记录，`audit_saved=true` 表示已成功落盘；保存失败会记录 `tool.audit.failed`，不会改变工具返回值。`result_count=0` 表示搜索完成但没有结果，不能当作找到了依据；`error_code` 表示校验、限流或网络等失败原因。只有 `tool.completed` 不能证明模型已验证最终答案。

详细内容不会写入普通 Docker 日志。已配置的服务密钥、常见密钥字段、Bearer token 和 URL 中的认证参数会被脱敏，URL 的用户密码及片段会移除；`snapshot_redacted` 标明内容是否变化。这不是任意秘密识别器，查询和公开网页仍可能包含敏感资料，应只由维护者查看，不转发到群里或公开 Issue。两类工具以外的内容、被拒绝的原始参数和供应商错误正文都不保存。

默认最多保留最近 1000 条、7 天内的记录；每次写入时清理过期和超量记录，空闲期间不会主动执行清理。单条详细 JSON 上限 96000 字节，超限会明确设置 `snapshot_truncated=true` 并保留预览；供应商正文原有的截断标记也会保留。记录文件创建为 `0600`，新目录为 `0700`（实际访问仍受系统及卷权限控制）。

Compose 将数据库保存在独立的 `fullish_tool-audit` 卷中，路径为 `/var/lib/fullish/tool-audit/audit.sqlite3`，更新、回滚或重建机器人容器会保留记录。不要对该部署运行 `docker compose down -v`，它会删除卷。本地运行默认写 `.state/tool-audit.sqlite3`，可用 `TOOL_AUDIT_PATH` 指定其他路径。

在服务器上，先从日志找到目标消息的 `trace_id`，再查询其搜索与读取结果：

```sh
sudo docker exec fullish-bot-1 python -m fullish.tool_audit --trace-id 填入trace_id
# 只查看其中一次调用；也可不带过滤参数查看最近 20 条。
sudo docker exec fullish-bot-1 python -m fullish.tool_audit --audit-id 填入audit_id
```

命令按时间顺序输出 JSON 行，`snapshot.request` 是查询，`snapshot.response` 是工具交给模型的结果。可用 `--limit 1..100` 限制条数，`--path` 查看指定数据库；查询以只读方式打开，不会创建不存在的数据库。这个入口仅供服务器维护者使用，没有对外 HTTP 接口。

已有部署需要先用新版本的 `scripts/server-setup.sh` 显式更新 Compose，再正常发布新镜像，以安装持久卷挂载；仅更新应用镜像不足以获得这个挂载。容器会继续聊天，但存储不可写时 `audit_saved=false` 并发出告警。启用前的调用没有保存详细结果，无法事后补回。

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

## 运行时与资源限制

固定版本的完整 gVisor 包由 `scripts/sandbox-runtime-setup.sh` 下载并核对 SHA-512；当前验收平台是 x86_64 Linux、cgroup v2 和 systemd。安装器只配置独立用户的 Rootless Docker，不修改生产 Docker。该专用 daemon 不创建默认网桥或 NAT，避免依赖宿主机 netfilter 权限；任务始终使用 `network=none`。可信构建阶段如需网络，可显式使用 rootless 构建的 `--network=host`，不影响运行时断网策略。

Rootless 环境下 runsc 自行遍历根 cgroup 会遇到权限错误。`scripts/sandbox-runsc.py` 将每个容器的 Sentry、Gofer 和后续 exec 进程加入同一个用户 systemd service，由 systemd 设置 CPU、内存、swap 和进程数硬限额。仅在核实这些实际控制文件后才启动 runsc。

适配器使用 `--ignore-cgroups` 的含义是把 cgroup 管理交给上述 systemd service，并不取消资源限制。broker 在启动和每次执行时再次独立读取宿主机 cgroup；无法确认限额时拒绝执行。适配器不会使用 `--TESTONLY-unsafe-nonroot`、宿主机网络、特权容器或 rootful Docker 作为回退。容器删除会停止对应 service；专用 daemon 重启会回收这些 service，工作区因此清空。

## 首次部署

此步骤是独立基础设施变更。准备好生产 Docker、`dockerd-rootless-setuptool.sh`、`rootlesskit`、`newuidmap`、`newgidmap`、用户 D-Bus、curl 和 tar；Ubuntu 的 UID 映射与用户会话依赖可安装 `uidmap dbus-user-session slirp4netns`，Docker Rootless Extras 按 [官方指南](https://docs.docker.com/engine/security/rootless/) 安装。

1. 从经过检查的当前仓库运行：

```sh
sudo bash scripts/sandbox-runtime-setup.sh
```

安装器创建或复用专用 `fullish-sandbox` 用户，配置 linger、用户 Docker、cgroup 委派及固定的 runsc 适配器。已有用户必须使用 `/var/lib/fullish-sandbox`，且不能加入生产 docker 组。重复执行在配置不变时不重启 daemon；升级固定运行时版本需要重新验收。

2. 以该用户在自己的 daemon 中构建沙箱镜像：

```sh
sandbox_uid=$(id -u fullish-sandbox)
sudo -u fullish-sandbox env HOME=/var/lib/fullish-sandbox XDG_RUNTIME_DIR=/run/user/$sandbox_uid \
  docker --host unix:///run/user/$sandbox_uid/docker.sock build --network=none -f deploy/Dockerfile.sandbox -t fullish-sandbox:local .
sudo -u fullish-sandbox env XDG_RUNTIME_DIR=/run/user/$sandbox_uid \
  docker --host unix:///run/user/$sandbox_uid/docker.sock image inspect --format '{{.Id}}' fullish-sandbox:local
```

基础镜像和 worker 在构建时固定。首版只有 Bash、Python 标准库和基础系统程序；额外依赖由维护者修改镜像安装，运行中不能联网安装。

3. 使用完整镜像 ID 安装并验收 broker：

```sh
sudo bash scripts/sandbox-setup.sh <群OpenID> sha256:<完整镜像ID>
```

该步骤同步经过检查的 Compose、发布脚本和 broker，保留现有 QQ、DeepSeek 等私密配置。它等待实际 socket 可连接并运行真实验收，全部通过后才把沙箱配置写入机器人环境；失败会恢复之前的 broker 文件，保持机器人配置未启用。重复执行保留 token 和群白名单；传入的新镜像 ID 会显式更新 broker 镜像。GID 10001 被其他组占用时停止。

4. 可以随时重复执行真实验收；只需要系统 Python 标准库，不依赖管理员环境中额外安装 HTTPX：

```sh
sudo python3 scripts/sandbox-smoke.py
```

验收只运行合成命令，不向 QQ 发消息。检查计算、文件复用、凭证缺失、只读文件系统、断网、超时、后台清理和输出截断。broker 同时验证宿主机实际限额。

5. 通过正常 CI/CD 部署机器人。候选镜像会先通过真实 Compose 挂载执行 `python -m fullish.sandbox_check`，成功才停止旧机器人；新机器人启动后再次验收，成功才记录版本和发送正常部署汇报。任一次失败都会使发布失败，启动后失败还会回滚。沙箱关闭时不要求它可用，不会发模型请求或测试群消息。

## CI 与发布门禁

完整代码 CI 除普通 Docker worker smoke 外，还会在一次性 Linux 运行器上创建独立用户、安装同一个固定 gVisor 包和适配器、启动真正的 Rootless Docker 与 broker，并从候选机器人镜像通过 Unix socket 执行验收。该步骤是镜像发布的前置条件。

回归测试还包括缺失 socket 挂载、broker 停止、实际 cgroup 限额漂移、重复安装和 daemon 重启恢复。部署事务测试验证失败发生在停止旧进程前时保留旧进程，启动后失败时回滚且不记录成功。这些检查可以阻止本次故障类型被当作健康发布，不能保证未来运行时或服务器的任何故障都不会发生。

## 升级、停用与验证范围

升级 worker 时以专用用户重建镜像，在 `/etc/fullish-sandbox.env` 更新固定镜像 ID，重启 `fullish-sandbox.service`；会重新创建不匹配的容器，群工作区随之清空。修改 broker 源码后重新安装服务文件；安装器保留已有配置，不会擅自替换镜像。

停用机器人沙箱工具时清空 `/opt/fullish/runtime.env` 的 `SANDBOX_SOCKET` 和 `SANDBOX_TOKEN` 后正常部署。停 broker 不会自动销毁已常驻的任务容器；完全回收时，以专用用户仅删除名称以 `fullish-sandbox-` 开头、经核对属于本功能的容器，再停专用 rootless daemon。不要操作生产 daemon。

单元测试继续使用合成身份验证权限、限流、认证和配置拒绝；CI 使用与部署机一致的 Docker Engine / Rootless Extras 29.9.1，避免 runner 预装 Moby 与新版 RootlessKit 混用。真实 rootless gVisor 验收和部署机上的发布前后验收共同覆盖运行时与挂载集成。升级这些运行时组件时，应一起更新固定版本并通过完整验收。
