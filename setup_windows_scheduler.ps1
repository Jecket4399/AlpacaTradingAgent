<#
.SYNOPSIS
    为 AI 量化交易系统注册 Windows 定时任务

.DESCRIPTION
    创建两个定时任务：
    1. 每日分析：每天早上 8:30 运行（美股开盘前）
    2. 每小时监控：交易时段每 60 分钟运行

.PARAMETER ProjectDir
    AlpacaTradingAgent 项目目录路径

.EXAMPLE
    .\setup_windows_scheduler.ps1 -ProjectDir "E:\project\lianghua\AlpacaTradingAgent"
#>

param(
    [string]$ProjectDir = "E:\project\lianghua\AlpacaTradingAgent"
)

$python = (Get-Command python).Source
$dailyScript = Join-Path $ProjectDir "run_integration_daily.py"
$monitorScript = Join-Path $ProjectDir "run_integration_monitor.py"

Write-Host "========================================" -ForegroundColor Green
Write-Host "  AI 量化交易系统 - Windows 定时任务配置" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Python: $python"
Write-Host "项目目录: $ProjectDir"
Write-Host ""

# ---- 清理旧任务 ----
$taskNames = @("\AI量化-每日分析", "\AI量化-每小时监控")
foreach ($name in $taskNames) {
    try {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "已删除旧任务: $name"
    } catch {}
}

# ---- 创建每日分析任务 (美东 8:00 = 北京时间 20:00) ----
$dailyAction = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$dailyScript`" --top 25" `
    -WorkingDirectory $ProjectDir

$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "20:00"

$dailySettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "\AI量化-每日分析" `
    -Action $dailyAction `
    -Trigger $dailyTrigger `
    -Settings $dailySettings `
    -Description "每天从 stock-screener 拉取 Top 25 动量股做 AI 深度分析" `
    -User $env:USERNAME `
    -RunLevel Limited

Write-Host " 每日分析: 每天 20:00 运行" -ForegroundColor Cyan

# ---- 创建每小时监控任务 (交易时段 21:30 ~ 04:00) ----
# 美股交易时段 = 北京时间 21:30 ~ 次日 04:00 (夏令时)
$monitorAction = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$monitorScript`" --once" `
    -WorkingDirectory $ProjectDir

# 每小时从 21:30 开始，到次日 04:00
$hourlyTrigger = New-ScheduledTaskTrigger -Daily -At "21:30"
$hourlyRepetition = $hourlyTrigger.Repetition
$hourlyRepetition.Interval = "PT1H"  # 每1小时
$hourlyRepetition.Duration = "PT8H"  # 持续8小时

$monitorSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit "PT10M"  # 每次最多运行10分钟

Register-ScheduledTask -TaskName "\AI量化-每小时监控" `
    -Action $monitorAction `
    -Trigger $hourlyTrigger `
    -Settings $monitorSettings `
    -Description "美股交易时段每小时检查推荐列表并择机执行交易" `
    -User $env:USERNAME `
    -RunLevel Limited

Write-Host " 每小时监控: 每天 21:30 开始，每1小时间隔，持续8小时" -ForegroundColor Cyan

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  配置完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "查看任务: taskschd.msc → 任务计划程序库 → AI量化"
Write-Host "手动运行每日分析:  python `"$dailyScript`""
Write-Host "手动运行一次监控:  python `"$monitorScript`" --once"
Write-Host "开启持续监控:      python `"$monitorScript`" --loop"
