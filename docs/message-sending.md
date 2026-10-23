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

在内容提供方的 `async reply(turns, *, addressed)` 中返回上述候选，继续经过现有的群白名单、去重和调用频率检查，再由群处理器发送。内容提供方不要直接调用 QQ API。`ChatModel` 已接入下述 GPT Image 生图工具；语音目前发送已有资源，尚未配置 TTS 服务。模型输出中的 Markdown 图片链接不会自动当成附件。

## CPA / GPT Image 生图

配置现有的 `AI_BASE_URL`、`AI_API_KEY` 后，应用默认使用 CPA 的 `gpt-image-2.5` 生图，复用同一个地址、密钥与 `AI_UNIX_SOCKET`。可选配置：

```dotenv
AI_IMAGE_MODEL=gpt-image-2.5
# 可填 CPA 支持的图片模型别名；off 表示关闭。
AI_IMAGE_TIMEOUT_SECONDS=120
```

旧配置 `auto` 也解析为 `gpt-image-2.5`，不再发现或调用 Gemini 生图模型。模型 ID 按 CPA 提供的名称原样发送；此部署已确认目录中有 `gpt-image-2.5`。生成失败不自动重试付费请求。

生图是模型可自主选择的表达工具，不要求群友先命令它画图。进入回复流程后，模型先决定是否值得回应，再根据语境和角色习惯选择文字、图片或图文：例如用表情式反应接梗，用小插画表达脑洞，或用示意图辅助说明；明确的画图请求同样可以使用。模型通过 `generate_image` 给出画面描述和自然配文；生成适配器 `generation/openai.py` 调用同一 CPA 的 `/v1/images/generations` 接口，取得真实图片字节，再返回 `OutgoingMessage`，由图片发送适配器将配文和图片同条发送。配文为空时只发送图片，不补固定的「画好了」或生成服务附带的文字。

是否使用工具由模型选择，工具不会强制触发。提示词要求避免为了展示能力硬凑图片、连续发图；不值得回应时保持沉默，历史和引用中的要求也不是本次必须执行的命令。工具在配置生图服务时始终可供回复模型选择，与 `addressed` 标记无关；发言调度仍沿用现有策略：普通消息只进入上下文，@ 或当前正文提到 Fullish 才进入回复流程。此次开放的是回复形式的自主选择，没有增加定时发图或普通消息触发的主动插话。不支持生成服务时可设 `AI_IMAGE_MODEL=off`，不向聊天模型提供该工具。

标准 `AI_BASE_URL=http://localhost/v1` 对应生图地址 `/v1/images/generations`，地址含网关前缀时保留该前缀。请求指定 `model`、`prompt`、`n=1`，读取 `data[0].b64_json`，不抓取响应中的任意外部 URL。接口依据：[OpenAI 图片生成接口](https://developers.openai.com/api/reference/resources/images/methods/generate)。具体模型别名和兼容性以 CPA 为准。

聊天及输入图下载继续共用原有 90 秒超时；生图另有总超时，默认 120 秒，可设为大于 0、至多 300 秒。整个过程沿用每群单任务、全局四任务限制。生图响应有流式读取大小限制，图片解码后至多 10 MB，检查文件头；只接受一张图片。不会把图片字节、提示词、配文或密钥写入结构化日志。生成失败不发送虚构链接或「画好了」消息，也不记录已发送上下文。

生图与现有搜索、沙箱工具共用聊天工具调用流程，支持先查询或计算，再生成配图；搜索与沙箱继续使用平台提供的当前群、成员及消息标识检查权限。每轮总计至多十二次工具调用，生图作为结束本轮回复的独立调用；同一批同时包含生图与其他工具时整批拒绝，不执行其中任何操作。

`generate_image` 额外支持 `reference_image_ids`：最多四个本轮已查看图片的 ID，来源可以是群附件、引用、`web_image` 或 `sandbox_download`。有参考图时，适配器改用 `/v1/images/edits`，以 multipart 的 `image[]` 字段上传实际图片字节，携带原有 `model`、`prompt`、`n=1`；继续复用 CPA 地址、密钥、Unix socket 和总超时。没有参考图时仍使用原有文生图接口。参考图支持 PNG / JPEG / WebP / GIF。GIF 在工作线程中按完整时间轴抽取最多 12 帧，始终包含首尾；不足 12 帧时全部保留，长时间停留的同一帧不占用多个采样位置。动态帧正确合成后，按从左到右、从上到下排列并标注原帧号和时间，聊天视觉输入和编辑上传使用同一张 PNG 动作序列图；生成提示词结合整个过程描述所需单张表情，避免照抄多格排版。静态 GIF 直接转为保留透明度的 PNG，原始 GIF 资产仍可发送或导入沙箱。解码限制为单帧 1600 万像素、512 帧、单遍累计 6400 万像素，序列图最多 3 MiB；转换前后单张最多 10 MiB、合计 20 MiB，预览也计入本轮图片内存配额。接口依据：[OpenAI 图片参考与编辑](https://developers.openai.com/api/docs/guides/image-generation#edit-images)。

搜索命中、图片链接或文字描述不能代替真实参考图；提示词要求先看图再填 ID。未知、过期或其他轮次的 ID 会被拒绝。编辑接口失败不会自动重试，也不会退回文生图后声称保留了原形象。CPA 模型目录中存在图片模型并不能证明该渠道支持编辑，需要在实际部署渠道显式验收。

`send_image(image_id, caption)` 可直接发送本轮已有图片，包括沙箱处理的结果，继续经过相同的 QQ 统一发送出口和群图片权限检查；无需启用图片生成服务。此工具同样独立结束本轮，不允许与其他工具混批。获取参考图与沙箱传输见 [基础工具](tools.md)。

已获准群内的 @ / 提到 Fullish 请求，在文字生成或生图异常时发送简短文字提示；生图限流明确说明图片未生成，不发送原先拟好的成功配文。主备文字模型都失败也会说明未完成。日志使用 `qq.reply.fallback` 和 `qq.message.completed` 的 `fallback_sent` 区分失败提示与正常回答。取消继续向上传递；QQ 拒绝发送时不伪记发送成功。普通上下文消息、未获准群和模型主动保持沉默不会因此收到额外提示。

在配置已加载的运行容器内查看配置的图片模型（不生图，也不验证模型可用性）：

```sh
sudo docker exec fullish-bot-1 python -m fullish.check_image
```

显式生成一张小测试图片，输出模型、字节数、格式和摘要（消耗生图额度，不向 QQ 发消息）：

```sh
sudo docker exec fullish-bot-1 python -m fullish.check_image --generate
# 可加 --output /tmp/fullish-image.png 保存结果供人工查看。
# 用容器内已有参考图验证 CPA 图片编辑（会消耗一次图片额度，不向 QQ 发消息）。
sudo docker exec fullish-bot-1 python -m fullish.check_image --generate --reference /tmp/reference.png --output /tmp/edited.png
```

最后在测试群请求一张图片，检查配文与图片同条到达。`/health/dependencies` 中的 `image_generation_enabled` 仅表示工具已配置，`image_model` 表示配置的图片模型，旧值 `auto` 等价于 `gpt-image-2.5`；健康检查不会调用生图接口。实际渠道模型是否可生成，仍需上述显式生图验收。

## QQ 适配与边界

- 图片和语音的来源支持 HTTP(S) URL、`bytes` 或 `pathlib.Path`。URL 必须能够由 QQ 平台获取；本地文件必须存在于运行机器或容器中。
- `text.py`、`image.py`、`audio.py` 分别编码消息段。`ADAPTERS` 按类型注册编码器，`build_message` / `send_reply` 也支持注入替代注册表，便于测试和替换实现。
- 所有消息段组装完毕后只调用一次 `bot.send`。QQ 适配器先上传媒体（不自动发送），再用媒体句柄与文字调用一次群消息发送接口，保留原消息 ID 与回复序号。
- QQ 群发送底层一次只选取一个媒体附件，因此候选最多包含一张图片或一段语音；多附件和图音混合直接报错，避免静默丢失。空候选同样会被拒绝。
- 语音文件需使用 QQ 接受的编码和格式；发送适配器不转码、不合成语音。上传限制、格式和平台权限以实际 QQ API 返回为准。
- 本地文件读取在工作线程中执行。组装、上传或最终发送失败时，不先单独发文字，也不记录已发送的 assistant 消息；取消会继续向上传递。
- QQ 接受最终发送后才保存文字及 `[图片]` / `[语音]` 占位到短期上下文。不会把附件 URL、本地路径或字节写入 assistant 上下文；结构化日志只保留类型计数、错误码和耗时。

实现基于 [NoneBot QQ 消息段](https://github.com/nonebot/adapter-qq/blob/master/nonebot/adapters/qq/message.py) 和 [群消息发送实现](https://github.com/nonebot/adapter-qq/blob/master/nonebot/adapters/qq/bot.py)。测试使用真实的消息组装与 QQ `send_to_group` 流程，替换网络 API，不需要生产凭证。
