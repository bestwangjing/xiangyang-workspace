$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$managedPython=Join-Path $projectRoot '.python\cpython-3.11.15-windows-x86_64-none\python.exe'
$legacyPython=Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $managedPython)) { throw '项目 Python 运行时缺失，不停止现有服务。' }
$env:PYTHONPATH=Join-Path $projectRoot '.venv\Lib\site-packages'
# Same data-root precedence as start-workspace.ps1 (machine override file, env var, LOCALAPPDATA).
$dataRootFile=Join-Path $projectRoot '.data-root.local'
if (Test-Path -LiteralPath $dataRootFile) { $env:XIANGYANG_DATA_ROOT=(Get-Content -LiteralPath $dataRootFile -Raw).Trim() }
elseif (-not $env:XIANGYANG_DATA_ROOT) { $env:XIANGYANG_DATA_ROOT=Join-Path $env:LOCALAPPDATA 'XiangyangWorkspace' }
$listeners=Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $ownedProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $launcherProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$($ownedProcess.ParentProcessId)"
    if ($ownedProcess.CommandLine -notlike '*backend.run*' -or ($ownedProcess.ExecutablePath -notin @($managedPython,$legacyPython) -and $launcherProcess.ExecutablePath -notin @($managedPython,$legacyPython))) { throw '端口被其他程序使用，不停止该进程。' }
    Stop-Process -Id $listener.OwningProcess
}
Start-Sleep -Milliseconds 500
Start-Process -FilePath $managedPython -ArgumentList '-m','backend.run' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'runtime-logs\server.out.log') -RedirectStandardError (Join-Path $projectRoot 'runtime-logs\server.err.log')
