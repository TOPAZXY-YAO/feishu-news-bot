# 注册每日 08:00 的飞书财经早报计划任务（当前用户，无需管理员）
# 用法: powershell -ExecutionPolicy Bypass -File register_task.ps1
$ErrorActionPreference = "Stop"
$taskName = "FeishuNewsBot"
$scriptPath = Join-Path $PSScriptRoot "feishu_news.py"

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Error "找不到 python，请先安装或加入 PATH"; exit 1 }

$action    = New-ScheduledTaskAction -Execute $py -Argument "`"$scriptPath`""
$trigger   = New-ScheduledTaskTrigger -Daily -At 08:00
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
             -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
             -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -MultipleInstances IgnoreNew

# 优先 S4U：未登录桌面也能跑；失败则回退为仅登录时运行
try {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "已注册任务 '$taskName'（每天 08:00，错过自动补跑，无需登录桌面）"
} catch {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
    Write-Host "已注册任务 '$taskName'（每天 08:00，错过自动补跑；仅登录状态可运行）"
}
Write-Host "Python: $py"
Write-Host "脚本  : $scriptPath"
