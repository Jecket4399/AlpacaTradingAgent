<#
.SYNOPSIS
    为 AI 量化交易系统注册 Windows 定时任务（美股交易时段）
.DESCRIPTION
    美股 9:30-16:00 EDT = 北京时间 21:30-次日 04:00（夏令时）
                       = 北京时间 22:30-次日 05:00（冬令时）
    脚本内部用 _is_market_hours() 自适应处理，无需区分冬夏令时

    创建两个定时任务：
    1. 每日同步：21:15 从 daily_stock_analysis 拉取 BUY 推荐
    2. 每小时监控：21:30 起每 60 分钟，持续到次日 04:30
#>

param(
    [string]$ProjectDir = "E:\project\lianghua\AlpacaTradingAgent"
)

$python = (Get-Command python).Source
$dailyScript = Join-Path $ProjectDir "run_integration_daily.py"
$monitorScript = Join-Path $ProjectDir "run_integration_monitor.py"

Write-Host "========================================" -ForegroundColor Green
Write-Host "  AI 量化交易 - Windows 定时任务" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Python: $python"
Write-Host "项目:   $ProjectDir"
Write-Host ""
Write-Host "美股时间线（北京时间 CST）："
Write-Host "  20:00  stock-screener 全市场扫描 (GitHub Actions)"
Write-Host "  21:00  daily_stock_analysis AI分析 (GitHub Actions)"
Write-Host "  21:15  每日同步：提取 BUY 推荐 (本机)"
Write-Host "  21:30  美股开盘 → 每小时监控启动"
Write-Host "  04:00  美股收盘 → 监控停止"
Write-Host ""

# ---- 清理旧任务 ----
foreach ($name in @("\AI量化-每日同步", "\AI量化-每小时监控")) {
    try {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "已删除旧任务: $name"
    } catch {}
}

# ---- 每日同步：21:15 ----
$dailyAction = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$dailyScript`"" `
    -WorkingDirectory $ProjectDir

$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "21:15"

Register-ScheduledTask -TaskName "\AI量化-每日同步" `
    -Action $dailyAction `
    -Trigger $dailyTrigger `
    -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew) `
    -Description "从 daily_stock_analysis 最新分析中提取 BUY 推荐，放入监控列表" `
    -RunLevel Limited

Write-Host "[ok] 每日同步: 每天 21:15" -ForegroundColor Cyan

# ---- 每小时监控：21:30 起每小时，持续到次日 04:30 ----
$monitorAction = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$monitorScript`" --once" `
    -WorkingDirectory $ProjectDir

$monitorTrigger = New-ScheduledTaskTrigger -Daily -At "21:30"
$monitorTrigger.Repetition.Interval = "PT1H"
$monitorTrigger.Repetition.Duration = "PT7H"   # 21:30 ~ 04:30 = 7小时

Register-ScheduledTask -TaskName "\AI量化-每小时监控" `
    -Action $monitorAction `
    -Trigger $monitorTrigger `
    -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit "PT10M") `
    -Description "美股交易时段每小时检查推荐列表并择机执行模拟盘交易" `
    -RunLevel Limited

Write-Host "[ok] 每小时监控: 21:30-04:30, 每1小时间隔" -ForegroundColor Cyan

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  完成！查看: taskschd.msc → AI量化" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "手动命令:"
Write-Host "  每日同步:  python run_integration_daily.py --dry-run"
Write-Host "  单次监控:  python run_integration_monitor.py --once"
Write-Host "  持续监控:  python run_integration_monitor.py --loop"
