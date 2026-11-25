# MbAutoSign：MineBBS 每日签到

脚本只使用 Cloak Browser，并复用项目中的专用浏览器配置。签到账户由你自己的 MineBBS 登录会话决定。

需要 Python 3.11+。支持 Windows x64、Linux x64 和 Linux arm64。在项目目录安装依赖，并将 `config.example.toml` 复制为 `config.toml`。如果已有配置，保留原文件。

Windows PowerShell：

```powershell
py -m pip install -r requirements.txt
if (!(Test-Path .\config.toml)) { Copy-Item .\config.example.toml .\config.toml }
py -X utf8 .\signin.py --check
```

Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
[ -e config.toml ] || cp config.example.toml config.toml
.venv/bin/python -X utf8 signin.py --check
```

Linux 浏览器需要系统共享库，例如 libatomic、NSS、GTK、X11 等；下载包不安装系统依赖。Debian/Ubuntu 上可用 Playwright 的 `python -m playwright install-deps chromium` 安装 Chromium 所需库，这条命令不下载浏览器。无桌面的 Linux 主机请保持 `browser.headless = true`；有界面测试需要可用图形会话。Windows 和 Linux 如共用工作目录，使用各自的浏览器配置目录，避免跨系统复用 Chromium 配置。

## Cloak Browser 和登录

默认先查项目缓存，再查本机的 `~/.cloakbrowser/chromium-*`。找不到时，自动从 Cloak Browser 官方发布下载当前代码固定的平台包，经大小和 SHA-256 校验后原子安装到 `.cloak-bin/<系统>-<架构>/`。自动安装不需要 `playwright install`，也不会改用系统 Chrome、Chromium 或 Edge。`browser.executable` 可指定受支持目录结构中的 Cloak 可执行文件（包含 `.cloakbrowser/chromium-<版本>/` 或项目 `.cloak-bin/` 的平台目录）；不符合约定的路径会记录并忽略。下载缓存与账号会话分别存放。

首次运行会创建专用配置目录。`login.username`、`login.password` 写在本机 `config.toml` 中；该文件为明文，请勿分享。`--check` 全程只读，`--login` 只恢复登录，`--submit` 尝试签到一次。打开同一配置的其他浏览器窗口会导致配置占用；关闭它后再运行。

掉线判断要求首页同时有 XenForo 游客标记、本站登录入口且无用户菜单。脚本打开登录弹窗，校验本站表单的目标后尝试登录，再重新加载首页确认。遇到验证码、无法确认的页面或异常时停止该轮操作。

需要恢复登录的模式遇到检测页时，最多被动等待 `browser.challenge_wait_seconds` 秒，默认 10，设置为 0 可关闭等待。只有站点自行返回正常首页才继续。`--check` 继续使用纯 HTML，不运行检测脚本。

有界面测试可将 `browser.headless` 设为 `false` 后运行 `--login`。本轮操作完成后浏览器自动关闭。

## 定时运行和排错

在 `[schedule]` 设置 `enabled = true`、`time = "09:00"` 和 `timezone = "Asia/Shanghai"`，然后运行 `--watch`。启动时若已到当天设定时间，会执行当天尚未认领的任务。只允许一个调度器在同一项目中运行，修改调度时间或总时限后请重启它。每天的任务由独立进程执行，默认总时限 `job_timeout_seconds = 600`，包括下载、启动、访问与关闭浏览器；任务失败或超时只结束本轮，调度器继续等待下一天。停止可按 Ctrl+C（Linux 服务也可发 SIGTERM）。系统重启后可用系统进程管理器重新启动 `--watch`。

轻量检查只加载首页 HTML，拦截子资源并禁用页面脚本。只有确认已登录且表单明确显示「今日尚未签到」时才允许签到。`logs/schedule/` 存放已认领日期，`logs/attempts/` 存放已保留的提交日期：当提交可能已到达、但结果未知时，当天不会自动重复提交。需要确认结果时使用 `--check`。

日志在 `logs/checkins.jsonl`，每条包含时间、模式、运行编号、进程、阶段或事件、耗时和级别。调度器里的 `job_id` 可与子进程日志的 `run_id` 对应。记录首页状态、登录/签到尝试次数、下载进度、浏览器清理和子进程退出码；异常仅记录类型、代码及定位，无账号、密码、Cookie、请求正文或 CSRF 令牌。当前文件约 2 MiB 自动轮转，保留 5 份归档；文件无法写入时转到终端日志。

| 常见状态 | 含义 |
| --- | --- |
| `profile_in_use` | 同一专用配置正在被使用。 |
| `security_check` / `verification_required` | 站点仍在检测或要求验证，该轮停止。 |
| `cloak_unavailable` | Cloak 解析、下载、校验、解压或安装失败；查看前面的阶段事件。 |
| `playwright_driver_failed` | 浏览器驱动未启动；精简 Linux 环境常见原因是缺少系统共享库。 |
| `worker_timeout` | 任务达到总时限，调度器清理该任务的进程。 |
| `submission_outcome_unknown` / `confirmation_missing` | 无法确认提交或提交后的状态；查看网页或运行 `--check`。 |

命令入口是 `signin.py`，实现放在 `mbautosign/`，回归测试放在 `tests/`。离线运行：`python -m unittest discover -s tests -v`。测试不会连接论坛。
