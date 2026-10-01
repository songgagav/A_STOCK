# 服务生命周期边界

记录日期：2026-10-02  
适用分支：`cleanup/phase0-baseline`

## 结论

当前不能删除 Celery，也不能把多个服务入口粗暴合并。Phase 3.1 已统一进程存活探测；本阶段只明确管理边界，避免同一服务被多个入口重复拉起或停止。

## 入口职责

| 入口 | 管理对象 | 责任边界 | 当前判断 |
| --- | --- | --- | --- |
| `src/daemon.py` | dashboard、realtime engine、观测栈、Celery worker/Flower | 主守护、启动/停止、健康编排 | 主管理入口，保留 |
| `src/dashboard_keepalive.py` | dashboard | 独立看护和异常恢复 | 与 daemon 有重叠，先核对计划任务 |
| `src/run_services.py` | dashboard、realtime engine | 手动启动/停止和兼容入口 | 保留，避免与 daemon 重复管理 |
| `src/tasks_db.py` | Celery task、数据更新任务 | 异步执行，不负责完整服务生命周期 | 保留，属于任务层 |
| `src/dashboard.py` | dashboard 内的查询、手动更新按钮 | Web 运维入口，不应自行成为主守护 | 保留，和任务层协调 |
| `src/data_update_daemon.py` | 数据同步/调度 | 数据更新专项入口 | 需继续确认是否被现行链路覆盖 |
| `ops/*.ps1` | 安装、启动、观测和计划任务 | 部署层入口 | 必须和 Python 入口一起审计 |

## 不同入口的边界

- `daemon.py` 负责服务生命周期编排；
- `dashboard_keepalive.py` 只负责看护时，不应与 daemon 同时拥有完整重启权；
- `run_services.py` 作为人工/兼容入口使用时，必须避免重复拉起同一 PID、端口或服务；
- `tasks_db.py` 负责异步任务执行，不等同于 Celery worker 的进程管理；
- `dashboard.py` 可以发起更新请求，但不应绕过任务层偷偷复制一套调度逻辑。

## 已完成的公共能力收敛

`src/daemon.py`、`src/dashboard_keepalive.py` 和 `src/run_services.py` 的进程存活判断已统一委托给 `src/proc_alive.py`。后续如果发现职责重叠，优先合并“进程探测”和状态报告，不直接删除服务入口。

## 后续确认清单

1. 列出每个入口的启动命令、PID 文件、端口、日志和停止方式；
2. 检查 Windows 服务、计划任务和 `ops/*.ps1` 是否仍调用 keepalive/run_services；
3. 明确 daemon 是否是唯一生命周期管理者；
4. 只有在外部入口迁移并通过回归后，才收敛重复的启动/停止代码。
