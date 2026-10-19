# 本地开发

使用 `.python-version` 指定的 Python 和 `pyproject.toml` 指定版本的 uv。开发环境不需要 QQ 账号、模型额度或生产凭证。

```sh
uv sync --locked
uv run python -m fullish
```

## 健康与版本接口

| 本地地址 | 含义 |
| --- | --- |
| `http://127.0.0.1:8080/health/live` | HTTP 进程可以响应 |
| `http://127.0.0.1:8080/health/ready` | 基础服务已完成启动 |
| `http://127.0.0.1:8080/version` | 应用版本、构建提交和运行模式 |

当前响应的 `mode=foundation`、`qq_connected=false` 表示运行的是部署骨架。QQ、模型和存储接入后，再扩展 readiness 的判断条件。

通过 `HOST`、`PORT`、`LOG_LEVEL` 调整本地运行配置。服务器的回环端口映射为 18080，详见 [部署指南](deployment-ssh.md)。

## 检查命令

```sh
python scripts/check_docs.py
uv sync --locked --group deploy
uv run --locked --group deploy ruff check .
uv run --locked --group deploy ruff format --check .
uv run --locked --group deploy pyright
uv run --locked --group deploy pytest
```

提交与评测约定见 [贡献指南](../CONTRIBUTING.md)。
