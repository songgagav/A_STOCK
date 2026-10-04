# 环境搭建

## 场景选择

| 场景 | 环境 | 说明 |
|---|---|---|
| 通用开发/测试 | `.venv314` | 运行大多数单元测试；不提供 `h5i_db` |
| 看板/行情/纸面盘 | `.venv310` | h5i 主数据源，生产推荐 |
| DRL 训练 | `.venv310` | 与 h5i_db、torch、gymnasium、stable-baselines3 同环境 |

## 创建环境

项目已提供受校验的 3.10 环境脚本：

```powershell
pwsh -File scripts/setup_py310_drl_venv.ps1
```

完成后验证：

```powershell
.venv310\Scripts\python.exe -c "import h5i_db, torch, gymnasium, stable_baselines3; print('runtime ok')"
```

不要把 h5i_db 强行加入 Python 3.14 的公共依赖文件；它是 CPython 3.10 原生扩展。

## 最小运行

```powershell
.venv310\Scripts\python.exe src\run_services.py start
```

若只需要看板：

```powershell
$env:BAR_STORE = 'h5i'
.venv310\Scripts\python.exe src\dashboard.py --port 8000
```

## 测试

```powershell
.venv314\Scripts\python.exe -m pytest tests -q --basetemp .pytest_tmp_setup
```

需要 h5i 的定向测试使用：

```powershell
.venv310\Scripts\python.exe -m pytest tests -q --basetemp .pytest_tmp_setup310
```

测试和运行产生的 `data/`、`logs/`、缓存以及本地临时文本不得提交。

## 生产前置

启动守护前运行：

```powershell
pwsh -File ops/start_daemon.ps1 -CheckOnly
```

该检查会验证解释器依赖、行情湖根目录、`kline_parts` 和 `stockdb.exe` 新鲜度。
检查失败时应修复上游或环境，不要用陈旧数据强行进入观察期。
