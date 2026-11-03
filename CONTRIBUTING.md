# 参与 Fullish

Fullish 已具备 QQ 群聊、CPA 模型与工具接入、可重复部署和回滚能力。下一步聚焦统一发送与可恢复的一次性提醒，范围见 [路线图](docs/roadmap.md)。贡献者不需要获得生产服务器权限或任何生产密钥。

## 开始开发

安装 `pyproject.toml` 指定版本的 uv，在克隆的仓库根目录执行：

```sh
uv sync --locked --group deploy
uv run python -m fullish
```

Python 版本以 `.python-version` 为准。`uv.lock` 是共同使用的依赖解析结果，应与依赖声明一起提交；日常开发使用 `--locked`，依赖升级在独立 PR 中执行并说明原因。

本地进程默认在 8080 端口启动。可通过进程环境变量 `HOST`、`PORT`、`LOG_LEVEL` 调整运行配置。`APP_REVISION` 由镜像构建写入，部署时保留该值以便追溯。

## 提交前检查

```sh
uv run --locked --group deploy ruff check .
uv run --locked --group deploy ruff format --check .
uv run --locked --group deploy pyright
uv run --locked --group deploy pytest
```

修改 Linux 部署脚本时，还应在 Linux / WSL / Git Bash 中运行 Bash 语法检查，并在测试环境检查实际 Docker 行为。修改工作流或 Compose 后，检查对应配置能否解析。CI 的容器构建和运行检查用于验证镜像入口与健康探针。

Linux 下可执行 `bash tests/deploy_smoke.sh`，验证部署成功、失败恢复和配置更新等事务场景。`tests/setup_smoke.sh` 只允许在临时容器中运行，CI 已配置隔离执行；它不会使用真实 Docker socket 或云凭证。

Windows 下编辑 Shell 文件保持 LF 换行；仓库的 `.gitattributes` 负责统一文本格式。

## PR 约定

- 一个 PR 聚焦一个可以验证的行为，说明改动原因、验证方式和对配置的影响。
- 功能验证优先采用少量代表性场景回放，用关联 trace 串起入口、授权、模型与工具、发送确认和最终状态，并断言实际结果与必要的异常分支。修复缺陷时优先补能复现问题的链路场景，不追求单测数量或覆盖率；仅对链路难以覆盖的关键逻辑补少量单测，避免照着实现写测试。文档或纯格式修改无需新增测试，已有检查仍应通过。
- 新增网络服务调用时，默认仅在外部边界使用假实现或模拟返回，保留真实内部模块协作；真实 QQ、模型或腾讯云调用仅用于维护者控制的集成验证。PR 应区分模拟回放和真实集成证据，不能用模拟成功代替实际接通的结论。Trace 不记录凭证或未经授权的群聊内容。
- 修改外部接口、数据存储、部署权限或发言调度前，先在 Issue 中说明设计和兼容方式。
- 不提交 QQ 密钥、模型密钥、云访问密钥、生产配置或未获许可的真实群聊记录；评测使用合成或脱敏样本。

维护者合并 PR 后，`main` 上的工作流构建镜像；仅在部署开关和 `production` 环境配置完成后更新服务器。外部 PR 的自动检查不读取生产凭证。

## 对话模块的贡献边界

适配器将 QQ 事件转换成内部记录，`conversation` 保存上下文，`reply` 编排回应，模型和工具只产生候选结果。聊天与部署简报共用 `scheduling` 群队列和 `sending.service` 投递服务；新增触发源复用这两个入口，不直接发送 QQ 消息。当前职责和依赖见 [架构边界](docs/architecture.md#模块职责与依赖)，持久化提醒等后续范围见 [主动消息草案](docs/proactive-messaging.md)。

私聊过滤应发生在事件入口，在保存上下文和调用模型之前完成。对话回放测试应同时覆盖「发出回复」与「保持沉默」。

## 维护者配置

每次任务开始与交付前核对涉及文档的有效性，及时更新现行指南；被替代的完整方案按 [归档规则](docs/archive/README.md) 移入 `docs/archive/`，记录原因、日期和替代入口并修复链接。普通开发不默认读取或检索归档正文，考证历史时再按需查阅，避免旧说明进入当前决策上下文。

新增、改名、移动或归档文档时，检查交叉引用的目标路径、章节锚点、入链和出链，以及引用内容是否仍支持正文结论；现行说明应指向现行替代，历史引用须标注。运行 `python scripts/check_docs.py` 验证本地路径和图片，另外核对脚本未覆盖的锚点与引用语义；修改外部引用时按需核对实际来源。

公开协作前确定许可证与维护者名单，并在 GitHub 上启用 `main` 分支保护：要求 PR、通过 CI、对工作流和部署脚本执行代码审查。`production` 环境只允许 `main` 部署；需要自动更新时无需为每次部署添加人工确认。

部署脚本和 Compose 的服务器副本需要显式更新，日常镜像发布不会悄悄替换服务器运维配置。操作方式见 [部署指南](docs/deployment.md)。
