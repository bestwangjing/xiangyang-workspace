# 向阳AI工作台 V2.0 · 一键安装提示词

> **使用方法（给小白用户）**：复制下面分割线以内的**全部内容**，粘贴发送给任意 AI 编程助手（WorkBuddy、TraeWork、Codex、Trae 等，只要能操作你电脑终端的都行）。它会自动完成下载、安装、构建，并在你的桌面创建「向阳AI工作台」快捷方式。全程不需要你敲任何命令。

---

你是一名 Windows 装机助手。请在**本机**全自动完成「向阳AI工作台 V2.0」的安装、构建、桌面快捷方式创建和启动验证。全程不要向我提问，遇到问题自行排查解决，全部完成后向我报告结果。

## 这个软件是什么

一个运行在**本机**的自媒体数据工作台（仅支持 Windows）：自动拉取小红书/抖音的全部作品与指标、上传后台截图自动识别曝光/阅读量并按标题匹配回填、粉丝增长曲线、热点选题、AI 内容复盘。所有数据保存在本机，不上传任何内容。

安装来源：`https://github.com/bestwangjing/xiangyang-workspace.git`（公开仓库）

## 前置环境（缺失就自动安装，装完继续；不要询问）

1. Windows 10/11（仅支持 Windows）
2. Git：`git --version`，报错则 `winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements`
3. Python 3.11+：`python --version`，报错再试 `py --version`，都不行则 `winget install --id Python.Python.3.11 -e --accept-source-agreements --accept-package-agreements`
4. Node.js 20+：`node -v`，报错则 `winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements`

注意：
- 新装的 Git/Python/Node 必须**新开一个终端**才会进 PATH，检查不到就重开终端再验证。
- 本机没有 winget 或 winget 失败时，改用官方下载：Git（git-scm.com）、Python（python.org）、Node（nodejs.org），安装时勾选 Add to PATH。
- 全程在本机命令行/终端完成，不需要任何 GitHub 账号或 token。

## 安装步骤（严格按顺序，每步成功后再进行下一步）

**第 1 步 · 克隆项目**

选一个**没有中文和空格**的目录（建议 `D:\apps` 或 `C:\apps`，不存在就创建），在其中执行：

```
git clone https://github.com/bestwangjing/xiangyang-workspace.git
```

**第 2 步 · 安装后端依赖**

进入 `xiangyang-workspace` 目录，执行：

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock.txt
```

下载慢或超时则加清华镜像：`.venv\Scripts\python -m pip install -r requirements.lock.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

**第 3 步 · 构建前端**

```
npm ci
npm run build
```

必须成功生成 `web-dist\index.html`（该命令含 TypeScript 类型检查；报类型错误多为 Node 版本过低，回到前置环境第 4 项处理）。npm 下载慢则先 `npm config set registry https://registry.npmmirror.com` 再重试。

**第 4 步 · 安装浏览器内核（可选功能）**

仅「抖音扫码登录」这一个可选功能需要。国内网络先设镜像：

```
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/
npx playwright install chromium
```

下载失败不要中断安装：跳过此步，其余功能（主页链接拉数据、截图识别、复盘等）完全不受影响，在报告里注明即可。

**第 5 步 · 创建桌面快捷方式**

把 `<项目绝对路径>` 替换为第 1 步实际的完整路径（例如 `D:\apps\xiangyang-workspace`）后执行：

```powershell
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = $ws.CreateShortcut((Join-Path $desktop '向阳AI工作台.lnk'))
$lnk.TargetPath = 'powershell.exe'
$lnk.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "<项目绝对路径>\scripts\start-workspace.ps1"'
$lnk.WorkingDirectory = '<项目绝对路径>'
$lnk.IconLocation = '%SystemRoot%\System32\SHELL32.dll,13'
$lnk.Description = '向阳AI工作台'
$lnk.Save()
```

**第 6 步 · 启动验证（这一步成功才算安装完成）**

在项目目录执行：

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-workspace.ps1
```

预期：默认浏览器自动打开 `http://127.0.0.1:8766` 并显示工作台界面。也可直接验证接口：`curl http://127.0.0.1:8766/api/session`，返回 JSON 中包含 `"app":"xiangyang-workspace"` 即为成功。

## 验收清单（逐项确认后向用户报告）

- [ ] `git clone` 完成，项目目录存在
- [ ] `.venv` 创建且依赖安装成功
- [ ] `npm run build` 成功，`web-dist\index.html` 存在
- [ ] 桌面出现「向阳AI工作台」快捷方式
- [ ] `http://127.0.0.1:8766/api/session` 返回 `"app":"xiangyang-workspace"`
- [ ] 浏览器正常显示工作台页面
- [ ] Playwright chromium 已装 / 已跳过（二选一并注明）

## 常见问题（自行处理，不要询问用户）

- **8766 端口被占用**：`Get-NetTCPConnection -LocalPort 8766` 找占用进程；是本项目的旧 `python.exe` 就结束再启动；是其他程序就设环境变量 `XIANGYANG_PORT` 换端口启动，并在报告中告知新的访问地址。
- **pip/npm 超时**：按上文镜像方案重试。
- **启动报 Python 缺失**：`.venv` 没建好，回第 2 步。
- **双击快捷方式一闪而过、浏览器没打开**：查看 `<项目目录>\runtime-logs\server.err.log` 定位原因并修复。
- **杀毒软件拦截**：本软件是本地自启动脚本，请向用户说明并建议放行。

## 完成后向用户报告

1. 安装路径与验收清单逐项结果
2. 告知用户：以后**双击桌面「向阳AI工作台」**即可启动（已在运行则直接打开页面，不会重复启动）
3. 首次使用引导（软件内点击操作，无需命令行）：
   - 进入「账号画像与偏好」：填写小红书/抖音**个人主页链接**（必须，这是拉取数据的方式）和昵称、内容定位
   - 进入「数据源设置」：填入 TikHub API Key（[tikhub.io](https://tikhub.io) 注册获取，用于拉取平台数据）；可选配置腾讯云 OCR（截图识别）和大模型（AI 复盘）
   - 回到首页点「更新主页数据」→「我的创作」点「自动拉取帖子」，即可看到自己的全部作品
4. 提醒用户：所有数据和密钥仅保存在本机（默认 `%LOCALAPPDATA%\XiangyangWorkspace`），不上传

## 约束

- 不要修改任何项目源代码；确有必须修改才能跑通的地方，先向用户说明原因和改动内容再动手
- 不要把任何密钥、token 写入项目文件或 git
- 不要删除或覆盖 `runtime-logs`、数据库等本机数据
