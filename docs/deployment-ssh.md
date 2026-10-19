# 通过 GitHub 和受限 SSH 自动部署

本项目默认采用 SSH 部署；腾讯云 TAT 是可选的另一种触发方式。两条路线复用同一个镜像发布、健康检查和回滚事务。

## 首次准备服务器

按照 [服务器准备说明](deployment.md#2-安装-docker-与-compose) 安装 Docker 和 Compose，然后上传仓库中的 `scripts` 与 `deploy` 目录。以仓库 `weiuou/Fullish` 为例：

```sh
sudo bash scripts/server-setup.sh ghcr.io/weiuou/fullish
```

初始服务只绑定 `127.0.0.1:18080`，不需要开放应用公网端口。保留当前管理员的 SSH 登录，用于初始化和恢复。

## 创建专用部署身份

在可信本机创建一对仅用于本项目的 Ed25519 密钥，将公钥上传到服务器：

```sh
ssh-keygen -t ed25519 -f fullish-github-actions -C fullish-github-actions
sudo bash scripts/server-ssh-setup.sh fullish-github-actions.pub
```

无人值守使用的私钥应无口令，保存在 GitHub `production` 的 Secret 中。服务器会创建 `fullish-deploy` 用户，公钥设置 `restrict` 和强制命令。它只能请求部署 `/opt/fullish/image-repository` 中指定仓库的完整镜像 digest，不能取得交互 Shell、转发端口或执行任意运维命令。现有管理员密钥继续用于人工运维。

服务器防火墙和云安全组需要允许部署运行器连接 SSH。普通 GitHub 托管运行器的来源 IP 会变化；本流程不会自动修改安全组。固定来源有要求时，可使用固定出口运行器或改用 TAT，生产服务器不要运行外部 PR 的 runner。

## GitHub 配置

创建 `production` Environment，部署分支仅允许 `main`。

Repository variables：

| 名称 | 配置 |
| --- | --- |
| `DEPLOY_TRANSPORT` | `ssh` |
| `DEPLOY_PLATFORM` | `linux/amd64`，ARM 服务器改用 `linux/arm64` |
| `DEPLOY_ENABLED` | 首次验收前 `false`，成功后 `true` |

`production` Environment variables：

| 名称 | 配置 |
| --- | --- |
| `SSH_HOST` | 服务器 IP 或域名 |
| `SSH_PORT` | 默认 `22` |

`production` Environment secrets：

| 名称 | 配置 |
| --- | --- |
| `SSH_DEPLOY_KEY` | 专用私钥完整内容 |
| `SSH_KNOWN_HOSTS` | 已由可信管理员连接验证的 SSH 主机公钥记录 |
| `QQ_APP_ID` / `QQ_APP_SECRET` | 可选，必须成对设置；部署时同步到服务器并启用 QQ，详见 [QQ 指南](qq-setup.md) |

主机公钥必须通过已有可信连接或腾讯云控制台核对，不能为了连接成功关闭 `StrictHostKeyChecking`。

默认 GHCR 镜像可以保持私有。部署 job 使用本次工作流的短期 `GITHUB_TOKEN` 拉取镜像，凭证通过加密 SSH 的 stdin 传递，只写入服务器 `/run` 下的临时 Docker 配置，任务结束即删除。无需给服务器长期保存 GitHub PAT。

使用自定义镜像仓库时，仍设置 `IMAGE_REPOSITORY`、推送所需的 `REGISTRY_USERNAME` / `REGISTRY_PASSWORD`；另在 `production` 中配置只读的 `REGISTRY_PULL_USERNAME` 变量和 `REGISTRY_PULL_PASSWORD` Secret。

## 首次发布、自动更新与回滚

1. 推送 `main`，检查代码、容器与镜像发布成功。
2. 在 Actions 手动运行 `CI and delivery`，勾选 `deploy`，可在自动更新关闭时完成首次部署。
3. 通过管理员 SSH 验证 `curl --fail http://127.0.0.1:18080/version` 和健康检查。
4. 首次验证通过后，将 `DEPLOY_ENABLED` 设为 `true`；后续合并 `main` 自动更新。

服务器上的回滚命令：

```sh
sudo /opt/fullish/bin/rollback.sh
```

回滚优先使用本机保存的上一成功镜像，不需要临时 GitHub 凭证。不要清理仍用于回滚的镜像。部署失败时会恢复本次同步前的 QQ 配置；成功部署后的手动镜像回滚继续使用当前凭证。数据库的恢复需另行处理。

部署脚本或 Compose 的变更仍需显式重新安装到服务器。仅修改应用代码时，日常发布只更新镜像。
