# 向阳AI工作台（Xiangyang Workspace）

本地优先的自媒体数据工作台：自动拉取 **小红书 / 抖音** 账号与作品数据，追踪指标变化，生成复盘报告。数据全部保存在你自己电脑上，密钥经 Windows DPAPI 加密存储，绝不上传。

> 仅支持 Windows 10/11（依赖 Windows 凭据保护 DPAPI 与本地文件锁）。

## 功能

- **账号总览**：粉丝数、作品数一键更新（TikHub API 拉取小红书公开数据；抖音扫码登录后读取）
- **我的创作**：全量/增量同步已发布作品，自动记录点赞、评论、收藏、分享等指标快照
- **抖音扫码登录**：弹出浏览器扫码，Cookie 经 DPAPI 加密落盘，支持作品播放量（创作者平台）
- **我的复盘**：结合大模型（Codex SDK）对作品表现做分析，导出 HTML 复盘报告
- **热搜库 / 爆款内容**：聚合多平台热点与低粉爆款参考
- **截图数据录入**：手机扫码上传后台截图，腾讯云 OCR 自动识别指标
- **每日调用限额**：每个第三方服务可设每日上限，防止意外扣费

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.11 · FastAPI · SQLite（标准库 sqlite3）· httpx |
| 前端 | React 19 · Vite 6 · TypeScript |
| 浏览器自动化 | Playwright（Chromium，抖音扫码登录） |
| 第三方 | TikHub（数据拉取）· 腾讯云 OCR · Codex SDK（大模型分析）· RedFox（热搜） |

## 安装

**零基础用户**：把 [INSTALL-PROMPT.md](INSTALL-PROMPT.md) 的全文复制给你的 AI 助手（WorkBuddy / TraeWork / Codex 等），它会自动完成安装并在桌面创建快捷方式。

**手动安装**（Windows，需 Python 3.11+ 与 Node.js 20+）：

```powershell
git clone https://github.com/bestwangjing/xiangyang-workspace.git
cd xiangyang-workspace
python -m venv .venv
.venv\Scripts\pip install -r requirements.lock.txt
npm ci
npm run build                 # 产出 web-dist/
npx playwright install chromium
powershell -ExecutionPolicy Bypass -File scripts\start-workspace.ps1
```

浏览器会自动打开 `http://127.0.0.1:8766`。

## 首次配置

启动后进入 **设置 → 数据服务**，按需填入（均为可选，填了才有对应功能）：

| 服务 | 用途 | 获取方式 |
| --- | --- | --- |
| TikHub API Key | 小红书/抖音数据拉取 | [tikhub.io](https://tikhub.io) 注册获取 |
| 腾讯云 SecretId/SecretKey | 截图 OCR 识别 | 腾讯云控制台开通 OCR 服务 |
| 模型配置 | 大模型复盘分析 | Codex SDK 兼容任意模型端点 |

密钥保存后仅以 DPAPI 密文存在于本机 `%LOCALAPPDATA%`，仓库与数据库中都不含明文。

## 目录结构

```
backend/     FastAPI 服务（路由、数据层、抖音登录、帖子拉取）
frontend/    React 单页应用源码（main.tsx / style.css / model-presets.ts）
scripts/     启动脚本与抖音扫码登录脚本（douyin-login.mjs）
tests/       Pytest 测试套件（85 项，含 API 拉取解析回归）
web-dist/    前端构建产物（npm run build 生成，不入库）
```

## 数据保存位置

数据根目录按以下优先级确定：

1. 项目根目录 `.data-root.local` 文件内容（本机覆盖，不入库）
2. 环境变量 `XIANGYANG_DATA_ROOT`
3. 默认 `%LOCALAPPDATA%\XiangyangWorkspace`

## 开发

```powershell
.venv\Scripts\python -m pytest tests -q      # 后端测试
npm run build                                # 前端构建（含 tsc 类型检查）
```

## 常见问题

- **双击桌面快捷方式没反应**：查看 `runtime-logs\server.err.log`；若提示 Python 缺失，说明 `.venv` 未创建，重跑安装步骤。
- **端口 8766 被占用**：任务管理器结束旧的 `python.exe`，或修改环境变量 `XIANGYANG_PORT` 后重启。
- **抖音扫码登录失败**：重新扫码即可；登录会话 10 分钟内有效，扫码后请等待 1-2 分钟让脚本读取账号信息。
- **TikHub 报“今日调用上限”**：设置 → 数据服务 中调高每日上限（默认按服务方计费谨慎设置）。

## 许可

仅供个人学习与自媒体运营使用。请遵守各平台服务条款与 TikHub 等服务方的使用政策。
