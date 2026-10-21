# QQ 官方机器人与 CPA

官方机器人使用 AppID / AppSecret 换取短期令牌，适配器负责刷新、WebSocket 心跳和断线重连，不需要登录个人 QQ 客户端。平台停服、权限变更或凭证失效仍可能导致离线，不能保证永久在线。

## 先创建测试机器人

1. 打开 [QQ 开放平台](https://q.qq.com/)，按平台要求完成开发者注册并创建机器人。
2. 在“开发管理”取得 AppID 和 AppSecret。如果要求 IP 白名单，填写实际服务器出口 IP；当前服务器为 `43.133.14.74`。
3. 在使用范围 / 测试配置中添加测试人员和测试群，按控制台提示将机器人加入群聊。是否需要审核以上线，以账号控制台为准。
4. 申请群消息能力。要像群友一样读取普通发言，需要平台开放 `GROUP_MESSAGE_CREATE`，并由群主在群内授权机器人获取全部消息。能力以控制台和实际收到的事件为准；没有该权限时先测试 @ 消息，不能据此宣称已接收全群消息。

参考：[官方鉴权说明](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html)、[腾讯 SDK 使用说明](https://github.com/tencent-connect/qqbot-nodejs/blob/main/USAGE.md)、[NoneBot QQ 适配器](https://github.com/nonebot/adapter-qq)。

## 推荐：在 GitHub 配置

进入仓库 Settings → Environments → production → Environment secrets，添加 `QQ_APP_ID` 和 `QQ_APP_SECRET`。两项都设置时，SSH 部署会同步凭证并启用 QQ；缺少其中一项会在连接服务器前报错，两项均未设置则保留服务器配置。

随后在 Actions 手动运行 `CI and delivery`，勾选 `deploy`，或合并一次代码修改。仅添加 / 修改 Secret 不会自动触发 Actions；纯文档提交仍不会构建、发布或部署。

凭证只提供给受保护的 `main` 部署 job，通过加密 SSH 的 stdin 发送，不写命令参数、镜像、构建产物或日志。服务器在部署锁内写入权限 `0600` 的 `/opt/fullish/runtime.env`，保留已有 CPA 配置；部署失败时恢复先前配置再回滚容器。临时传输文件在部署结束后删除。

此凭证同步目前仅支持默认 SSH 路线；TAT 部署需在服务器手动配置。QQ 应用密钥失效或平台权限不足时，进程健康不等于 QQ 登录成功，应检查 `qq_connected`。

## 可选：直接配置服务器

编辑服务器 `/opt/fullish/runtime.env`，保留已有 AI 配置，添加：

```dotenv
QQ_ENABLED=true
QQ_APP_ID=你的AppID
QQ_APP_SECRET=你的AppSecret
# 可选：逗号分隔的群 OpenID，空值允许机器人已加入的群。
QQ_ALLOWED_GROUPS=
```

群 OpenID 由官方事件提供，不是普通 QQ 群号。AppSecret 只写服务器配置，不发送到公开聊天、GitHub 或工作流日志。权限保持 `0600`。使用 `sudoedit /opt/fullish/runtime.env` 后，在服务器执行：

```sh
sudo bash -c 'set -a; . /opt/fullish/state/release.env; set +a; FULLISH_IMAGE="$CURRENT_IMAGE" docker compose -f /opt/fullish/compose.yaml up -d --wait'
curl -fsS http://127.0.0.1:18080/health/dependencies
```

这一步重新创建当前镜像的容器以读取新环境变量，不需要重新构建或发布 Package。当前采用向外建立 WebSocket 的方式，无需开放新的公网端口或配置 Webhook。

验收：`qq_connected=true`，群内 @ 或提到 `Fullish` 能收到回复；再确认普通群消息事件已到达。私聊不参与群上下文或聊天模型。启用动态群管理后，C2C 私聊由独立的开发者菜单处理，只有完成一次性绑定的开发者可修改权限；频道私信继续丢弃。用法见 [私聊开发者菜单](group-control.md)。

## CPA 模型配置

```dotenv
AI_BASE_URL=http://localhost/v1
AI_API_KEY=服务器上的CPA客户端密钥
AI_MODEL=gemini-3.8-flash-high
AI_FALLBACK_MODEL=gpt-6-luna
AI_UNIX_SOCKET=/run/fullish-cpa/api.sock
# 生图复用同一 CPA 密钥；auto 为默认值，也可指定实际图片模型 ID 或 off。
AI_IMAGE_MODEL=gpt-image-2.5
AI_IMAGE_TIMEOUT_SECONDS=120
```

模型名来自当前 CPA 的 `/v1/models`。该部署默认使用 CPA 提供的 `gemini-3.8-flash-high`，文字请求报错、超时、空内容或响应格式错误时，在同一轮切换一次到 `gpt-6-luna`。后续请求继续使用备用模型，下一轮重新尝试主模型；设 `AI_FALLBACK_MODEL=` 可关闭回退。切换保留上下文、工具结果和剩余额度，不重跑已执行工具，并去掉 Gemini 专用的思考签名。主请求预留备用请求时间，整轮仍受原有总超时限制；工具耗尽后的最终答复也支持回退，两者都失败则使用原有未完成提示。结构化日志通过 `model.fallback.started` 记录原因和目标模型，各请求记录实际模型。若 CPA 更换提供商或名称，需重新核对列表。

同机 CPA 只监听宿主机 `127.0.0.1:8317`，容器自己的 `127.0.0.1` 无法访问它。这里用 systemd 的 Unix socket 转发，通过只读目录挂载提供给 HTTPX；保持 CPA 的监听范围，无需公网绕行。

已有 Docker 部署上安装这项配套设施：

```sh
sudo install -d -m 755 /opt/fullish/cpa
sudo install -m 644 deploy/fullish-cpa.socket deploy/fullish-cpa.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fullish-cpa.socket
# 按 SSH 部署指南更新 Compose；CI 只更新应用镜像，不安装这些主机文件。
sudo bash scripts/server-setup.sh ghcr.io/weiuou/fullish
```

这些单元适用于具有 `/usr/lib/systemd/systemd-socket-proxyd` 的 Ubuntu / Debian，转发进程使用 systemd 临时用户。socket 在容器内为 `/run/fullish-cpa/api.sock`，CPA 仍校验 Bearer 密钥。远程模型服务改用 HTTPS 的 `AI_BASE_URL` 并移除 `AI_UNIX_SOCKET` 即可。

使用一次真实生成验证模型（会消耗少量额度）：

```sh
sudo docker exec fullish-bot-1 python -m fullish.check_model
```

`/health/dependencies` 只查询模型目录和 QQ 连接状态，不生成文本。Docker readiness 只检查进程已启动，避免 QQ / 模型短暂故障触发错误回滚。`ai_configured` 表示 AI 配置已加载；是否真正可用仍需查看依赖检查和实际生成结果。

## 当前聊天策略

每群保留最近 60 条、总计不超过 6000 字的消息（引用正文、文字、`[表情]`、`[图片]` 都计入），上下文空闲 30 分钟后在下一次消息处理时清理；最多 50 个群，部署或重启会清空。普通群消息用于上下文，@ 或当前正文提到 Fullish 才触发生成；暂不随机插话。消息进入按群隔离的 FIFO 队列，同群完整处理“生成 → 发送 → 发送确认后记录上下文”后才消费下一条，避免下一条 @ 被忙碌规则丢弃或抢在上一条发送前生成。不同群并行，全局模型生成最多四个；并发名额不足或距离该群上次生成不足五秒时等待，不直接跳过。每群队列最多暂存 100 条，满时入口等待空间；普通消息同样按顺序进入上下文。队列位于进程内，停机最多等待 20 秒排空，超时会取消并记录尚未处理的消息；重启不恢复待处理消息，严重积压还可能超过 QQ 的回复有效期。

消息采集由独立的 `collection` 模块处理，各类内容有单独解析器。语音、视频和文件先保留 `[语音]`、`[视频]`、`[文件]` 占位及附件元数据，不读取内容；表情 ID 和 @ 目标也会保留。发送者使用平台成员 OpenID，平台有 `username` 时同时保存显示名，正文不能改写发送者字段。模型输入使用 JSON 分开封装身份与正文；上述 6000 字限制针对正文，元数据另存。扩展入口及限制见 [群消息采集与解析](message-collection.md)。

只 @ 机器人、不附带文字也是有效呼叫。适配器去掉 @ 后，应用用 `（@了 Fullish）` 表达这次呼叫并正常请求模型；无文字的其他消息也保留上下文，不以空文本为由丢弃。

纯图片、表情的消息也会进入上下文，不再被丢弃。真正要回复时，只把最近两分半钟内最多四张图的字节（单张上限 2 MB，仅 JPEG / PNG / GIF / WebP，按文件头校验）以 data URL 附在各自用户消息上，保留发送者和消息顺序。引用图片后 @ 机器人时，引用的正文、发送者及图片单独保留，本次引用图片优先占用这四张额度；原消息不必已在历史中，引用附件从本次收到时重新计算时效。同一 URL 每轮只下载一次，引用图片与当前图片分别标注来源。更早、未选中或下载失败的图片保留 `[图片]` 占位，模型不会猜测内容。图片由独立的 HTTP 客户端下载，不带模型密钥；下载和模型调用共用本次回复的总超时，单图下载仍最多 15 秒。无法识别文件头的内容会跳过，即使响应头声称是图片。如果 CPA 不支持多模态输入，请求会失败并只记录错误码，需要改用文字描述方案。

回复默认为一到三句，避免客服套话，保留选择沉默的能力。长期记忆、主动插话评测和过时话题判断仍是后续工作。

发送出口支持独立的图片、语音适配器，可以发送已有 URL、字节或本地文件，图片可以与文字同条发送。GPT Image 生图复用现有 CPA 地址、密钥和 socket，默认使用 `gpt-image-2.5`；进入回复流程后，聊天模型可根据语境自主选择生图表达反应、想法或说明，不要求群友先发画图命令。配文与真实生成图片同条发送，也支持只发图片。发言触发仍为 @ / 当前正文提到 Fullish，频率限制沿用现有策略。接口要求、验收命令及 QQ 的单附件限制见 [图片、语音与图文发送](message-sending.md)。

## 追踪消息流转

应用在 INFO 级别输出 `flow {JSON}` 结构化日志。同一条群消息从官方事件接收、解析分发、内容解析、上下文记录、触发决策、图片下载、模型请求与响应，到 QQ 发送和最终处理结果，使用同一个 `trace_id`。`group_ref` 和 `sender_ref` 是原始标识的摘要，便于关联而不直接输出 OpenID。启动、停止、QQ 连接与断开也有对应事件。

`conversation.generating.context_trace_ids` 按模型输入顺序列出历史消息来源；图片日志里的 `source_trace_id` 指向图片所属的历史消息，当前 `trace_id` 指向本次请求。发送成功后才记录 assistant 上下文。`qq.message.completed` 是处理结束记录，结果为 `sent`、`no_reply`、`failed` 或 `cancelled`；发送成功表示 QQ API 接受请求，不代表客户端已阅读。

不生成时，`conversation.skipped.reason` 明确记录 `context_only`（仅上下文）、`duplicate`、`group_not_allowed` 或 `group_capacity`。`conversation.waiting.reason` 记录 `group_busy`、`global_capacity` 或 `cooldown` 的等待；`queue.enqueued` / `queue.dequeued` 记录入队、队列深度和等待时间，停机未排空记录 `queue.cancelled`。模型选择沉默记录为 `model.completed.outcome=silence`。图片失败保留占位，并记录 HTTP 状态、格式不符、超限或连接问题；模型和 QQ 发送失败记录安全错误码、所在阶段及耗时，不输出上游响应正文、密钥、图片 URL、图片字节或聊天正文。

在服务器查看最近的结构化日志（需安装 `jq`）：

```sh
sudo docker logs --since 30m fullish-bot-1 2>&1 | sed -n 's/^.*flow //p' | jq -c .
# 把上一步找到的 trace_id 填入，查看一条消息的完整过程。
sudo docker logs --since 30m fullish-bot-1 2>&1 | sed -n 's/^.*flow //p' | jq -c 'select(.trace_id == "填入trace_id")'
```

保持 `LOG_LEVEL=INFO` 才能看到正常路径。日志由现有 Docker 日志轮转保留（单文件 10 MB、最多 3 个），容器被替换后旧容器日志不再保留；需要长期审计时应由宿主机采集到持久日志服务。未出现 `qq.event.received` 表示应用未收到该事件，应继续排查 QQ 连接和平台权限。
