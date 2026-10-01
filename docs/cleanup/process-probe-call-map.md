# 进程检测调用图

## 扫描结果

排除 `_archive/`、`data/`、`logs/` 后，关键词命中 105 条。复核命令：

```powershell
rg -n "_proc_alive|proc_alive|OpenProcess|GetExitCodeProcess|STILL_ACTIVE|ERROR_ACCESS_DENIED" `
  --hidden --glob '!.git/**' --glob '!_archive/**' --glob '!data/**' --glob '!logs/**' .
```

## 调用关系库存

| 调用方 | 依赖 | 语义 | 动作 |
| --- | --- | --- | --- |
| `src/proc_alive.py` | Windows API / `os.kill` | `probe` 三态、`alive` 二值包装 | 权威实现，保留 |
| `src/daemon.py::_proc_alive` | `proc_alive.alive` | 生命周期看护，未知按存活 | 兼容薄包装，保留 |
| `src/dashboard.py::_proc_alive` | `proc_alive.alive` | 面板状态二值展示 | 兼容薄包装，保留 |
| `src/flow_watchdog.py::_default_proc_alive` | `proc_alive.probe` | 三态健康判定 | 有意适配器，保留 |
| `src/dashboard_keepalive.py::_proc_alive` | 本地 ctypes/OpenProcess | 生命周期看护 | 改为委托 `alive` |
| `src/run_services.py::_proc_alive` | 本地 ctypes/OpenProcess | 服务启停 | 改为委托 `alive` |
| `tests/test_proc_alive.py` | 权限、退出码和三态行为 | 回归契约 | 保留并扩展 |

## 判定

真实重复实现是 `dashboard_keepalive.py` 和 `run_services.py`。不能把所有调用统一成 `probe(pid) is True`：`None` 代表无法判定，生命周期逻辑必须通过 `alive(pid)` 默认按存活处理；健康告警才使用 `probe(pid)` 保留三态。

`daemon.py`、`dashboard.py` 的 `_proc_alive` 虽然仍有函数名，但已经是兼容包装，且现有测试依赖 daemon 的调用形态，不应在本阶段强行删除。

## 下一步

逐文件替换两个独立 ctypes 实现，每次修改后运行：

```powershell
.venv314\Scripts\python.exe -m pytest tests/test_proc_alive.py -q --basetemp .pytest_tmp_proc
.venv314\Scripts\python.exe -m pytest tests -q -k "keepalive or run_services or daemon" --basetemp .pytest_tmp_proc
```
