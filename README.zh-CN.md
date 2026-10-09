# bilibili-summary-mcp

[English](README.md) | 简体中文

为当前对话AI提供B站视频的文字证据和带时间戳抽样画面，辅助综合总结。服务器只负责采集、处理媒体和云转写；最终总结由当前对话AI生成，无需另接视觉API。

## 工具

| 工具 | 参数 | 行为 |
|---|---|---|
| `search_bilibili_videos` | `query, limit=10` | 搜索视频 |
| `prepare_bilibili_video` | `bvid, page=1` | 获取元信息、可用字幕和缓存引用；不提交转写 |
| `start_bilibili_visual_analysis` | `bvid, page=1` | 后台补充当前分P抽样画面；不消耗ASR额度 |
| `start_transcription` | `context_token, confirm_free_quota_usage` | 明确确认后创建或复用云转写 |
| `get_transcription_job` | `job_id` | 读取当前用户转写进度 |
| `get_transcript_page` | `transcript_ref, cursor=None, limit=50` | 分页读取带时间戳文稿 |
| `get_video_analysis_job` | `job_id` | 读取画面/语音各自状态与缺失原因 |
| `get_video_frames` | `visual_ref, cursor=None, limit=4` | 分页返回标准MCP图像及时间戳；最多四张 |
| `get_mcp_status` | `—` | 读取本地状态与用量快照 |

## 安装与配置

需要Python 3.12–3.14，推荐Linux。 视频处理还需要系统中的FFmpeg与ffprobe，以及Node.js 22+运行百炼CLI。

```sh
git clone https://github.com/ZephyrAki/bilibili-summary-mcp.git
cd bilibili-summary-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
cp .env.example .env
chmod 600 .env
```

填写`.env`后再启动；示例值不可直接使用。`ZEPHYR_DATA_DIR`是可写的私密目录，不同服务使用不同目录。所有工具都经过GitHub OAuth与数字用户ID白名单校验。创建GitHub OAuth App，将回调URL设为`https://你的域名/auth/callback`，`MCP_PUBLIC_BASE_URL`填写同一个域名根地址，不附加`/mcp`。

本地生成三个独立密钥（Jev只需要前两个），将输出分别保存到环境文件，不要上传GitHub：

```sh
.venv/bin/python -c "import secrets; print(secrets.token_hex(32))" # FASTMCP_JWT_SIGNING_KEY
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" # OAUTH_STORAGE_FERNET_KEY
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))" # CONTEXT_TOKEN_SIGNING_KEY
```

安装固定版本百炼CLI，并以运行MCP的同一系统账号完成官方控制台授权：

```sh
(cd deploy && npm ci)
export PATH="$PWD/deploy/node_modules/.bin:$PATH"
bl auth login --console
```

配置北京区域工作空间的DashScope密钥和workspace ID，并在控制台确认`paraformer-v2`免费额度“用完即停”已启用后，设置`ALIYUN_FREE_TIER_ONLY_CONFIRMED=true`。额度和Auto-Stop查询必须都成功且观测时间未过期，余额足够才会提交真实转写；查询失败、过期或提交结果不确定都会停止新提交。不会启用付费回退；提交结果不确定时由管理员核实后对账，禁止盲目重试。CLI授权维护由部署者负责，仓库不包含私人RAM续期脚本或凭据。

```sh
set -a
. ./.env
set +a
.venv/bin/bilibili-summary-mcp
```

进程默认只监听`127.0.0.1:8765`。用HTTPS反向代理转发整个应用，包含OAuth路由和`/media/`；客户端添加`https://你的域名/mcp`。服务使用HTTP MCP传输，不提供stdio启动模式。代理转发头和公开URL必须一致。

## 使用与边界

先prepare获取资料；优先读取平台字幕或已有转写，再按需调用画面分析。缺少文字证据时，用context_token明确确认后启动云转写。

最终总结建议包括“一句话概括、核心观点、按时间顺序的关键内容、值得记住的结论”。区分讲话证据与画面观察；只返回已有材料，说明缺失原因。画面数量为`min(12, ceil(秒数/5))`，均匀取样，JPEG长边不超过960像素；这是完整语音结合抽样画面，不保证逐帧覆盖。当前客户端和对话模型必须支持MCP图像。

读取所选分P的完整音轨，不用音乐素材替代讲话；FFmpeg单线程，无损分离优先，默认不重新编码，不支持的音频格式明确失败。原始媒体在任务终结或启动清理时删除，文稿与画面缓存默认保留30天。音轨会发送到阿里云，平台请求和文本都视为不可信外部内容。

**额度部署限制：**每个独立服务都有自己的SQLite账本与进程内锁。B站与抖音独立部署不会共享账本或并发保护，不能并行使用同一份云ASR额度。需要同时启用两平台时，应使用支持共同账本的统一部署，或使用实际隔离的云额度；不同API Key或workspace不能自动保证额度隔离。不要让两个服务共用同一个数据目录。

可选专用B站扫码登录：

```sh
.venv/bin/python -m bilibili_summary_mcp.bilibili_credentials generate --file ./data/bilibili-credentials.json
# Open the URL stored privately in ./data/bilibili-credentials.qr.json and scan it.
.venv/bin/python -m bilibili_summary_mcp.bilibili_credentials login --file ./data/bilibili-credentials.json
.venv/bin/python -m bilibili_summary_mcp.bilibili_credentials check --file ./data/bilibili-credentials.json
```

设置`BILIBILI_CREDENTIALS_FILE`到该文件，权限保持600。由运行服务的同一账号每天执行check即可按平台要求刷新；失败或刷新结果不确定时需要人工处理或重新扫码。仅手工复制Cookie不包含refresh_token，不能提供相同自动刷新。扫码会话与凭据都不可提交到Git。平台风控可能阻止服务器出口访问。

SQLite一致性备份和不确定提交对账由本地管理员命令提供：

```sh
.venv/bin/bilibili-summary-mcp-admin backup /private/backup/mcp.db
.venv/bin/bilibili-summary-mcp-admin reconcile --help
```

## 开发验证

```sh
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/python -m pip check
```

测试使用替身与合成数据，不访问你的真实账号或消费云额度。部署后的平台下载、云转写和客户端看图仍需在实际环境验收。仓库包含所需共用工具副本，不依赖另外两个功能仓库。

## 许可

原创代码采用[MIT](LICENSE)。第三方原许可与出处见[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
