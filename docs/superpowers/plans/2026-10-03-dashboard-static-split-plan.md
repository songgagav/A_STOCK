# Dashboard 静态资源拆分实现计划

Spec: `docs/superpowers/specs/2026-10-03-dashboard-static-split-design.md`

## Global constraints

- 不改变 `/`、`/index.html`、`/api/*` 的响应语义。
- 不修改冻结、PaperBook、交易和数据读取逻辑。
- 不引入第三方依赖。
- 保留现有 `PAGE` 名称作为兼容别名，但来源改为模板缓存。
- 不触碰工作区中用户已有的文档和未跟踪文件。

## Task 1: 静态资源契约测试（先 RED）

新增 `tests/test_dashboard_static.py`，以可测试的资源加载/路由辅助函数或
`Handler` 请求为目标，覆盖 CSS/JS 200、MIME、白名单 403、缺失 404、编码穿越、
规范化合法路径、模板缺失异常、页面无内联资源等契约。

Expected: 新测试在生产实现尚未加入时失败，且失败原因是缺少静态资源加载/路由契约。

## Task 2: 资源机械提取与模板缓存

从现有 `PAGE` 原样提取：

- `templates/dashboard.html`
- `static/dashboard.css`
- `static/dashboard.js`

HTML 使用 `/static/dashboard.css` 和 `/static/dashboard.js`，不引入视觉变化。
`dashboard.py` 基于 `Path(__file__).resolve().parent.parent` 启动时读取模板，缺失时
抛出带绝对路径的 `FileNotFoundError`。

Expected: Task 1 的资源/模板测试进入 GREEN；模块导入和 `/` 页面可读。

## Task 3: 静态路由与 ETag

在 `Handler` 增加 `/static/` 路由：URL 解码一次、URL 级规范化、硬编码白名单、
`Path.is_relative_to()` 边界检查、CSS/JS MIME、mtime_ns+size ETag、304 和 no-cache。

Expected: Task 1 全部通过，现有 dashboard API 契约不变。

## Task 4: Commit 1 验证与提交

运行模块导入、资源烟测、dashboard 定向测试和 `git diff --check`，确认没有视觉/接口
之外的改动；提交资源拆分实现，不包含测试文件。

## Task 5: Commit 2 测试提交

在 Commit 1 之后再次运行测试，单独提交 `tests/test_dashboard_static.py`。

## Task 6: 全量回归与自审

运行 `.venv314` dashboard 定向测试、`.venv310` 兼容测试和全量 pytest；检查差异、
路径穿越、资源 MIME、未跟踪文件隔离。最终做一次独立于实现过程的自审。

## Review focus

- HTML 是否仍包含内联 `<style>` 或内联 `<script>`。
- 静态路由是否可能暴露白名单外文件。
- URL 编码与 Windows 反斜杠是否绕过路径检查。
- 模板缺失是否在启动/导入阶段失败。
- ETag 命中是否真的返回 304 而不是重新发送内容。
- API 与现有页面数据行为是否保持不变。
