# valuation 主表重建流程（Runbook）

> 适用对象：`data/h5i/market.db` 的 `valuation` 表（约 1540 万行）。
> 目的：当逐日入库漏行、近似行（`source='approx_pb_rebuild'`）补位且 `pe_ttm` 为空时，
> 重算近似行的 PE(TTM) 并重建主表，使 `ep` 因子（`pe_ttm` 的来源）恢复可用。

## 何时需要执行

| 触发条件 | 判据 |
|---|---|
| 哨兵报 CRITICAL | `scripts/valuation_coverage_sentinel.py` 退出码 1（如 `pe_ttm` 覆盖率 < 50%） |
| 主表 PE 覆盖率下滑 | 近 12 个交易日 `pe_ttm` 中位覆盖率 < 50% |
| 因子层失真 | `factor_fusion` 合并补丁后仍有个股 `ep` 大面积缺失 |
| 首次建立 / 环境迁移 | 新库首次回填 |

**为什么需要**：`valuation_backfill` 的近似行只补 `pb`（`close / bvps_pit`），
`pe_ttm` 按设计置空 ⇒ 那段窗口 `ep` 因子整体失效。逐日入库一旦漏行，近似行就会顶上来，
缺口静默产生。

## 0. 前置检查（约 5 分钟）

```powershell
# ① 确认没有本仓库的作业在读写该库(其他应用的只读消费者无妨)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId,CommandLine | Format-List

# ② 磁盘余量应 >= 库体积的 2 倍
Get-PSDrive D | Select-Object @{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}}
Get-ChildItem 'data\h5i\market.db' -Recurse -File | Measure-Object -Property Length -Sum

# ③ 记录重建前状态(用于事后对照)
python <work>\val_state.py '重建前 PRE'
```

## 1. 备份（整库复制，最稳）

```powershell
robocopy 'data\h5i\market.db' 'data\backup\market.db.<YYYYMMDD_HHMM>' /E /NFL /NDL /NJH /NJS /R:1 /W:1
# 校验：文件数与总字节数必须与源逐项一致
```

- robocopy 退出码 **0~7 均为成功**，8 以上才是失败。
- 备份目录放在 `data/backup/`，**不要**放在 `data/h5i/` 下（避免被客户端当成库）。

## 2. 预检

```powershell
python src\valuation_backfill.py check
```

⚠️ **已知误报**：`row_count` 会报 `ok=false`，因为该断言假设表内还没有近似行，
而实际已有 130 万行。只要 `sample_*` 三个抽样日与 `dup_symbol=0` 通过即可继续。

## 3. 执行（两阶段，只读→只写）

```powershell
# 阶段一：重算近似行到 parquet（不触碰数据库，可安全重跑）
python src\valuation_backfill.py approx-build            # -> data/approx_pb_rows.parquet

# 阶段二：重建主表（剔除全部旧近似行 + 重灌 parquet + 按 (ts,symbol) 重建）
python src\valuation_backfill.py rebuild --approx data\approx_pb_rows.parquet
```

- 窗口由 `WINDOW_LO`（2025-08-04）与 `WINDOW_HI`（**默认今天**，可用环境变量
  `VB_WINDOW_HI` 覆盖）决定；`WINDOW_HI` 必须是动态的，否则重建会丢掉窗口外的近似行。
- 阶段一约 8~15 分钟（254+ 个交易日），阶段二约 30~60 秒（分块 append）。
- 重建成功会自动写版本戳 `data/pit/valuation_build.json`。

## 4. 执行后验证（10 分钟）

| 检查项 | 方法 | 预期 |
|---|---|---|
| 表行数 | `val_state.py` | ≥ 重建前（旧近似行 136.1 万 → 新 136.7 万） |
| `source` 分布 | `val_state.py` | `calculated_from_financials` **逐位不变**（14,037,803） |
| 重复行 | `rebuild` 输出 `dup_removed` | `0` |
| PE 覆盖率 | `val_state.py` 的 `recent_pe_rate` | 从 ~19% 升到 ~70% |
| 哨兵 | `python scripts\valuation_coverage_sentinel.py` | CRITICAL 0 |
| 版本戳 | `data/pit/valuation_build.json` | 存在、时间戳为本次 |
| 缓存自动失效 | 打印 `vnpy_backtest._pit_cache_path('2024-07-03', 20)` | 文件名携带**新** `_d<ver>` |

## 5. 回滚（仅当验证不通过）

```powershell
Remove-Item 'data\h5i\market.db' -Recurse -Force
Copy-Item 'data\backup\market.db.<YYYYMMDD_HHMM>' 'data\h5i\market.db' -Recurse
python <work>\val_state.py '回滚后'   # 行数/source 分布应与备份时一致
```

## 6. 已知坑（都已在代码中修复，重跑前确认未回归）

1. **`WINDOW_HI` 硬编码**曾导致重建**丢失窗口外近似行**（实测少 54,978 行，含 PB 覆盖）。
   现已改为默认"今天"。⇒ 重建前确认 `WINDOW_HI` 是动态的。
2. **补行门槛把近似行自己算进去**曾使 `fill_days` 变空集、**一行都不生成**
   （重建静默无效）。现已只统计 `source <> 'approx_pb_rebuild'` 的**真实行** ⇒ 幂等。
3. **`check` 的 `row_count` 误报**（见第 2 节），不要据此中止流程。
4. **缓存无数据版本**曾使重建后仍命中旧选股结果。现已把补丁文件哈希 + 版本戳
   一并纳入 `vnpy_backtest._data_version()`。

## 7. 两条交付路线的等价性（已验证）

供因子层取数有两条路：**主表自算**（本流程）与**补丁合并**（`factor_fusion._pe_patch_all`）。
2026-09-14 实测（台账第 26 条）：两条路线在组合级**完全等价**——同一配置下影子回测
`rb` 与 `pefix` 2 位小数相同、0/11 窗口差异；格子级差异中位 2.59%，对组合无影响。
因此：主表可用时优先主表自算，补丁作为兜底安全网保留。
