# 定时任务模板（E-1）

- **macOS**：把 `com.autobiz.backup.plist` 复制到 `~/Library/LaunchAgents/`，把 `SCRIPTS_DIR` 改成
  本仓库 `scripts/` 的绝对路径，然后 `launchctl load ~/Library/LaunchAgents/com.autobiz.backup.plist`。
- **Linux（systemd）**：把 `autobiz-backup.service` / `.timer` 复制到 `~/.config/systemd/user/`，
  替换 `SCRIPTS_DIR` 后 `systemctl --user enable --now autobiz-backup.timer`。
- **Windows**：任务计划程序新建每日任务，操作指向 `"bash scripts/backup.sh"`（需 WSL/Git Bash）。

## 每日简报 / 每周复盘（ADR-0015）

先在 `.env` 配好 `FEISHU_BOT_WEBHOOK` / `FEISHU_BOT_SECRET`，手动试一次：`uv run autobiz brief --send`。

- **macOS**：把 `com.autobiz.brief.plist`（日报 08:00）和 `com.autobiz.brief-weekly.plist`（周一 08:30）复制到
  `~/Library/LaunchAgents/`，把 `SCRIPTS_DIR` 换成本仓库 `scripts/` 的绝对路径，然后分别 `launchctl load <文件>`。
- **Linux（systemd）**：`autobiz-brief.{service,timer}` 与 `autobiz-brief-weekly.{service,timer}` 复制到
  `~/.config/systemd/user/`，替换 `SCRIPTS_DIR` 后 `systemctl --user enable --now autobiz-brief.timer autobiz-brief-weekly.timer`。
- **Windows**：任务计划程序新建两个任务，操作分别为 `bash scripts/brief.sh` 与 `bash scripts/brief.sh --weekly`。
- 运行日志：`$AUTOBIZ_HOME/logs/brief.log`。电脑关机 / 休眠时不会推送，开机唤醒后补跑。
