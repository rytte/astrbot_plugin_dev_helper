# 开发助手

AstrBot 管理员使用的开发与排障插件，提供命令与模型工具查询、最近日志和当前会话记录，支持日志配色图片和 JSON 高亮图片。

插件标识：`astrbot_plugin_dev_helper`。版本：`0.3.3`。图片命令可使用本地 Chromium 或已安装的 Microsoft Edge。

## 安装依赖

支持 AstrBot `>=4.27.4,<5`。本版基于原版 AstrBot `4.28.0-beta.1`（基线提交 `0c9050084`）开发，直接使用现有插件接口，**无需修改 AstrBot 核心或应用补丁**。

将本目录放到实例的 `data/plugins/astrbot_plugin_dev_helper`，或在 WebUI 上传 `dist/astrbot_plugin_dev_helper.zip`。ZIP 根目录包含插件入口及元数据。配置 AstrBot 管理员后发送 `/inspect` 检查加载结果。

图片渲染使用 Playwright 和 Pygments，依赖声明在 `requirements.txt` 中。请在 **运行 AstrBot 的同一 Python 环境和系统用户下**安装 Python 依赖：

```sh
python -m pip install -r data/plugins/astrbot_plugin_dev_helper/requirements.txt
```

已安装 **Microsoft Edge** 时，在插件配置的「浏览器可执行文件路径」中填写 Edge 的绝对路径并保存即可，**无需安装 Chromium**。例如 Windows 常见路径为 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`；直接填写路径，不加引号。也可填写本地 Chromium 路径。插件使用独立的无头浏览器实例，不使用日常浏览器的登录状态或用户配置。

该配置留空（初始默认值）时使用 Playwright 安装的 Chromium，需要另行安装对应的浏览器：

```sh
python -m playwright install chromium
```

Linux 使用 Chromium 时可执行 `python -m playwright install --with-deps chromium` 安装浏览器及系统依赖，并安装中文字体（Debian/Ubuntu：`apt-get install fonts-noto-cjk`）。Docker 部署需在运行 AstrBot 的容器内安装所选浏览器，无法直接使用宿主机的 Edge；建议写入自定义镜像以便重建后保留。Windows 使用系统中文字体。

插件不会在查询时自动安装、下载或切换浏览器。依赖缺失、所选浏览器启动失败或渲染超时会返回明确错误；文字命令可以独立使用。图片内容只在本机处理，不调用 AstrBot 的远程 HTML 渲染服务，不加载远程脚本、字体或图片。

`v0.2.0` 已删除用户黑名单功能及 `/ban` 命令，不再读取或写入此前的黑名单 KV 数据。升级后原规则不再生效，遗留数据不会自动删除。

`v0.2.2` 将会话记录命令改为 `/chatlog`，移除 `/history`，不保留旧别名。命令转模型工具等配置中的本插件命令标识也需改为 `chatlog`。

## 插件配置

在 WebUI 的插件管理中打开「开发助手」的配置，修改后保存，AstrBot 会重载插件使配置生效。

| 配置项 | 界面名称 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `chatlog_default_count` | 会话记录默认条数 | 10 | `/chatlog`、`/chatlog-pic` 省略条目数时共用 |
| `logs_default_count` | 日志默认条数 | 30 | `/logs`、`/logs-pic` 及各自的 `warning` 子命令共用 |
| `browser_executable` | 浏览器可执行文件路径 | 空字符串 | 留空使用 Playwright 安装的 Chromium，也可由管理员配置本地 Chromium 或 Edge 路径；两个图片命令共用 |

两个条数配置项均为 1–100 的整数。命令中显式指定的条目数优先于配置，例如 `/chatlog 10` 始终查询最近 10 条；记录不足时返回实际可用条目。插件加载时校验配置，收到非法值或未知配置项会明确报错。

升级不会覆盖已保存的配置值；已有配置如需改为 10 条，请在插件配置中修改「会话记录默认条数」。

## 命令

示例中的 `/` 使用 AstrBot 实际配置的唤醒前缀。所有命令仅限 AstrBot 管理员，不使用长选项参数。

| 命令 | 功能 |
| --- | --- |
| `/inspect` | 数量概览与命令帮助 |
| `/inspect commands [页码]` | 已注册的内置和插件命令、指令组、别名、权限、启用状态 |
| `/inspect tools [页码]` | 内置、插件和已连接 MCP 的工具说明、来源与参数 |
| `/inspect plugin <插件标识> [页码]` | 汇总指定插件的命令和模型工具 |
| `/logs [条目数]` | 查看最近日志，默认条数可配置，初始为 30 |
| `/logs warning [条目数]` | 查看 WARNING、ERROR、CRITICAL 日志，与 `/logs` 共用默认条数 |
| `/chatlog [条目数]` | 查看当前会话选中对话最近的已保存记录，默认条数可配置，初始为 10 |
| `/logs-pic [条目数]` | 日志图片，使用 WebUI 的日志等级配色，默认条数与 `/logs` 相同 |
| `/logs-pic warning [条目数]` | WARNING、ERROR、CRITICAL 日志图片 |
| `/chatlog-pic [条目数]` | 当前会话已保存记录的 JSON 高亮图片，默认条数与 `/chatlog` 相同 |

目录每页 20 项，默认第一页，输出给出下一页命令。日志与会话记录的条目数为 1–100；错误参数、多余参数和未知子命令明确拒绝。

```text
/inspect commands 2
/inspect plugin astrbot_plugin_command_tools
/logs warning 20
/chatlog 10
/logs-pic warning 20
/chatlog-pic 5
```

`inspect`、`logs`、`chatlog`、`logs-pic`、`chatlog-pic` 是五个注册的根命令，子命令由各自入口严格解析。命令转模型工具等插件若需要引用本插件，应使用根命令标识，并通过其参数传入子命令。

## 查询结果

命令目录读取已注册的处理器，包含子命令和现有别名，继承父指令组的管理员要求及停用状态。查询不执行命令或自定义过滤器。同名命令分别按所属插件显示。

工具目录读取插件、MCP 和内置工具的注册信息，不调用模型或工具。内置工具中的动态注入项也会列出；实际请求是否携带它们，仍取决于人设、会话及运行时设置。停用插件若没有留下注册信息，会提示信息不可用。未连接的 MCP 服务无法提供实时工具目录。

Agent 委派入口及其嵌套工具会一并列出，子工具注明所属 Agent 入口，并保留自身的插件或 MCP 来源和启用状态。同一工具对象被多个 Agent 引用时只计一项，保留各入口关系；不同对象即使同名也分别展示。工具名称引用会按注册目录解析，无法解析的引用明确提示信息不可用；未指定工具列表的 Agent 标记“按请求决定”。

首版通过 `inspect plugin` 按插件汇总；不提供单独的来源筛选。命令与现有注册名称、别名冲突时，初始化会报错。插件加载后出现的冲突，会在本插件命令执行前复查并提示；其他插件可能已完成过滤或执行，需在插件管理中解决重名问题。

## 日志与会话记录

日志仅限管理员私聊查询。读取 AstrBot 现有 `LogBroker` 缓存；当前基线容量为 500 条，不扫描日志文件，也不读取重启前已丢失的缓存。`logs warning` 先筛选级别，再取最后 N 条，最终按时间正序输出；异常堆栈属于同一条日志。空缓存、无匹配记录及日志源不可用分别提示。

日志输出使用正文自带的时间和级别，不再额外添加时间、级别行。概览信息与日志正文之间空一行；各条日志之间仅换行，不插入空行，正文内部的换行和异常堆栈格式保留。

`chatlog` 在命令所在会话中返回，群内回复对群成员可见。查询遵循 AstrBot 的群成员独立会话设置，验证对话归属，只读取已保存的 `conversation.history`，不会创建、切换或修改对话。一次用户消息和一次助手回复通常计为两条；工具调用、工具结果按实际保存条目计数。第三方 Agent 未保存到本地的内容无法显示。

输出遮蔽可识别的密钥、令牌、密码、认证头和内嵌媒体数据。日志正文和工具结果中的 JSON 会递归脱敏，包括多次序列化、转义后的工具参数；保留普通字段及 JSON 结构，过深的嵌套内容会明确隐藏。复杂自由文本中的秘密无法保证全部自动识别。会话记录中的多媒体使用类型摘要。

`inspect`、`logs` 和 `chatlog` 均不再按单条字符数或回复总字符数截断内容；目录分页和日志、会话记录的条数限制仍然生效。

文字命令回复以纯文本交给 AstrBot 标准回复流程发送，由主程序按配置处理回复前缀、分段和 QQ 合并转发。QQ 的 aiocqhttp 适配器在文本超过 `platform_settings.forward_threshold`（转发消息的字数阈值）时自动合并转发。本插件不再自行按 3000 字符切分发送，仍关闭自动文本转图片和 Markdown。

## 图片输出

`logs-pic` 与 `chatlog-pic` 共用文字版的数据查询、权限、脱敏和条数限制。`logs-pic` 仅限管理员私聊，`chatlog-pic` 在当前会话回复，群内图片对群成员可见。空记录、非法参数等仍用简短文字说明。

日志图片采用与 WebUI 控制台相同的深色背景和等级配色，使用正文已有时间、来源和级别。每条日志首行保持原位，后续行（自动换行及正文中的换行）缩进 26 个空格的宽度，异常堆栈原有缩进在此基础上保留。JSON 图片以数组显示选中的记录，保留角色、工具调用和结果字段，键名、字符串、数字和布尔值分别着色；字符串内部的 JSON 保持其原始字符串类型。

图片宽度为 1200 像素，长行自动换行，每页最多 56 行正文，沿完整显示行分页，不截掉内容。所有页面渲染完成后依次交给 AstrBot 标准回复流程发送，页脚注明页码；图片保存在内存中，不留日志或对话截图文件。单次渲染超过 120 秒会明确报错，不发送不完整的渲染结果。插件串行执行渲染任务，结束后关闭浏览器。

日志布局和配色参照 WebUI；字体取决于部署环境，图片不包含 WebUI 的筛选按钮或交互编辑功能。

## 开发验证

使用原版 AstrBot 源码及其 Python 环境。源码位于相邻 `AstrBot` 目录时，在插件目录执行：

```powershell
../AstrBot/.venv/Scripts/python.exe -m pytest -q
../AstrBot/.venv/Scripts/ruff.exe check .
../AstrBot/.venv/Scripts/ruff.exe format --check .
```

安装 Playwright 的 Chromium 后，设置环境变量 `PICTURE_TESTS=1` 运行测试，可额外验证真实浏览器分页、中文排版及无网络请求；另设 `PICTURE_TEST_EXECUTABLE` 为 Edge 或本地 Chromium 的绝对路径，会一并验证指定浏览器。未启用时仅跳过浏览器集成测试。

源码在其他位置时设置 `ASTRBOT_SOURCE`。测试将运行数据隔离到系统临时目录，使用真实事件、命令过滤器和标准消息流水线；平台发送和会话服务使用隔离替身，不连接聊天平台或付费模型。尚未做真实 QQ 等平台的联调。
