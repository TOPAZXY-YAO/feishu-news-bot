# 注册本地通道计划任务：每小时检查《管理世界》《数量经济技术经济研究》（仅中国大陆网络可达的源）
# 去重保证无新内容时不推送、不打扰。用法: powershell -ExecutionPolicy Bypass -File register_cn_task.ps1
$ErrorActionPreference = "Stop"
$taskName = "FeishuPaperCNLocal"
$workDir = $PSScriptRoot

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Error "找不到 python"; exit 1 }

$action = New-ScheduledTaskAction -Execute $py -Argument '"paper_bot.py" --cn-local' -WorkingDirectory $workDir

# 每小时重复，持续十年（覆盖电脑任意开机时段；月刊制下去重机制保证零打扰）
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Write-Host "已注册任务 '$taskName'（每小时检查一次，无新论文不推送）"
Write-Host "Python: $py"
Write-Host "目录  : $workDir"
