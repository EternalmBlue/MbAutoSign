# MbAutoSign：MineBBS 每日签到

在 Windows x64、Linux x64 或 Linux ARM64 上运行。程序使用专用的 Cloak Browser 配置保存 MineBBS 登录会话。

## 下载安装

从 [GitHub Releases](https://github.com/EternalmBlue/MbAutoSign/releases) 下载对应平台的压缩包。每个包都包含本平台的可执行文件、启动脚本、`config.toml` 和第三方声明。无需另装 Python，可用发行页的 `SHA256SUMS` 校验压缩包。

- **Windows x64**：解压 `MbAutoSign-windows-x64.zip`，进入解压目录。填写配置后，在 PowerShell 运行 `.\run-windows.ps1`，也可以直接运行 `MbAutoSign.exe`。若 PowerShell 因文件来自网络而拒绝运行，校验压缩包后可执行 `Unblock-File .\run-windows.ps1`。
- **Linux x64 / ARM64**：用 `tar -xzf MbAutoSign-linux-x64.tar.gz`（ARM64 将文件名换成 `MbAutoSign-linux-arm64.tar.gz`）解压，然后进入同名目录。填写配置后运行 `./run-linux.sh`，也可以直接运行原生二进制 `./MbAutoSign`。

Linux 发行包以 Ubuntu 22.04 为构建基线。Cloak Browser 仍需要系统的共享库，如 libatomic、NSS、GTK、X11，安装包不更改系统依赖。无桌面的主机请保持 `browser.headless = true`。

## 配置和运行

解压后直接编辑可执行文件旁的 `config.toml`。`[login]` 中的 `username` 和 `password` 默认留空，填写后可在登录会话失效时恢复登录。配置是明文，请勿分享。找不到配置时，程序会在终端和 `logs/checkins.jsonl` 提醒你创建并填写，然后退出。

- `schedule.enable = true`（默认）：进程常驻，默认每天北京时间 `00:05` 执行，可通过 `schedule.time` 和 `schedule.timezone` 调整。启动时若已过当天设定时间，会执行当天尚未认领的任务。
- `schedule.enable = false`：启动后尝试签到一次，完成或失败即退出。

启动时无需任何参数。旧配置里的 `schedule.enabled` 仍可读取；建议改成 `schedule.enable`。升级时请先备份并保留已有的 `config.toml`，避免被新版包中的空白配置覆盖；新默认值不会应用到旧配置。专用浏览器配置、`.cloak-bin/` 和 `logs/` 也请保留。

程序先查项目缓存和本机 Cloak 默认安装缓存，只使用当前平台的固定版本。没有可用的本地版本时，先从 Cloak 官方源下载，失败时转试官方 GitHub 源，校验文件大小和固定 SHA-256 后安装到 `.cloak-bin/`。**发行包不包含 Cloak Browser**，首次且无缓存时需要联网。配置里的自定义可执行路径不会生效。

首页明确显示会话失效时才尝试登录。遇到检测页，最多被动等待 `browser.challenge_wait_seconds` 秒；遇到验证码或无法确认页面时停止该轮。只有确认登录且页面明确显示今日未签到才会提交。

## 定时行为和日志

同一安装目录只运行一个调度器，修改调度配置后重启。每次尝试由独立子进程执行，`job_timeout_seconds` 默认 600 秒。只有明确尚未尝试发送登录或签到 POST 时，首页断网、DNS 失败、连接超时、HTTP 408/429/5xx 或临时的 Cloak 下载故障才可当天重试。默认间隔 300 秒、额外重试 2 次，可用 `retry_delay_seconds` 和 `retry_count` 调整。达到上限、跨日、调度器重启或提交结果不明时不再重试。可按 Ctrl+C 停止调度器。

`logs/checkins.jsonl` 记录阶段、结果、HTTP 或网络错误类别和耗时，无账号、密码、Cookie 或请求正文。用调度器的 `job_id` 对应子进程的 `run_id`。日志约 2 MiB 自动轮转，保留 5 份归档。请保留 `logs/schedule/` 和 `logs/attempts/` 的日期标记，避免重复认领或提交。

| 常见状态 | 怎么处理 |
| --- | --- |
| `configuration_file_missing` | 在程序所在目录创建并填写配置。 |
| `homepage_failed` / `homepage_unreachable` | 查看 `home_status` 或 `network_error`，确认论坛和网络可用。 |
| `cloak_unavailable` | 查看前面的下载源、校验或安装事件。 |
| `profile_in_use` | 关闭占用同一专用配置的浏览器窗口。 |
| `security_check` / `verification_required` | 站点仍在检测或要求验证，本轮已停止。 |
| `worker_timeout` | 任务达到总时限；调度器会继续运行。 |
| `submission_outcome_unknown` / `confirmation_missing` | 到 MineBBS 网页确认结果；程序不会自动重复提交。 |

## 从源码运行

需要 Python 3.11+。如果项目根目录还没有 `config.toml`，先手动将 `packaging/config.toml` 复制到项目根目录，填写后再运行：

```sh
python -m pip install -r requirements.txt
python -X utf8 signin.py
```

离线回归：`python -m unittest discover -s tests -v`。发布工作流在三个原生平台上运行测试并构建 PyInstaller 发行物；推送 `vX.Y.Z` 标签后发布 GitHub Release。也可手动运行工作流，仅构建供下载的 CI 产物。
