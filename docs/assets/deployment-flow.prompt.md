# Deployment flow image

- Generated with the built-in ImageGen tool.
- Asset: [deployment-flow.png](deployment-flow.png).
- The diagram describes the default SSH route and the documentation-only CI branch.

## Prompt

```text
Use case: infographic-diagram
Asset type: a compact deployment-flow banner embedded in the GitHub README of Fullish, an open-source QQ group chatbot.
Primary request: Create a polished, extremely legible software deployment flowchart as a real raster image. Wide landscape 3:1 composition, approximately 1800 x 600. White background, restrained blue/slate main path, green success and muted orange rollback, flat technical illustration, thin consistent strokes, ample breathing room, clear sans-serif Simplified Chinese text. No decorative characters, no fake product UI, no watermark, no photorealism.
Composition: six evenly spaced rounded rectangular cards in one main horizontal row connected left-to-right with arrows. Label each card EXACTLY as follows, in this order:
1 "合并 main"
2 "测试与构建"
3 "GHCR 镜像"
4 "SSH 部署"
5 "健康检查"
6 "运行最新版"
The arrow from card 5 to 6 is green and labelled "通过".
From card 5, one downward orange arrow labelled "失败" leads to a separate lower card labelled "恢复上一镜像". This is a fallback destination, not another forward deployment step.
A small distinct lower-left branch from card 1 shows a lightweight documentation-only route: one small card "仅文档变更" followed by an arrow to "文档检查" and then "完成". This documentation route must not connect to GHCR, SSH, build, or runtime deployment cards.
Use simple generic line icons above labels (merge, checks, container, server, heartbeat, checkmark), never replace words with icons. Typography must be crisp, accurate and readable when the full image is displayed at 900px width. No extra captions or explanatory paragraphs. The diagram should feel like concise, trustworthy developer documentation. Ensure correct arrow direction and no crossing lines.
```
