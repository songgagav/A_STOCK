# 运维 Runbook

本文件描述本仓的本地/纸面盘运行方式。当前策略只允许 `PaperBook`，不接入真实券商。

## 运行时选择

| 环境 | 用途 | h5i 主数据 |
|---|---|---|
| `.venv310` | 生产守护、盘中引擎、看板、h5i/DRL 任务 | ✅ |
| `.venv314` | 通用开发与部分测试 | ❌，运行 h5i 看板会返回数据源不可用 |

生产入口必须使用 `.venv310`。`src/run_services.py` 会优先选择该解释器，
`ops/start_daemon.ps1` 也默认选择该解释器；`TRAE_PYTHON` 可显式覆盖。

## 启动前检查

```powershell
cd D:\狗屁通のA大奇妙冒险\A_stock_rotation
pwsh -File ops/start_daemon.ps1 -CheckOnly
```

检查通过后启动守护：

```powershell
pwsh -File ops/start_daemon.ps1
```

仅启动看板：

```powershell
$env:BAR_STORE = 'h5i'
.venv310\Scripts\python.exe src\dashboard.py --port 8000
```

服务接口核验：

```powershell
Invoke-WebRequest http://localhost:8000/api/health
Invoke-WebRequest http://localhost:8000/api/marketboard
Invoke-WebRequest 'http://localhost:8000/api/abnormal?limit=5'
```

`marketboard` 和 `abnormal` 应返回 `ok=true`；若出现 `h5i 不存在`，先确认当前进程
不是由 `.venv314` 启动，再检查：

```powershell
.venv310\Scripts\python.exe -c "import h5i_db; print('h5i ok')"
```

## 停止与重启

```powershell
.venv310\Scripts\python.exe src\run_services.py stop
.venv310\Scripts\python.exe src\run_services.py start
```

若端口仍被旧的高权限进程占用，用管理员 PowerShell 结束实际监听 PID 后再启动；
不要反复猜测旧 PID。

## 常见故障

### 1. `h5i 数据源不可用`

这是解释器/依赖问题，不是空数据。使用 `.venv310` 重启看板，并确认 `data/h5i/market.db`
存在。不要把 `.venv314` 的结构化错误当成市场无数据。

### 2. 守护启动被拒绝：行情引擎落后

`ops/start_daemon.ps1 -CheckOnly` 会拒绝落后于最后已收盘交易日的 `stockdb.exe`。
Phase E 观察期不得使用 `-AllowStaleEngine` 绕过该门禁；应先刷新上游行情引擎。

### 3. LLM 点评超时

先检查 Ollama 的 `/api/tags`、`/api/ps` 和最小对话请求。服务可达不等于模型已完成推理；
没有生成 `data/daily/<YYYYMMDD>/llm_commentary.json` 就不能标记点评成功。

### 4. 看板加载旧页面

看板模板在进程启动时缓存。修改模板后必须重启看板，再用 `Ctrl+F5` 强制刷新浏览器。

## Phase E 观察前置

只允许 shadow：

```powershell
.venv310\Scripts\python.exe -c "import sys; sys.path.insert(0, 'src'); from signal_snapshot import read_mode_control; print(read_mode_control('.'))"
```

预期 `mode=shadow`；`TRADE_BROKER` 未设置时按项目默认值 `paper` 处理。
观察期间不切换 enforce、不接实盘、不手动补写快照。
