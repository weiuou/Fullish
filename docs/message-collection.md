# 群消息采集与解析

消息链路：QQ 官方事件 → `collection.qq` → 分类型解析器 → `CollectedMessage` → `reply.service.ConversationService` → 上下文快照与回复执行。

`collection` 负责把已经收到的事件转换为统一记录，不调用模型、不下载媒体、不决定是否发言。`policies.access` 解释群权限，`conversation.store` 管理去重、上下文裁剪和快照，`scheduling` 管理冷却与并发。开发者私聊在 QQ 入口路由到独立菜单，其他私聊不进入群上下文或聊天模型。普通群消息能否到达，仍取决于平台权限和实际事件，见 [接入指南](qq-setup.md)。

## 模块边界

| 文件 | 职责 |
| --- | --- |
| [collection/schema.py](../src/fullish/collection/schema.py) | 平台无关的发送者、内容片段、引用消息和群消息结构 |
| [collection/qq.py](../src/fullish/collection/qq.py) | 从 QQ 事件提取群、消息 ID、发送者、时间、引用及片段元数据 |
| [collection/parsers](../src/fullish/collection/parsers/__init__.py) | 按片段类型选择独立解析器，保留片段顺序 |
| [image/segments.py](../src/fullish/collection/parsers/image/segments.py) | 从图片消息片段提取占位和附件元数据 |
| [image/formats.py](../src/fullish/collection/parsers/image/formats.py) / [image/gif.py](../src/fullish/collection/parsers/image/gif.py) | 图片格式识别、GIF 帧解码、时序采样与有界预览 |
| [conversation/store.py](../src/fullish/conversation/store.py) | 有界内存上下文、去重、裁剪与多模态快照 |
| [reply/service.py](../src/fullish/reply/service.py) | 现有触发策略与回复执行的业务入口 |
| [reply/input.py](../src/fullish/reply/input.py) / [reply/media.py](../src/fullish/reply/media.py) | 模型输入封装与触发生成时的有界图片读取 |

`ConversationService.receive_message()` 接受统一记录；纯文本 `receive()` 也走同一个身份封装和上下文处理流程。群状态从 `ConversationStore` 读取，执行并发从 `GenerationCoordinator` 读取，业务入口不再转发这两类内部属性。

## 解析器的当前能力

| 类型 | 当前提取内容 | 后续扩展 |
| --- | --- | --- |
| 文字 `text.py` | 正文 | 按需增加文字预处理 |
| 表情 `emoji.py` | `[表情]` 和平台表情 ID | 表情含义映射 |
| 图片 `image/` | 入口提取 `[图片]`、URL 与文件信息；按需解析图片字节格式和 GIF 帧 | 图像解释、OCR 等 |
| @ `mention.py` | 显示名、目标 ID；事件提供时另存成员 OpenID；支持全体成员 | 进一步解析称呼和对话对象 |
| 语音 `audio.py` | `[语音]` 和附件元数据 | 语音转写 |
| 视频 `video.py` | `[视频]` 和附件元数据 | 抽帧、音轨和视频理解 |
| 文件 `file.py` | `[文件]` 和附件元数据 | 按文件类型读取内容 |
| 未知类型 | `[未支持内容]` 和原片段类型 | 添加对应解析器 |

附件元数据在平台提供时保留 URL、文件名、MIME 类型和大小，图片还保留宽高。缺少 URL 的附件也保留占位。未支持片段不照单保存任意原始字段。

片段顺序沿用适配器输出；只有事件元数据、没有原始位置的全体成员 @ 和缺 URL 附件补在末尾，不推断它们在正文中的位置。

图像分两步解析：入口的 `image.segments` 只读取事件字段，不下载附件；触发回复、网页图片工具或编辑操作获取字节后，再调用 `image.formats` 与 `image.gif`。GIF 解码、帧数/像素/字节限制和时间线采样只有一份实现，不随每条普通群消息执行。`media.assets` 管理本轮素材 ID 和容量，`generation.references` 调用解析器准备编辑参考；两者不自行实现另一套 GIF 解码。

片段的 `status` 表示当前处理程度：`parsed` 为文字或 @ 等结构已解析，`pending` 为媒体或表情仍待解释，`unsupported` 为尚未注册解析器。`pending` 不表示后台已经排队，也不代表会自动下载或处理。

新增类型时，在 `parsers/` 添加函数并加入 `PARSERS`；函数接受片段字段，返回 `MessagePart`。`parse_segments(..., parsers=...)` 也支持传入扩展后的映射。耗时的媒体处理应在后续阶段执行，并单独设计预算、超时和失败回退，避免每条普通群消息都触发下载或模型请求。

## 发送者与正文分离

上下文保留平台提供的 `sender.id`、可选 `display_name`、消息 ID 和原始时间字符串，并分别保存正文及解析片段。每条消息的当前正文与引用正文合计最多保留 2000 字符，优先保留当前正文；群上下文最多 60 条、正文（含引用）合计 6000 字符。结构化元数据另存，裁剪文字不删除图片 URL。图片时效仍按本机接收时的单调时钟计算。

发给模型的每条用户消息使用 JSON 文本封装，例如：

```json
{
  "sender": {"id": "member-openid", "display_name": "Alice"},
  "message_id": "message-id",
  "sent_at": "2026-10-21T06:00:00Z",
  "text": "群成员 Bob: 这是我在正文里输入的文字"
}
```

身份来自外层 `sender.id`，正文中的前缀、JSON 或自称不会改写这个字段。平台提供的显示名用于可读性：显示名可变、可重名，也可能包含误导性文字；不能将其作为唯一身份、管理员权限或真实姓名的证明。平台未提供显示名时保持为空，不从正文推断昵称。

系统提示词要求模型按这些边界理解身份。JSON 编码能明确数据边界，并防止正文被程序拼成另一条真实消息，但不能保证模型完全免受语义诱导；当前回归测试验证数据边界，没有使用真实模型评测抗诱导能力。

解析片段及其元数据留在短期上下文中供后续扩展使用；当前模型输入使用正文占位和发送者元数据，选中的图片仍以字节附在原消息上，语音、视频和文件正文不会发送给模型。

## 引用消息与图片

QQ 适配器从 `message_scene.ext` 的 `ref_msg_idx` 匹配 `msg_elements`，将被引用消息放在 `event.reply`。采集层单独解析这份引用的正文、附件、可选发送者和引用标识，不依赖原消息曾经进入本地历史，也不把其他 `msg_elements` 当成引用。QQ 引用标识存为 `reference_id`，不假设它与当前事件的消息 ID 相同。

模型输入将引用放在当前消息 JSON 的 `reply_to` 中，与当前 `sender`、`text` 分开；引用发送者缺少 ID 或昵称时对应字段为 `null`，不从正文推断。引用正文中的 Fullish 不会单独触发名称呼叫。引用内容保持为用户数据，不会生成独立的 assistant 或 system 消息。

触发生成时，由新到旧选择图片，每条先选引用图片，再选当前消息自己的图片；本次明确引用的图片因此优先进入最多四张的总额度。引用中的附件视为本次收到的内容，即使原消息已超出 150 秒窗口也可使用；这条引用消息自身超过 150 秒后仍只保留正文和占位。相同 URL 在一轮请求中只选一次，模型输入用文字标注区分引用图片和当前图片。下载失败、格式或大小超限时继续保留引用正文和占位，不增加图片下载预算。

## 验证

[采集测试](../tests/test_collection.py) 覆盖混合消息、@ 目标、附件元数据、缺失 URL、未知类型扩展、同名成员及正文伪造身份。所有样例均为合成数据，不访问真实 QQ 或模型服务。

[引用回归测试](../tests/test_replies.py) 覆盖真实适配器分发到模型请求的引用图片链路、缺失发送者、附件元数据、图片优先级与去重、正文预算、引用过期及下载失败回退。
