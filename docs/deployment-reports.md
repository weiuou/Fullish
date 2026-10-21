# 部署成功后的 QQ 汇报

部署事务确认新容器健康、镜像 digest 正确并原子写入 `state/release.env` 后，才调用新容器中的汇报入口。失败部署不会发送成功通知；通知或 AI 故障只产生警告，不回滚已经健康的服务。SSH 和 TAT 共用这段服务器逻辑。

## 内容与版本范围

通知包括当前部署镜像的完整 Git commit hash、被替换版本的 hash，以及 AI 生成的简短中文更新摘要。基线来自部署开始时实际运行镜像的 `org.opencontainers.image.revision`，目标来自已验证新镜像的相同标签；不会使用最新分支 HEAD 或仅使用本次 push 的范围。

程序分页读取两个版本之间的全部新提交，再逐个读取完整提交说明及 GitHub 提供的各文件 diff，并分页读取文件列表。因此，中途合并的文档提交、后来被 revert 的中间提交也会进入摘要资料。较长资料分片总结，再聚合为简报，不抽样丢弃提交。

GitHub API 对二进制或过大的文件可能不提供文本 diff：程序保留文件名、状态、增删统计，并在通知中明确标注缺少 diff 的文件数量。超过提交文件 API 的 3000 文件上限、历史分叉、API 限流或无法完整取得提交范围时，不声称已生成完整摘要。当前读取公开仓库，不配置长期 GitHub 凭证，受公开 API 频率限制约束。

首次部署没有可比较版本时，仅概括当前 commit 并明确说明；同一版本重部署报告没有新 commit。手动回滚会按相反方向概括被撤销的提交。回滚到尚未包含汇报功能的老镜像时，通知入口不可用，只记录警告。

提交资料仅作为模型输入数据，使用独立于群聊人格的摘要提示词，不执行提交中的指令。摘要阶段最多 180 秒；摘要失败时仍尝试发送版本信息及摘要失败提示。QQ API 返回成功仅代表平台接受消息，不保证群成员已阅读。

## 启用

在服务器 `/opt/fullish/runtime.env` 中设置：

```dotenv
QQ_DEPLOY_REPORT_GROUP_OPENID=<目标群的官方 OpenID>
DEPLOY_REPORT_REPOSITORY=weiuou/Fullish
```

留空 `QQ_DEPLOY_REPORT_GROUP_OPENID` 即禁用。普通 QQ 群号不能直接作为这个配置值。机器人必须已加入目标群、配置 QQ 和 AI 凭证，且群内允许机器人主动发送消息；代码会先查询 `allow_proactive_msg`，关闭时不消耗摘要模型请求，也不冒充对群消息的被动回复。

服务器脚本需要更新一次：上传当前仓库的 `scripts/` 和 `deploy/`，按 [SSH 部署说明](deployment-ssh.md) 重新运行 `server-setup.sh` 安装基础设施，再发布包含汇报功能的镜像。原有运行凭证会保留；此后日常部署自动触发汇报，不需要修改 GitHub Secrets 或扩大受限 SSH 命令权限。

应用容器依然使用现有只读文件系统和网络入口。服务器通过 `docker exec` 中的 CLI 请求容器内部 `/internal/deployment-report`；该接口要求由 QQ 应用密钥派生的专用 HMAC token，并核对当前运行 revision。CLI 会短暂等待 QQ 连接；已经开始汇报或发送失败时不自动重试，以免发生不确定的重复发送。

## 排查

部署输出显示 `Deployment report accepted by QQ` 表示平台已接受。`WARNING: deployment succeeded, but the QQ deployment report failed` 表示部署成功而通知失败，需检查应用的 `deployment.report.*` 日志。日志不输出提交 diff、摘要正文、群 OpenID 或凭证。

若群内主动消息未开启，需要管理员在 QQ 客户端的机器人管理中允许主动发送；本功能不会修改群权限。若需补发，可在确认之前未送达后，通过管理员 SSH 在当前容器中执行 `python -m fullish.release_report --previous <旧提交> --current <当前提交>`。正常部署应继续由既有发布脚本触发。

验证覆盖 [提交和通知测试](../tests/test_release_report.py) 及 [部署事务测试](../tests/deploy_smoke.sh)，使用合成记录、模拟 GitHub / AI / QQ 和 Docker，不向真实群发测试消息。
