# Dim 与 visualize 接入及验证

状态：功能开发中，尚未完成生产验收。日期：2026-11-12。设计与后续范围见 [开发路径](agent-runtime-plan.md)，接口与存储设计记录于 [Issue #43](https://github.com/weiuou/Fullish/issues/43)，绘图需求见 [Issue #42](https://github.com/weiuou/Fullish/issues/42)。

## 已实现的接入结构

Fullish 可以为显式列出的群选择 Dim 回复后端。每个被叫到的消息建立独立任务会话，输入使用现有群上下文；Dim 输出经 Fullish 的公共发送服务确认后才记入群历史。新后端关闭额外的表达后处理。其他群沿用原后端。

首版固定 `dimcode-alpha@0.1.0-alpha.20`、原生协议 v3，插件 SDK/runtime 固定 `0.19.3`。选择原生 App Server 协议的原因是固定版本 ACP 创建请求不能直接设置本方案需要的工具白名单与 system prompt；没有维护第二套生产控制协议。

插件在受保护的本地通道上调用 Fullish，真实群身份由运行授权绑定。它能读取绘图指南、请求离线渲染、查看 PNG 并登记最终候选。Fullish 负责把候选送给 QQ；插件登记成功不代表发送成功。一次任务最多两次渲染；原生模型循环、任务时间和桥接调用也有上限。

最小运行登记位于 `GROUP_CONTROL_DIR/agent-runs.sqlite3`。它只保存事件摘要、run/session 和执行/投递状态，不保存可重放的文字或图片。相同事件重投不会再次执行，重启后的未决运行与投递标为不明。成功记录默认保留七天，未决记录不自动过期；该登记不提供自动任务恢复或补发。

## 当前验证证据

| 层次 | 结果 | 说明 |
| --- | --- | --- |
| 固定 Dim 原生协议 | 已通过本地探针 | 真实 Windows 二进制与认证 named pipe；本地模拟模型，无付费调用 |
| Dim 插件与最终候选 | 已通过本地探针 | 真实 rutis 插件、受限工具集合和私有 HTTP 桥接 |
| 完整渲染与修正 | 已通过本地探针 | 真实 Dim/插件/浏览器，两次渲染；真实 PNG 到达模型请求，最终字节回到 Fullish；broker 为外部模拟边界 |
| 群聊与投递语义 | 已有模拟回放 | 确认后记历史、撤权、重启去重、取消与跨会话产物拒绝 |
| Linux Dim 容器 | 已通过本地探针 | 真实 alpha.20、私有 Unix RPC、插件、候选登记；断网、只读根目录、512 MiB/128 PIDs |
| 渲染容器及 rootless gVisor | 待完成 | 本地浏览器成功不证明生产沙箱可运行；已加入独立 Linux CI 门禁 |
| 实际模型质量与 QQ 实群 | 待完成 | 当前模型和 QQ 使用合成外部边界 |

Windows 验收二进制源码为 `d3910d14999119c37d61d5d52139dfa29b81eab3`，SHA-256 为 `d5bda3496bd6782f574ae7446158c16731829d32e8bfb63fab150f48ea544d25`。其他平台须分别记录实际二进制摘要，不能沿用该值。

## 本地开发与探针

使用 Python/uv 的版本约束执行：

```sh
uv sync --locked --group deploy
npm ci --ignore-scripts --prefix plugins/visualize
npm ci --ignore-scripts --prefix plugins/visualize/renderer
uv run --locked python scripts/probe_dim.py --executable /absolute/path/to/dimcode
uv run --locked python scripts/probe_dim_plugin.py --executable /absolute/path/to/dimcode
```

`--executable` 指向原生二进制而非 PowerShell/npm 启动脚本。探针创建临时独立 home、工作区与模拟模型，只使用合成输入，退出时关闭并回收自己启动的 daemon。可用 `--output` 把包含合成 Timeline 的诊断报告保存到 `.state/`，不要用这套 fixture 接收真实群聊。

增加 `--browser /absolute/path/to/chromium` 时，插件探针通过模拟 broker 调用真实本地渲染器，验证看图、同轮修正一次、最终 PNG、公共发送服务与模拟 QQ 确认后的历史。这项检查明确不证明 gVisor 隔离。renderer 的独立测试为：

```sh
cd plugins/visualize/renderer
FULLISH_CHROMIUM=/absolute/path/to/chromium node --test render.test.mjs
```

## 拟上线配置

以下文件和命令用于准备后续验收；完成真实沙箱和部署检查后才能作为上线依据。

```sh
docker build -f deploy/Dockerfile.agent -t fullish-agent:local .
docker build --target visualization -f deploy/Dockerfile.sandbox -t fullish-sandbox:visualization .
```

普通沙箱仍是默认构建目标。渲染镜像预装 Node、Chromium、中文字体、Mermaid 和 Playwright，运行时断网；源文件与输出有大小、像素、路径及时间限制。HTML/SVG 的页面脚本关闭，Mermaid 使用受限 DSL 和严格模式，远程与文件子资源被拒绝。

使用独立的 [Compose 覆盖文件](../deploy/compose.agent.yaml) 加入 agent；它只挂载独立 home、任务工作区、RPC 目录与两份所需凭证，不挂载 bot 控制数据库、钱包或宿主机目录。服务使用 UID/GID 10001，与 bot 通过同 UID 的私有 socket 通信；容器文件系统仍分离。CPA 经宿主机回环地址访问，agent 不开放 WebSocket 或公网服务。

维护者在部署目录准备 `agent.env`（参考 [模板](../deploy/agent.env.example)）和私有 `agent-secrets/model-key`、`agent-secrets/bridge-token`。bridge token 至少 32 字符，凭证为 UID/GID 10001 所有的 `0600` 普通文件；bot 只挂载 bridge token。真实值不进入仓库、Compose 环境文本或命令参数。

在 bot 的既有私有 `runtime.env` 中显式设置：

```dotenv
AGENT_BACKEND=dim
DIM_ALLOWED_GROUPS=your-explicit-test-group
SANDBOX_VISUALIZATION=true
```

`compose.agent.yaml` 提供固定 `DIM_ENDPOINT`、`DIM_WORKSPACE_ROOT`、`DIM_BRIDGE_SOCKET` 和 `DIM_BRIDGE_TOKEN_FILE`。测试群仍须通过原群策略允许聊天、沙箱和图片。Dim 第一版仅接受具体群清单，不接受 `*`。仅文字后端可以关闭 `SANDBOX_VISUALIZATION`，不启动可视化桥接。

维护者需显式把覆盖文件安装到既有部署目录，在 `agent-image` 文件保存经过核对的不可变 agent 镜像 digest，并预先启动该 companion。新版 `scripts/deploy.sh` 发现覆盖文件后将其加入每次 Compose 操作；不会自行安装它或替换其中的镜像版本。普通 bot 镜像更新不负责下载新 agent 依赖或升级沙箱镜像。

启用 Dim 时，发布在停止旧 bot 前运行 `python -m fullish.agents.check --preflight`，新 bot 启动后再次执行不带参数的检查。它验证固定版本、插件状态和已授权群的 SVG/Mermaid/HTML 真实沙箱 PNG 取回；后置检查还验证私有桥接拒绝未绑定 session。检查不调用模型或 QQ。失败会阻止替换或触发原有回滚，不记录健康发布。没有授权绘图的群时明确报告零次绘图检查，不能把该结果当作该群已获绘图能力。

Dim 的 home 会持久化 Timeline；任务结束会请求关闭并删除对应 session，但不能把该动作当作整个状态卷的安全擦除。实群前仍须确认发行版本的附件/数据库保留行为和运维清理规则，不将生产群聊写进开发者个人 Dim home。

## 停用与恢复边界

先停止接收新 Dim 任务，等待在途完成或撤销授权并确认停止，再将 `AGENT_BACKEND=legacy`。保持 Dim endpoint/运行登记配置时，新路由仍抑制已占用事件，避免切换后端重跑。结果不明的运行只查询和人工核对，不自动提交或补发。

回滚到不认识新运行登记的旧二进制前，先排空并核对未决任务；旧版本没有新增的跨重启去重保证。删除 Compose 覆盖文件之前同步撤销 bot 的 agent 配置，保留私有 home 和登记用于核对。不要把删除持久卷作为普通回退步骤。

持续会话、跨轮改图、公开网页托管和后台 agent 任务仍按 [开发路径](agent-runtime-plan.md) 的后续阶段独立交付。
