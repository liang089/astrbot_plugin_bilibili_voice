# astrbot_plugin_bilibili_voice

B 站视频搜索 / 下载插件：在 QQ 里搜索 B 站视频，把音频转成语音发送；也可以在 AstrBot WebUI 中搜索后直接把音频文件下载到本地。

> 本插件最初由 DeepSeek 辅助编写。v2.1.0 修复了「只能搜索、不能下载」的问题，并重写了发送链路；v2.1.1 修复 B 站 CDN 的 HTTP 567 拦截。

## 功能

- B 站扫码登录（建议使用小号，避免账号风险）
- 关键词搜索、BV 号查询、视频链接 / 分 P 下载
- 视频音频提取，优先转换为 WAV 并以 QQ 语音发送
- 语音发送失败时自动降级为音频文件发送
- WebUI 中搜索后可直接下载音频文件（浏览器保存）
- 支持配置下载目录、时长限制、搜索数量、ffmpeg 路径和发送方式

## 环境要求

- AstrBot **>= 4.26.0**（`astrbot.api.web` 的 `file_response` 和插件页下载桥接从该版本开始提供）
- Python 依赖见 `requirements.txt`
- 推荐安装 **ffmpeg** 并加入 `PATH`：
  - 官方 Docker 镜像已内置 ffmpeg
  - Windows / 源码部署请从 <https://ffmpeg.org/download.html> 下载，解压后把 `ffmpeg.exe` 所在目录加入 `PATH`
  - 也可以在插件配置项 `ffmpeg_path` 中填写 `ffmpeg.exe` 的路径或所在目录
  - 没有 ffmpeg 时插件仍可下载原始音频（m4a）并以文件形式发送，但无法转换为 QQ 语音

## 安装

1. 在 AstrBot WebUI 的「插件」页面从插件市场安装，或把本仓库放进 `data/plugins/astrbot_plugin_bilibili_voice`。
2. 安装依赖：AstrBot 会根据 `requirements.txt` 自动安装；也可以手动执行：

   ```bash
   pip install -r requirements.txt
   ```

3. 重启或在插件页重载插件。

## 使用

### 1. 登录 B 站

- 在插件 WebUI 点击「获取二维码」，用哔哩哔哩 APP 扫码；
- 或在聊天中发送 `/bili_login`，机器人会发送二维码图片；
- 扫码确认后发送 `/bili_status` 检查登录状态。

> 强烈建议使用没有重要数据的小号登录。扫码登录会让插件保存该账号的 Cookie，存在被风控或封号的风险。

### 2. 聊天指令

| 指令 | 说明 |
| --- | --- |
| `/bili_login` | 获取扫码登录二维码 |
| `/bili_refresh` | 刷新二维码 |
| `/bili_status` | 查看登录状态 |
| `/bili_search 关键词` | 搜索 B 站视频 |
| `/bili_down 编号` | 下载搜索结果，例如 `/bili_down 1` |
| `/bili_down BV号` | 下载指定视频，例如 `/bili_down BV1QK4y1C7tD` |
| `/bili_down 链接` | 下载视频或分 P，支持 `?p=2` 形式 |
| `/bili_help` | 查看帮助 |

下载完成后会优先发送 QQ 语音；如果语音发送失败，会自动改为发送音频文件。

### 3. WebUI 下载

在插件可视化页面中：

1. 搜索视频；
2. 点击结果右侧的「下载音频」；
3. 插件会在服务器完成提取和转码，然后由浏览器直接保存音频文件。

「下载音频」会真正触发浏览器下载，而不是只返回服务器路径。

## 配置

配置文件：`data/config/astrbot_plugin_bilibili_voice_config.json`

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `download_dir` | string | `data/bilibili_voice` | 音频缓存目录，相对 AstrBot 工作目录或绝对路径 |
| `max_duration_seconds` | int | `1800` | 允许下载的最长视频秒数，`0` 表示不限制 |
| `search_limit` | int | `5` | 搜索结果数量（1-20） |
| `ffmpeg_path` | string | 空 | ffmpeg 可执行文件或所在目录；留空自动从 PATH 查找 |
| `proxy` | string | 空 | 下载时使用的代理，例如 `http://127.0.0.1:7890`；留空直连 |
| `send_as_voice` | bool | `true` | 优先以语音发送，失败时降级为文件 |

## 常见问题

### 只能搜索，不能下载？

v2.1.0 / v2.1.1 已修复以下问题：

1. **WebUI 下载没有真正触发浏览器保存**：旧版 `/download` 只返回服务器文件路径。现在新增 `/download/file` 文件接口，并使用 AstrBot 插件页的 `bridge.download()` 触发浏览器下载。
2. **下载结果路径靠猜**：旧版用 `prepare_filename()` + 扫描最新 WAV 来定位输出文件，分 P / 播放列表场景容易拿错文件或报错。现在优先读取 yt-dlp 的 `requested_downloads`，只接受本次下载生成的文件。
3. **发送方式不符合 AstrBot 规范**：旧版直接调用 OneBot `call_action` 发送 `base64://` / `file:///`，跨 Docker 或 NapCat 部署时经常失败。现在统一使用 `Record.fromFileSystem()` / `File` 组件，由 AstrBot 适配器处理转换和上传。

如果仍然失败，请按顺序检查：

- **ffmpeg 是否存在**：在 AstrBot 容器/主机执行 `ffmpeg -version`；不存在时安装，或填写 `ffmpeg_path`。
- **是否登录**：发送 `/bili_status`；出现 403 / 412 时先 `/bili_login` 扫码登录。
- **HTTP 567 / `Restricted Access`**：这是 B 站腾讯 EdgeOne CDN 对服务器 IP 的拦截，不是插件逻辑错误。v2.1.1 已自动加 `Range: bytes=0-` 头；若仍出现，请在配置中填写可用的 `proxy`，或换一台网络环境不同的服务器。
- **视频是否超过时长限制**：默认 1800 秒，可在配置中调大或设为 `0`。
- **磁盘空间与目录权限**：确认 `download_dir` 可写。
- **AstrBot 版本**：低于 4.26.0 不支持文件下载接口，请升级。

### 语音发送失败，只收到文件？

QQ / NapCat 对语音格式、时长和大小有限制。音频超过 20 MiB 时插件会直接按文件发送；语音发送失败时也会自动改为音频文件。也可以把 `send_as_voice` 设为 `false` 直接发文件。长视频生成的 WAV 文件会比较大，建议把 `max_duration_seconds` 调小。

### 二维码加载不出来？

- 聊天中 `/bili_login` 使用本地 `qrcode` 库生成二维码，不依赖外部图片服务；
- WebUI 同样优先使用后端生成的二维码图片；
- 如果本地二维码生成失败，会回退到 `api.qrserver.com`，该域名在部分网络下不可用。

## 更新日志

### v2.1.1

- 修复 B 站 CDN（腾讯 EdgeOne）返回 `HTTP 567 / Restricted Access` 导致无法下载：下载请求自动附加 `Range: bytes=0-`
- 新增 `proxy` 配置，便于服务器 IP 被 CDN 限制时走代理
- 补充 567 的明确错误提示

### v2.1.0

- 修复 WebUI「下载音频」只显示服务器路径、不触发浏览器下载的问题
- 新增 `/download/file` 文件下载接口和一次性 token
- 重写下载结果定位逻辑，支持分 P / 播放列表，避免误取旧文件
- 下载时补充 B 站 Referer、匿名 Cookie、重试和超时设置
- 下载请求附加 `Range: bytes=0-`，绕过 B 站 CDN 的 567 拦截；新增 `proxy` 配置
- 无 ffmpeg 时自动降级下载原始音频，不再直接失败
- 发送链路改用 AstrBot `Record` / `File` 组件，修复跨容器发送失败
- 新增本地二维码生成、`ffmpeg_path` 配置和更清晰的错误提示
- 修正最低 AstrBot 版本要求为 `>=4.26.0`

## 许可证

MIT，见 [LICENSE](LICENSE)。
