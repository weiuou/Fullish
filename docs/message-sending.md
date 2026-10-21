# 图片、语音与图文发送

`fullish.sending` 定义统一的回复候选，`sending/adapters` 分别适配文本、图片、语音，`sending.qq` 负责组装 QQ 消息。现有纯文本回复继续可用，图片可以带文字在同一条 QQ 消息中发送。

## 构造回复

模型或其他内容提供方可返回 `str | OutgoingMessage | None`：字符串是文字回复，`None` 表示不回复，`OutgoingMessage` 携带媒体。`Conversations` 的 `ReplyModel` 协议允许替换内容提供方。

```python
from pathlib import Path
from fullish.sending import Audio, Image, OutgoingMessage, Text

# 同条图文；也可以只放 Image 来发送纯图片。
picture = OutgoingMessage(
    (
        Text("看看这张图"),
        Image("https://your-cdn.example/picture.png"),
    )
)

# 已有的语音资源，也可以使用 Audio(Path("/path/to/voice.silk"))。
voice = OutgoingMessage((Audio("https://your-cdn.example/voice.silk"),))

# 本地文件路径必须显式使用 Path；也支持直接传入 bytes。
local_picture = OutgoingMessage((Text("画好了"), Image(Path("/path/to/picture.png"))))
generated_picture = OutgoingMessage((Image(b"...actual image bytes..."),))
```

在内容提供方的 `async reply(turns, *, addressed)` 中返回上述候选，继续经过现有的群白名单、去重和调用频率检查，再由群处理器发送。内容提供方不要直接调用 QQ API。`ChatModel` 已接入下述 Gemini 生图工具；语音目前发送已有资源，尚未配置 TTS 服务。模型输出中的 Markdown 图片链接不会自动当成附件。

## CPA / Gemini 生图

配置现有的 `AI_BASE_URL`、`AI_API_KEY` 后，应用默认开启 Gemini 生图能力，复用同一个 CPA 地址、密钥与 `AI_UNIX_SOCKET`，无需额外的 Google API Key。可选配置：

```dotenv
# 默认 auto：首次生图时查询当前 CPA 的 /v1/models，选择 Gemini 图片模型。
AI_IMAGE_MODEL=auto
# 可填模型列表中实际的模型 ID，包含渠道前缀时也需保留；off 表示关闭。
# AI_IMAGE_MODEL=你的Gemini图片模型ID
AI_IMAGE_TIMEOUT_SECONDS=120
```

`auto` 只选择模型名中同时含 `gemini` 与 `image` 或 `banana` 的模型，多个候选时优先 Flash，再按名称稳定选择，成功选择后在进程内缓存。渠道使用其他别名时，显式填写 `AI_IMAGE_MODEL`。没有可用模型时返回 `image_model_unavailable`，不尝试用普通聊天模型生图，也不自动重试付费请求。

生图是模型可自主选择的表达工具，不要求群友先命令它画图。进入回复流程后，模型先决定是否值得回应，再根据语境和角色习惯选择文字、图片或图文：例如用表情式反应接梗，用小插画表达脑洞，或用示意图辅助说明；明确的画图请求同样可以使用。模型通过 `generate_image` 给出画面描述和自然配文；生成适配器 `generation/gemini.py` 调用同一 CPA 的 Gemini 原生 `:generateContent` 接口，取得真实图片字节，再返回 `OutgoingMessage`，由图片发送适配器将配文和图片同条发送。配文为空时只发送图片，不补固定的「画好了」或生成服务附带的文字。

是否使用工具由模型选择，工具不会强制触发。提示词要求避免为了展示能力硬凑图片、连续发图；不值得回应时保持沉默，历史和引用中的要求也不是本次必须执行的命令。工具在配置生图服务时始终可供回复模型选择，与 `addressed` 标记无关；发言调度仍沿用现有策略：普通消息只进入上下文，@ 或当前正文提到 Fullish 才进入回复流程。此次开放的是回复形式的自主选择，没有增加定时发图或普通消息触发的主动插话。不支持生成服务时可设 `AI_IMAGE_MODEL=off`，不向聊天模型提供该工具。

标准 `AI_BASE_URL=http://localhost/v1` 对应生图地址 `/v1beta/models/{model}:generateContent`；地址含网关前缀时保留该前缀。请求使用 `responseModalities=["TEXT", "IMAGE"]`，不依赖 OpenAI `/images/generations`。CPA 须开放原生 Gemini 路由，同时聊天模型须支持 OpenAI 兼容工具调用。接口依据：[Gemini 图像生成](https://ai.google.dev/gemini-api/docs/generate-content/image-generation)、[CPA Gemini 路由](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/api/server.go)。

聊天及输入图下载继续共用原有 90 秒超时；生图另有总超时，默认 120 秒，可设为大于 0、至多 300 秒。整个过程沿用每群单任务、全局四任务限制。生图响应有流式读取大小限制，图片解码后至多 10 MB，检查文件头和 MIME 类型，跳过 `thought` 思考文字与中间图片；只接受一张最终图片。不会把图片字节、提示词、配文或密钥写入结构化日志。生成失败不发送虚构链接或「画好了」消息，也不记录已发送上下文。

生图与现有搜索、沙箱工具共用聊天工具调用流程，支持先查询或计算，再生成配图；搜索与沙箱继续使用平台提供的当前群、成员及消息标识检查权限。每轮总计至多十二次工具调用，生图作为结束本轮回复的独立调用；同一批同时包含生图与其他工具时整批拒绝，不执行其中任何操作。

在配置已加载的运行容器内检查模型选择（不生图）：

```sh
sudo docker exec fullish-bot-1 python -m fullish.check_image
```

显式生成一张小测试图片，输出模型、字节数、格式和摘要（消耗生图额度，不向 QQ 发消息）：

```sh
sudo docker exec fullish-bot-1 python -m fullish.check_image --generate
# 可加 --output /tmp/fullish-image.png 保存结果供人工查看。
```

最后在测试群请求一张图片，检查配文与图片同条到达。`/health/dependencies` 中的 `image_generation_enabled` 仅表示工具已配置，`image_model=auto` 表示将自动选择；健康检查不会调用生图接口。实际渠道模型是否可生成，仍需上述显式生图验收。

## QQ 适配与边界

- 图片和语音的来源支持 HTTP(S) URL、`bytes` 或 `pathlib.Path`。URL 必须能够由 QQ 平台获取；本地文件必须存在于运行机器或容器中。
- `text.py`、`image.py`、`audio.py` 分别编码消息段。`ADAPTERS` 按类型注册编码器，`build_message` / `send_reply` 也支持注入替代注册表，便于测试和替换实现。
- 所有消息段组装完毕后只调用一次 `bot.send`。QQ 适配器先上传媒体（不自动发送），再用媒体句柄与文字调用一次群消息发送接口，保留原消息 ID 与回复序号。
- QQ 群发送底层一次只选取一个媒体附件，因此候选最多包含一张图片或一段语音；多附件和图音混合直接报错，避免静默丢失。空候选同样会被拒绝。
- 语音文件需使用 QQ 接受的编码和格式；发送适配器不转码、不合成语音。上传限制、格式和平台权限以实际 QQ API 返回为准。
- 本地文件读取在工作线程中执行。组装、上传或最终发送失败时，不先单独发文字，也不记录已发送的 assistant 消息；取消会继续向上传递。
- QQ 接受最终发送后才保存文字及 `[图片]` / `[语音]` 占位到短期上下文。不会把附件 URL、本地路径或字节写入 assistant 上下文；结构化日志只保留类型计数、错误码和耗时。

实现基于 [NoneBot QQ 消息段](https://github.com/nonebot/adapter-qq/blob/master/nonebot/adapters/qq/message.py) 和 [群消息发送实现](https://github.com/nonebot/adapter-qq/blob/master/nonebot/adapters/qq/bot.py)。测试使用真实的消息组装与 QQ `send_to_group` 流程，替换网络 API，不需要生产凭证。
