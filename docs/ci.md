# CI 与发布规则

工作流会先比较变更文件，再决定执行哪些任务。PR 和 `main` 提交始终产生检查结果，避免文档 PR 因缺少必需检查而无法合并。

| 变更 | 执行内容 |
| --- | --- |
| 仅文档 | 本地文档链接和图片检查；跳过依赖安装、容器构建、Artifact、Package 发布与部署 |
| 代码、依赖、测试、部署或工作流配置 | 完整检查和容器验证；合并 `main` 后发布镜像，按部署开关更新服务器 |
| 手动运行工作流 | 显式运行完整流程；可用于重新部署和首次验收 |

文档范围包括 `docs/`、根目录 Markdown / reStructuredText 文件，以及 GitHub 的 Issue 和 PR 文本模板。`src/` 等业务目录中的 Markdown 仍按代码变更处理，以免误跳过将来的人格或提示词更新。

移动文件时，同时检查原路径和新路径；不能确定比较范围时执行完整流程。代码和文档混合提交也执行完整流程。

若代码发布期间又合并了文档，文档流程不会取消排队中的代码流程。发布前会比较是否出现了更新的代码；只有文档更新时，已经验证的代码镜像仍可正常上线。

仅文档变更不会产生新镜像，也不会改变服务器上 `/version` 返回的构建提交号。

配置目标群并更新服务器部署脚本后，成功部署会触发 [QQ 部署汇报](deployment-reports.md)。汇报使用实际替换前后的镜像 revision；两次部署之间的文档提交也纳入摘要，不单独为文档变更发部署通知。

自动更新流程图由内置 ImageGen 生成，原始提示词保存在 [图片说明](assets/deployment-flow.prompt.md)。部署细节见 [SSH 部署](deployment-ssh.md) 或 [TAT 部署](deployment.md)。

## 依赖自动更新

Dependabot 自动版本更新 PR 已关闭，仓库不再保留其自动更新配置。GitHub Actions、Python 依赖和 Docker 基础镜像由维护者按需升级，手动提交 PR，经审查和 CI 验证后合并。正常的 CI、镜像发布与部署流程继续运行。

当前支持 Python 3.13 系列，补丁和镜像 digest 更新也需手动提交 PR。迁移 Python 系列时，需要在独立 PR 中同步 `pyproject.toml` 的版本约束、`.python-version`、Dockerfile 和工具的 Python 目标版本，并重新验证依赖与容器。

uv 的版本固定在 Dockerfile、`pyproject.toml` 的 `tool.uv.required-version` 和工作流中的 `setup-uv` 配置里。升级 uv 时须同步这些位置，并用新版本检查锁文件和构建；只修改 Docker 镜像版本的 PR 会被版本约束拒绝，不应绕过检查合并。
