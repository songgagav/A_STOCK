# Celery / 服务生命周期调用图

## 扫描结果

排除 `_archive/`、`data/`、`logs/` 后，关键词命中 575 条。复核命令：

```powershell
rg -n "celery|Celery|flower|Flower|worker|tasks_db|daemon|run_services|dashboard_keepalive|data_update_daemon" `
  --hidden --glob '!.git/**' --glob '!_archive/**' --glob '!data/**' --glob '!logs/**' `
  --glob '*.py' --glob '*.ps1' --glob '*.bat' --glob '*.xml' --glob '*.md' .
```

## 调用关系库存

| 管理者/调用方 | 被管理组件 | 入口性质 | 当前判断 |
| --- | --- | --- | --- |
| `src/daemon.py` | dashboard、realtime engine、观测栈、Celery worker/Flower | 主守护入口 | 活跃，当前最接近唯一管理者 |
| `src/tasks_db.py` | Celery task、`db_stats.update_all_tables` | 异步数据更新入口 | 活跃，不能删除 |
| `src/dashboard.py` | `tasks_db`、`update_db`、手动/异步更新按钮 | Web 运维入口 | 活跃，需与 daemon 协调职责 |
| `src/run_services.py` | dashboard、realtime engine | 手动服务入口 | 活跃或兼容入口，需避免重复拉起 |
| `src/dashboard_keepalive.py` | dashboard | 独立看护入口 | 与 daemon 存在职责重叠，优先评估是否退役 |
| `src/data_update_daemon.py` | 数据同步/调度 | 数据更新入口 | 需确认是否被现行 daemon/run_daily 覆盖 |
| `ops/*.ps1` | 服务安装、启动和观测栈 | 部署/运维入口 | 不能只搜索 Python 调用 |
| `tests/**`、`scripts/**` | 故障演练和清理工具 | 非生产或验证入口 | 删除前需保留可审计演练能力 |

## 判定

当前不能删除 Celery。主要任务是确认 dashboard、daemon、run_services、dashboard_keepalive 是否同时管理同一服务，并收敛到一个生命周期管理者。`dashboard_keepalive.py` 是优先调查对象，但不能在确认外部计划任务前直接删除。

## 下一步

- 对每个入口记录启动命令、PID 文件、端口、日志和停止方式；
- 逐项检查 Windows 服务、计划任务和 `ops/*.ps1`；
- 决定 daemon 是否为唯一管理者，再处理 keepalive/run_services 的兼容层。
