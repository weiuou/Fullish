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

自动更新流程图由内置 ImageGen 生成，原始提示词保存在 [图片说明](assets/deployment-flow.prompt.md)。部署细节见 [SSH 部署](deployment-ssh.md) 或 [TAT 部署](deployment.md)。

## 依赖自动更新

Dependabot 每周检查 GitHub Actions、Python 依赖和 Docker 基础镜像，更新先作为 PR 执行检查，由维护者审查后合并。

Python 镜像的自动版本更新保持在当前支持的 3.13 系列，继续接收补丁和镜像 digest 更新；3.14 及以上版本暂不自动升级。迁移 Python 系列时，需要在独立 PR 中同步 `pyproject.toml` 的版本约束、`.python-version`、Dockerfile 和工具的 Python 目标版本，并重新验证依赖与容器。

uv 的版本固定在 Dockerfile、`pyproject.toml` 的 `tool.uv.required-version` 和工作流中的 `setup-uv` 配置里。升级 uv 时须同步这些位置，并用新版本检查锁文件和构建；只修改 Docker 镜像版本的 PR 会被版本约束拒绝，不应绕过检查合并。
