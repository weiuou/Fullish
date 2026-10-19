# 腾讯云服务器部署指南

本文面向基本空闲的 Ubuntu / Debian 云服务器，使用 GitHub Actions 构建镜像，腾讯云自动化助手 TAT 触发服务器更新。操作完成后，合并到 `main` 会执行检查、发布镜像、更新容器并验证健康状态。

当前发布的是基础服务，尚不能作为 QQ 机器人聊天。本文配置的健康检查只验证基础服务启动。仓库文件本身不会配置你的腾讯云账号或 GitHub；以下首次设置需要在你的账号中完成。

## 1. 确认服务器与网络

在腾讯云控制台记录实例 ID 和地域，例如 `ins-…` / `lhins-…` 与 `ap-guangzhou`，确认实例正在运行、位于 VPC，TAT 客户端在线。CVM 和轻量应用服务器均可使用 TAT；客户端安装与检查遵循 [腾讯云官方说明](https://cloud.tencent.com/document/product/1340/51945)。

首次登录服务器后执行：

```sh
cat /etc/os-release
uname -m
df -h /
```

默认镜像目标为 `linux/amd64`，对应 `x86_64`。若服务器是 `aarch64`，在后文设置 `DEPLOY_PLATFORM=linux/arm64`。保留足够空间容纳当前镜像和上一个版本，并按实际运行量监控磁盘。

服务器需要访问选定的镜像仓库，TAT 客户端需要正常连接腾讯云。中国大陆地域对 GHCR、Docker Hub 的访问情况需要在这台服务器实测；可达性不能由工作流构建成功来证明。

SSH 仅用于你自己首次初始化和必要运维，按个人访问来源设置安全组即可；日常 GitHub 部署通过 TAT，不需要向 GitHub 运行器开放 SSH。服务器上的基础服务只监听回环地址的 18080 端口，无需新增公网入站规则。

## 2. 安装 Docker 与 Compose

按服务器实际系统，使用 [Ubuntu 官方安装步骤](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository) 或 [Debian 官方安装步骤](https://docs.docker.com/engine/install/debian/#install-using-the-apt-repository)，添加 Docker 官方 apt 仓库后安装：

```sh
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

上面的安装命令依赖先完成相应文档的 apt 源设置。使用 `docker compose` 插件，版本至少为 2.30，以支持运行配置的 `format: raw`；不要混用旧版 `docker-compose` 命令。初始化脚本不会安装 Docker 或修改服务器 apt 源。服务器还需提供 `flock` 和 `timeout`，Ubuntu / Debian 通常分别由 `util-linux` 和 `coreutils` 提供。

## 3. 将部署文件安装到服务器

将仓库上传到你自己的 GitHub 仓库，默认分支设为 `main`。首次可以把代码包上传到服务器，或克隆你已经创建的仓库。示例中的 `OWNER`、`SERVER`、`USER` 均需替换为实际值。

在本地仓库根目录，可以仅上传初始化需要的文件：

```sh
ssh USER@SERVER "mkdir -p ~/fullish-bootstrap"
scp -r scripts deploy USER@SERVER:~/fullish-bootstrap/
```

先确定镜像仓库路径：默认使用全小写的 `ghcr.io/owner/fullish`，如选择 TCR 则使用它的完整仓库路径，详见下一节。随后登录服务器执行，将示例仓库替换为你的实际值：

```sh
cd ~/fullish-bootstrap
sudo bash scripts/server-setup.sh ghcr.io/owner/fullish
sudoedit /opt/fullish/runtime.env
```

初始化会把 Compose 和部署、回滚脚本安装到 `/opt/fullish`，并把唯一允许的镜像仓库写入 `image-repository`。参数只能是仓库路径，不能包含标签或 digest；重新执行时若仓库不一致会拒绝修改。运行配置来自 `deploy/runtime.env.example`，已存在的 `runtime.env` 和部署状态会保留。以后添加 QQ、模型密钥时，在服务器编辑该文件；不要放进仓库、Dockerfile、构建参数或 TAT 命令参数。

服务器路径约定：

| 路径 | 用途 |
| --- | --- |
| `/opt/fullish/compose.yaml` | 单实例 Compose 配置 |
| `/opt/fullish/runtime.env` | 应用运行环境变量 |
| `/opt/fullish/image-repository` | 唯一允许部署的镜像仓库 |
| `/opt/fullish/bin/deploy.sh` | 部署指定 digest 的镜像 |
| `/opt/fullish/bin/rollback.sh` | 恢复记录的上一成功镜像 |
| `/opt/fullish/state/release.env` | 原子保存当前与上一成功镜像的 digest |

Compose 和运维脚本的变更需要维护者重新上传并执行初始化。日常 CI/CD 只更新应用镜像，所以调整服务器编排不会随普通代码合并隐式生效。

## 4. 选择并验证镜像仓库

默认使用 `ghcr.io/你的账号/fullish`，仓库名转换为小写。Actions 使用该仓库的 `GITHUB_TOKEN` 推送镜像。

GitHub Container Registry 首次发布的包默认是私有的，即便代码仓库公开。开源镜像可在 Package 设置中改为公开，让服务器匿名拉取；如果继续私有，则在服务器以执行 Docker 的 root 身份登录，使用仅能读取该镜像的凭证。见 [GHCR 访问说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。

例如私有 GHCR 在服务器交互式执行：

```sh
sudo docker login ghcr.io --username YOUR_GITHUB_USERNAME
```

在密码提示中输入具有相应包读取权限的 token，不把 token 写入命令历史。首次构建完成后，从 Actions 输出获取完整镜像 digest，在服务器验证 `sudo docker pull 仓库@sha256:摘要`。

如果访问 GHCR 不稳定，使用你已经配置好的腾讯云 TCR 或其他可达仓库，并设置下表的自定义仓库参数；初始化时传入的仓库必须与它相同。GitHub 托管运行器必须能推送，服务器必须能拉取同一仓库。TCR 的实例、网络和计费配置需要单独选择，本流程不会自动开通或购买 TCR。

私有自定义仓库需要两份用途不同的认证：GitHub 保存推送凭证，服务器通过 `sudo docker login 仓库域名` 保存读取凭证。服务器凭证过期时，需更新登录后再部署。

## 5. 创建固定的 TAT 部署命令

在腾讯云自动化助手中创建保存的 Shell 命令：

1. 内容使用仓库中的 `deploy/tat-command.sh`，不要上传应用密钥。
2. 开启自定义参数，参数名为 `image`，由工作流传入完整镜像 digest。
3. 执行身份设为 `root`，工作目录为 `/opt/fullish`；命令需要访问 Docker 和 root 管理的运行配置。
4. 命令超时设为 900 秒，覆盖镜像拉取、健康检查和失败恢复。工作流另用 `TAT_WAIT_TIMEOUT=960` 等待结果，多留 60 秒用于观察远端完成；部署 job 总超时为 20 分钟。这三个时间分别约束远端命令、状态查询和整个 GitHub job。
5. 保存命令 ID。先在控制台对当前实例运行一次，使用第一份镜像的完整 digest。

TAT 的 `InvokeCommand` 接收命令 ID、实例 ID 和参数，返回执行活动 ID；调用成功只代表已经提交，工作流还会查询执行任务并检查退出结果。见 [接口文档](https://cloud.tencent.com/document/api/1340/52678)。

创建专用 CAM 用户供 GitHub 部署使用，仅授予 `tat:InvokeCommand` 和 `tat:DescribeInvocationTasks` 所需权限，将资源限制到这个命令和这台实例。按实例类型使用 CVM 或 Lighthouse 的资源标识，参照 [TAT 支持的 CAM 资源与操作](https://cloud.tencent.com/document/product/598/70026) 在控制台验证策略。不要授权创建、修改或执行任意临时命令。

TAT 凭证可以触发 root 身份的部署，属于生产权限。固定命令和 digest 校验用于减少误操作，不能将该凭证交给不可信贡献者。

## 6. 配置 GitHub 仓库与生产环境

在仓库 Settings 中创建名为 `production` 的 Environment，并将允许部署的分支限定为 `main`。这套环境功能在公开仓库可用；私有仓库需核对你的 GitHub 计划。见 [GitHub 环境说明](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)。

在 **Repository variables** 设置：

| 名称 | 内容 |
| --- | --- |
| `DEPLOY_ENABLED` | 配置与首次验证完成后设为 `true`；未设置时仅检查和构建，不部署 |
| `DEPLOY_PLATFORM` | 可不填，默认 `linux/amd64`；ARM 服务器填 `linux/arm64` |
| `IMAGE_REPOSITORY` | 可不填，默认 `ghcr.io/owner/repo`；使用 TCR 时填完整仓库路径，不含标签或 digest |
| `REGISTRY_USERNAME` | 仅自定义镜像仓库使用的推送用户名 |

在 **Repository secrets** 设置：

| 名称 | 内容 |
| --- | --- |
| `REGISTRY_PASSWORD` | 仅自定义镜像仓库需要，填写推送凭证；默认 GHCR 使用 `GITHUB_TOKEN` |

在 **production / Environment variables** 设置：

| 名称 | 内容 |
| --- | --- |
| `TENCENTCLOUD_REGION` | 服务器所在地域，如 `ap-guangzhou` |
| `TAT_COMMAND_ID` | 已创建的 TAT 命令 ID |
| `TAT_INSTANCE_ID` | 唯一目标实例 ID |

在 **production / Environment secrets** 设置：

| 名称 | 内容 |
| --- | --- |
| `TENCENTCLOUD_SECRET_ID` | 专用 CAM 用户的 SecretId |
| `TENCENTCLOUD_SECRET_KEY` | 对应 SecretKey |

运行身份的密钥只进入部署 job。PR 测试不使用生产 Environment，不读取这些凭证；QQ 和模型密钥也不需要进入 GitHub。

启用 `main` 分支保护，至少要求 PR 和自动检查通过；代码与工作流修改经过维护者审查。配置完成后设置 `DEPLOY_ENABLED=true`，无需为每次部署添加人工审批即可自动更新。以后需要发布窗口或额外审批时，再调整 Environment 规则。

## 7. 首次发布与验收

1. 先保持自动部署关闭，推送到 `main`，确认检查与镜像构建成功。
2. 确认镜像公开性或服务器登录，并在服务器拉取本次完整 digest。
3. 在 TAT 控制台运行保存的命令，确认服务器部署成功。
4. 完成 GitHub `production` 配置并开启部署开关，在 Actions 中对 `main` 手动运行工作流，或合并一个小 PR。
5. 检查 Actions 中的 TAT 任务成功记录，并在服务器验证版本。

服务器验证命令：

```sh
curl --fail http://127.0.0.1:18080/health/live
curl --fail http://127.0.0.1:18080/health/ready
curl --fail http://127.0.0.1:18080/version
```

`/version` 的 `revision` 应与本次提交对应；健康响应中的 `mode=foundation` 和 `qq_connected=false` 是当前预期。重启服务器后再次检查，验证容器恢复启动。

查看当前容器与发布记录：

```sh
sudo docker ps --all --filter label=com.docker.compose.project=fullish
sudo cat /opt/fullish/state/release.env
```

从第一条输出取得容器 ID，再用 `sudo docker logs --tail 100 容器ID` 查看最近日志。不要把 `runtime.env` 的内容附到公开 Issue 或构建日志中。

## 8. 发布失败与回滚

部署以镜像 digest 为准。拉取失败时保留现有运行版本；替换容器后健康检查失败，会尝试恢复记录的上一成功镜像。本次 Actions 仍应标记失败，让维护者知道新版本没有上线。镜像回滚不恢复 `runtime.env` 或 Compose 配置；如果问题来自配置变更，需要先恢复相应配置。重新部署同一镜像也会重启服务并应用当前配置。

如需主动回退，在服务器执行：

```sh
sudo /opt/fullish/bin/rollback.sh
curl --fail http://127.0.0.1:18080/version
```

第一次部署没有上一成功版本，失败后需要修复配置或镜像再执行。回滚也可能受磁盘、配置或 Docker 故障影响；检查脚本输出并确认 `/version`，不要把「开始回滚」当成「回滚成功」。

| 现象 | 优先检查 |
| --- | --- |
| 构建成功、拉取失败 | 服务器网络、镜像仓库权限、root 的 Docker 登录、digest 是否存在 |
| TAT 调用失败 | 地域与实例 ID、agent 在线状态、VPC、CAM 授权 |
| TAT 已提交但任务失败 | TAT 执行输出、退出码、服务器部署脚本和 Docker 状态 |
| 镜像启动失败 | CPU 架构、`runtime.env`、容器日志、健康检查 |
| Actions 超时 | 到 TAT 控制台确认远端任务是否仍在执行，再决定下一步 |
| 手动回滚后又更新 | 后续提交仍会触发自动部署；需要调查时将 `DEPLOY_ENABLED` 暂时设为 `false` |

服务器和 GitHub 都限制并行部署。不要在有远端任务执行时反复触发重试；先确认已有任务状态。

## 9. 后续接入 QQ 和数据库

接入 QQ 时，先在测试群验证消息收发，再扩展 readiness 和退出时的任务处理。当前单实例更新会有短暂中断，机器人应使用消息去重和可恢复的处理状态。

引入数据库前准备备份与恢复演练，采用对上一应用版本兼容的迁移。容器回滚只恢复应用镜像，不能撤销数据库内容变化。持久数据不应写入容器可写层。

这套初始流程不需要额外服务器。需要真实模型集成测试、多人并行验收或不中断升级时，再增加独立测试环境。
