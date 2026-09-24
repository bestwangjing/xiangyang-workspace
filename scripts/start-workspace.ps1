$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$managedPython = Join-Path $projectRoot '.python\cpython-3.11.15-windows-x86_64-none\python.exe'
$pythonPath = if (Test-Path -LiteralPath $managedPython) { $managedPython } else { Join-Path $projectRoot '.venv\Scripts\python.exe' }
$env:PYTHONPATH = Join-Path $projectRoot '.venv\Lib\site-packages'
# Data root precedence: per-machine .data-root.local file, then XIANGYANG_DATA_ROOT,
# then a portable LOCALAPPDATA default (machines without a D: drive must still work).
$dataRootFile = Join-Path $projectRoot '.data-root.local'
if (Test-Path -LiteralPath $dataRootFile) { $env:XIANGYANG_DATA_ROOT = (Get-Content -LiteralPath $dataRootFile -Raw).Trim() }
elseif (-not $env:XIANGYANG_DATA_ROOT) { $env:XIANGYANG_DATA_ROOT = Join-Path $env:LOCALAPPDATA 'XiangyangWorkspace' }
$baseUrl = 'http://127.0.0.1:8766'
try {
    $probe = Invoke-WebRequest -Uri "$baseUrl/api/session" -UseBasicParsing -TimeoutSec 2
    if ($probe.StatusCode -eq 200 -and ($probe.Content | ConvertFrom-Json).app -eq 'xiangyang-workspace') { Start-Process $baseUrl; exit }
} catch { }
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python 环境缺失，请按开发使用说明安装依赖。' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web-dist\index.html'))) { throw '前端构建缺失，请运行 npm run build。' }
$logRoot = Join-Path $projectRoot 'runtime-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
Start-Process -FilePath $pythonPath -ArgumentList '-m','backend.run' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'server.out.log') -RedirectStandardError (Join-Path $logRoot 'server.err.log')
for ($attempt=0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $probe = Invoke-WebRequest -Uri "$baseUrl/api/session" -UseBasicParsing -TimeoutSec 2
        if ($probe.StatusCode -eq 200 -and ($probe.Content | ConvertFrom-Json).app -eq 'xiangyang-workspace') { Start-Process $baseUrl; exit }
    } catch { }
}
throw "服务未能启动，请查看 $logRoot\server.err.log"
