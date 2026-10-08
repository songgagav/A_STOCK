# Bandit B608 第一批审计标注

审计日期：2026-10-02  
范围：本轮 `bandit.json` 中 126 条 B608；本批优先处理 Bandit 标为 `MEDIUM` confidence 的 34 条。只标注，不修改源代码，不批量添加 `# nosec`。

## 审计结论

Bandit 的 B608 是“SQL 字符串含动态片段”的启发式规则，不等价于已证实 SQL 注入。当前代码大多使用内部表名、白名单股票代码、日期或参数占位符，但仍有几类必须保留复核：

1. **动态标识符**：表名、列名、`SELECT` 字段列表、备份表名必须来自白名单或内部生成规则，不能直接接受外部字符串。
2. **动态值拼接**：日期、股票代码、路径等若不是参数绑定，需确认来源边界和格式校验。
3. **动态 SQL 片段**：`WHERE` 条件片段、列清单、占位符列表需要确认不会被任意调用方注入。

本批没有发现可以仅凭静态扫描直接“接受”的统一模式；因此全部保留为 `review_required`，不把“当前调用方看起来受控”写成安全证明。

## 优先复核组

### A 组：动态标识符/DDL，优先级高

- `src/cn_lake_feed.py:81`
- `src/dashboard.py:2340`
- `src/dashboard.py:2343`
- `src/dashboard.py:2345`
- `src/rebuild_symbols.py:70`
- `src/rebuild_symbols.py:71`
- `src/update_db.py:287`
- `src/update_db.py:298`

核对点：调用方是否只能传内部表名/列名；是否应集中到标识符白名单；备份表名是否只由内部日期生成。

### B 组：值拼接或 SQL 片段，优先级中高

- `src/health_check.py:101`
- `src/health_check.py:185`
- `src/health_check.py:192`
- `src/health_check.py:211`
- `src/health_check.py:223`
- `src/health_check.py:227`
- `src/agent_tools.py:411`
- `src/db.py:767`
- `src/db.py:818`
- `src/db.py:1134`
- `src/h5i_bar_store.py:260`
- `src/h5i_bar_store.py:268`
- `src/h5i_bar_store.py:289`
- `src/h5i_bar_store.py:300`
- `src/paper_book.py:1002`

核对点：日期/代码是否经过格式化和白名单校验；动态条件片段是否只来自内部固定模板；已经使用参数绑定的部分不能因为 B608 自动判为漏洞，但仍需确认片段本身的来源。

### C 组：同步/诊断/回填工具，优先级中

- `src/_cncheck.py:7`
- `src/_diag.py:28`
- `src/dashboard.py:58`
- `src/free_stockdb_sync.py:154`
- `src/free_stockdb_sync.py:328`
- `src/h5i_ingest.py:87`
- `src/local_pull.py:179`
- `src/local_pull.py:730`
- `src/local_pull.py:762`
- `src/rebuild_financials.py:135`
- `src/update_db.py:105`

核对点：这些入口是否只由内部任务调用；诊断工具是否可能被用户输入、环境变量或命令行参数传入未经校验的标识符。

## 后续批次

首批 34 条完成后，低置信度队列原有 92 条。2026-10-03 已完成第二批 30 条 dashboard、数据访问与 h5i 入口审计，详见 [`bandit-b608-low-2026-10-03.md`](bandit-b608-low-2026-10-03.md)；当前剩余 62 条。后续按实际入口优先：h5i 剩余查询 → 因子研究脚本 → 其余回测和迁移工具。每个批次只补审计标注和证据，不在没有单独修复任务时改 SQL。

## 当前状态

本文件是审计队列和风险标注，不是“全部安全”证明。B608 仍计入 Bandit 非零结果；若后续确认某条为误报，应记录具体输入约束和调用方，再决定是否添加局部、带理由的 `# nosec`。

