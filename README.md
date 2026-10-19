# Fullish

Fullish 的目标是成为可自托管、可扩展的 QQ AI 群友：读取群聊、理解话题、决定是否接话，并在消息入口忽略私聊。

当前仓库处于**部署基础阶段**。已经提供 NoneBot2 服务骨架、进程健康探针、容器构建和腾讯云自动部署流程；尚未接入 QQ、模型或数据库。探针成功表示基础服务可运行，不表示机器人已经可以聊天。

## 开发与部署

技术基础为 Python 3.13、uv、NoneBot2、Docker Compose 和 GitHub Actions。开发者本地运行不需要 QQ 账号、模型额度或腾讯云凭证。

```sh
uv sync --locked
uv run python -m fullish
```

使用 `.python-version` 指定的 Python 和 `pyproject.toml` 指定的 uv 版本。启动后访问：

| 地址 | 含义 |
| --- | --- |
| `http://127.0.0.1:8080/health/live` | HTTP 进程能够响应 |
| `http://127.0.0.1:8080/health/ready` | 基础服务已完成启动 |
| `http://127.0.0.1:8080/version` | 应用版本、提交号与当前运行模式 |

基础检查：

```sh
uv sync --locked --group deploy
uv run --locked --group deploy ruff check .
uv run --locked --group deploy ruff format --check .
uv run --locked --group deploy pyright
uv run --locked --group deploy pytest
```

## 自动更新流程

```mermaid
flowchart LR
    A[提交 PR] --> B[无生产凭证的自动检查]
    B --> C[合并 main]
    C --> D[测试与构建容器镜像]
    D --> E[GHCR 或配置的镜像仓库]
    E --> F[腾讯云 TAT 执行部署命令]
    F --> G[服务器拉取指定 digest]
    G --> H[Compose 更新与健康检查]
    H -->|失败| I[尝试恢复上一镜像]
```

服务器只负责运行已构建的镜像。首次初始化完成后，合并到 `main` 即可触发更新。自动部署默认关闭，配置完成后设置 `DEPLOY_ENABLED=true` 开启。

腾讯云 Ubuntu / Debian 的完整首次操作、GitHub 变量与权限设置、验收和回滚见 [部署指南](docs/deployment.md)。多人协作约定见 [贡献指南](CONTRIBUTING.md)，设计边界见 [架构说明](docs/architecture.md)。

## 目录

```text
.github/workflows/  自动检查、镜像发布和部署工作流
src/fullish/        NoneBot2 服务与健康探针
tests/              可在无外部凭证环境运行的测试
deploy/             Compose、运行配置示例和 TAT 命令模板
scripts/            服务器初始化、部署、回滚和 TAT 调用
docs/               部署操作与架构决策
```

## 接下来

1. 在腾讯云服务器验证完整发布与回滚链路。
2. 验证 QQ 官方接入的群消息权限，实现接收群消息、过滤私聊和发送回复。
3. 添加群级调度、模拟群聊回放、人格和模型接口。
4. 按实际需求引入 PostgreSQL、迁移和备份；建立对话自然度评测。

公开发布并接受外部贡献前，需要确定开源许可证与维护者名单。
