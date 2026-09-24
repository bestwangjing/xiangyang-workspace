# 向阳AI工作台 · 一键安装提示词

> 使用方法：复制本文全部内容，发送给任意 AI 助手（WorkBuddy、TraeWork、Codex 等），它会自动完成安装，并在你的桌面创建「向阳AI工作台」快捷方式。

---

你是一名 Windows 装机助手。请在本机全自动完成「向阳AI工作台」的安装、构建、桌面快捷方式创建和启动验证，全程不要向我提问，遇到问题自行排查解决。完成后向我报告结果。

## 目标

把 GitHub 仓库 `https://github.com/bestwangjing/xiangyang-workspace.git` 安装到本机并可通过桌面快捷方式一键启动，浏览器自动打开 `http://127.0.0.1:8766`。

## 前置环境（缺失就自动安装，装完继续）

1. Windows 10/11（本项目仅支持 Windows）
2. Git：终端执行 `git --version`，报错则 `winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements`，装完重开终端
3. Python 3.11 及以上：`python --version`，报错则 `winget install --id Python.Python.3.11 -e --accept-source-agreements --accept-package-agreements`
4. Node.js 20 及以上：`node --v`，报错则 `winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements`

注意：新装的 Git/Python/Node 需要新开一个终端窗口才会进 PATH。

## 安装步骤（按顺序执行，每步成功后再进行下一步）

1. 选一个安装目录（建议 `D:\apps` 或 `C:\apps`，目录不要有中文和空格），在该目录下执行：

   ```
   git clone https://github.com/bestwangjing/xiangyang-workspace.git
   ```

2. 进入项目目录，创建 Python 虚拟环境并安装依赖（国内网络可加清华镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple`）：

   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.lock.txt
   ```

3. 安装前端依赖并构建：

   ```
   npm ci
   npm run build
   ```

   `npm run build` 必须成功结束且生成 `web-dist\index.html`（这一步包含 TypeScript 类型检查，报类型错误说明环境异常，请排查 Node 版本）。

4. 安装抖音扫码登录所需的浏览器内核（下载较大，国内网络先设置镜像）：

   ```
   set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/
   npx playwright install chromium
   ```

5. 创建桌面快捷方式。把下面代码中的 `<项目绝对路径>` 替换为第 1 步的实际安装路径后执行：

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

6. 启动验证（这一步成功才算安装完成）：

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-workspace.ps1
   ```

   预期：默认浏览器自动打开 `http://127.0.0.1:8766`，页面正常显示工作台界面。也可以直接验证接口：

   ```
   curl http://127.0.0.1:8766/api/session
   ```

   返回 JSON 中包含 `"app":"xiangyang-workspace"` 即为成功。

## 验收清单（逐项确认后向用户报告）

- [ ] `git clone` 完成，项目目录存在
- [ ] `.venv` 创建且 `pip install -r requirements.lock.txt` 成功
- [ ] `npm run build` 成功，`web-dist\index.html` 存在
- [ ] `npx playwright install chromium` 成功
- [ ] 桌面出现「向阳AI工作台」快捷方式
- [ ] 服务启动后 `http://127.0.0.1:8766/api/session` 返回 `"app":"xiangyang-workspace"`
- [ ] 浏览器能正常打开工作台页面

## 常见问题（自行处理，不要询问用户）

- **8766 端口被占用**：`Get-NetTCPConnection -LocalPort 8766` 找到占用进程；若是本项目的旧 `python.exe` 直接结束重启；其他程序则设置环境变量 `XIANGYANG_PORT` 为其他端口再启动，并告知用户访问地址变了。
- **pip/npm 下载超时**：pip 加清华镜像；npm 用 `npm config set registry https://registry.npmmirror.com` 后重试。
- **playwright 下载失败**：确认已设置 `PLAYWRIGHT_DOWNLOAD_HOST` 镜像再重试；仍失败可告知用户此功能仅影响"抖音扫码登录"，不影响其他功能使用。
- **启动报 Python 缺失**：说明 `.venv` 未创建成功，回到第 2 步。
- **双击快捷方式窗口一闪而过但浏览器没打开**：查看 `<项目目录>\runtime-logs\server.err.log` 定位原因。

## 完成后向用户报告的内容

1. 安装路径与各验收项结果
2. 告知用户：以后双击桌面「向阳AI工作台」即可启动（若已在运行会直接打开页面）
3. 告知用户首次配置（软件内完成，不涉及命令行）：
   - 打开软件后进入「设置 → 数据服务」，填入 TikHub API Key（[tikhub.io](https://tikhub.io) 注册获取），用于拉取小红书/抖音数据
   - 可选：腾讯云 OCR（截图识别指标）、大模型配置（AI 复盘分析）
   - 所有密钥仅加密保存在本机，不会上传
4. 提醒用户：本项目数据默认保存在 `%LOCALAPPDATA%\XiangyangWorkspace`

## 约束

- 不要修改任何项目源代码；如果确有必须修改才能跑通的地方，先向用户说明原因和改动内容再动手
- 不要把任何密钥、token 写入项目文件
- 不要删除或覆盖 `runtime-logs`、数据库等本机数据
