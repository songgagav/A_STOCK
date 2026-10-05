# 运维 Runbook

本文面向非交易时段维护和故障排查。任何涉及行情、信号快照或交易闸门的操作，
先确认当前是否为交易时段；不要用手工补数据替代正式发布链路。

## 服务控制

统一服务入口：

```powershell
pwsh -File ops/service_control.ps1 -Action status
pwsh -File ops/service_control.ps1 -Action start
pwsh -File ops/service_control.ps1 -Action stop
pwsh -File ops/service_control.ps1 -Action restart
pwsh -File ops/service_control.ps1 -Action heal
pwsh -File ops/service_control.ps1 -Action diag
```

启动顺序由脚本保证：先探活 `AStockStockdb`，再启动 daemon。不要直接双击
`stockdb.exe`，也不要绕过 daemon 直接启动引擎。

常用只读检查：

```powershell
Get-Service AStockStockdb
Get-Process stockdb, python -ErrorAction SilentlyContinue
Get-Content logs\stockdb_service.err.log -Tail 80
Get-Content logs\daemon_tail.log -Tail 80
```

## 看板

```powershell
python src/run_services.py status
python src/run_services.py start
python src/run_services.py stop
```

看板默认监听 8000。若端口被占用，先查看 `logs/dashboard.pid` 和实际命令行，
不要盲目强杀未知 Python 进程。模板和静态资源变更后必须通过服务管理路径重启，
仅修改磁盘文件不会刷新已运行进程。

## 数据源 Router

Router 默认关闭；非交易时段可做确定性测试和临时目录 shadow smoke：

```powershell
$env:DATA_SOURCE_ROUTER_ENABLED = "1"
$env:DATA_SOURCE_ROUTER_MODE = "shadow"
$env:DATA_SOURCE_ROUTER_SYMBOLS = "600000.SH"
```

首次真实网络探针必须使用临时 root、小范围标的和 shadow。不得在没有质量报告、
manifest 和审计记录时写入生产 h5i。`enforce` 需要人工确认，且不得接实盘。

## ArcticDB 缺失

ArcticDB 是兼容层，不是当前 Router/h5i 主路径的必要依赖。缺包时：

- 盘前检查必须返回明确 `SKIP/retired` 或 `unavailable`；
- 退化检查不得把缺失转换成“无退化”；
- 不删除 `src/arctic_store.py`；
- 迁移计划见 `docs/evolution/arcticdb-compat-migration.md`。

## 安全扫描

在 `.venv314` 中执行：

```powershell
python -m pip_audit -r requirements.txt --format columns
python -m detect_secrets scan --force-use-all-plugins .
python -m bandit -r src -ll -f txt
```

Bandit 非零不是可忽略的成功；当前 medium 命中全部为 B608，需按 SQL 输入边界
逐项复核。`detect-secrets --all-files` 会把运行时日志、缓存和数据纳入扫描，
出现高熵伪阳性时必须分区判断，不能直接用作清洁结论。

## 交易时段保护

- 不在交易时段执行 ArcticDB 迁移、依赖升级、服务重装或生产数据回填；
- 不在 Phase E 期间修改 signal freeze、PaperBook 和实时消费逻辑；
- `TRADE_BROKER` 非 `paper` 时不得进行自动交易测试；
- 任何快照缺失、校验失败或数据源陈旧都应 fail-closed，并保留账本记录。
