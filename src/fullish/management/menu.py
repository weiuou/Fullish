"""Deterministic developer menu text and feature names."""

NAMES = {"聊天": "chat", "搜索": "search", "生图": "image", "沙箱": "sandbox"}
MENU = (
    "开发者菜单\n群列表：查看群编号\n选择 1：管理指定群\n状态：查看当前群\n"
    "开启 搜索 / 关闭 生图：切换功能\n默认 开启 搜索：设置新群默认功能\n"
    "命名 我的群：给当前群加备注\nMCP：管理动态工具服务器\n菜单：返回这里"
)
MCP_MENU = (
    "MCP 列表\nMCP 添加 名称 https://服务地址/mcp\n"
    "MCP 添加 名称 env:MCP_专用地址变量\nMCP 检查 名称\n"
    "选择群后：MCP 授权 名称 / MCP 撤销 名称\n"
    "MCP 限定 名称 工具1,工具2（* 为全部）\n"
    "MCP 停用 名称 / MCP 启用 名称 / MCP 删除 名称\n"
    "添加后需单独授权群。密钥由服务器环境配置，请勿私聊发送。"
)
