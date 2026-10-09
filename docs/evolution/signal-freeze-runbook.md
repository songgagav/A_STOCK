# 09:25 信号冻结 Shadow Runbook

状态：运行手册；仅用于 Phase E shadow 观察，不启用 enforce，不接实盘。  
日期：2026-10-09

## 观察窗口

观察日必须同时满足：交易日、守护实际运行过 09:25、快照状态为 `ready`、当日证据已写入 [`signal-freeze-promotion.md`](signal-freeze-promotion.md)。

2026-10-03 至 2026-10-07 为非交易窗口，不计入连续 5 个交易日观察。2026-10-08 的历史 live_state 没有形成有效冻结证据，不能追认计数；2026-10-09 仅在今日 08:30、09:25 和收盘证据完整后才可计入。周末和节假日不补写快照、不补算观察日。

## 启动前检查

在仓库根目录执行：

```powershell
$py = (Resolve-Path '.venv314\Scripts\python.exe').Path
$env:PYTHONPATH = (Resolve-Path 'src').Path
& $py -c "from signal_snapshot import read_mode_control; print(read_mode_control('.'))"
& $py -c "import config; print('TRADE_BROKER=', config.TRADE_BROKER)"
& $py -c "import realtime_engine; print('realtime_engine=ok')"
Test-Path data\trade_calendar.json
```

预期：模式为 `shadow`，`TRADE_BROKER` 为 `paper`，引擎可导入，交易日历存在。控制文件缺失时，安全默认仍为 `shadow`；不要为了观察强行创建 `enforce` 控制记录。

## 运行时边界

1. `daemon.py` 只在交易日拉起盘中引擎；它是 09:25 冻结的正常触发入口。
2. 09:25 前候选只用于准备、日志和监控，不作为权威目标池。
3. 09:25 时生成并验证 `data/daily/<YYYYMMDD>/signal_snapshot_<YYYYMMDD>.json`。
4. 09:25 后不手动替换快照；午间重选只能进入迟到信号归档。
5. 快照缺失、无效或篡改时，只估值不自动调仓；当日不可通过补写快照恢复自动调仓。
6. 迟到信号不自动影响仓位，必须等完整候选池和权重的人工审核，且只作为次日可选输入。

## 每日证据

快照成功后检查以下路径：

```powershell
$today = Get-Date -Format 'yyyyMMdd'
Test-Path "data\daily\$today\signal_snapshot_$today.json"
Test-Path "data\daily\$today\late_signals_$today.json"
Get-Content docs\evolution\signal-freeze-promotion.md
```

把真实运行结果写入 promotion 文档：快照状态/hash、迟到候选数、差异分类、证据路径和审核人。未分类差异默认是 `unexplained`，出现一次即从次日重新计数。

## 发布与重启纪律

修改源码后必须按 [`docs/deployment.md`](../deployment.md) 的强制清单重启守护并核对运行时代码版本。只看到新文件或周期快照刷新，不等于守护已经加载新模块。

2026-10-09 开盘准备记录：行情 freshness 已通过，守护进程已重启并等待 08:30；09:25 快照时钟已统一为 `Asia/Shanghai`。在今日同日快照实际生成前，不得把本记录解释为“已完成观察日”。

观察期禁止：

- 修改冻结相关源码；
- 手动补写或覆盖生产快照；
- 将模式改为 `enforce`；
- 设置非 `paper` broker 或接入券商 API；
- 把非交易日算入 5 日证据。

只有连续 5 个真实交易日没有 `unexplained` 差异，并完成具名人工批准，才可另开 Phase F PR 讨论 paper-only enforce；本手册本身不会触发切换。
