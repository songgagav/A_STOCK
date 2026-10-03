# Dashboard 静态资源拆分设计

日期：2026-10-03  
状态：设计已确认，待实现计划评审  
范围：仅拆分资源，不改变 API、数据语义或冻结行为

## 1. 目标与非目标

### 目标

- 将 `src/dashboard.py` 中的单页 HTML、CSS、JavaScript 外置。
- 保持 `/`、`/index.html`、所有 `/api/*` 的现有行为。
- 通过受限静态路由提供 `dashboard.css` 和 `dashboard.js`。
- 为资源请求增加 ETag，避免重复传输。
- 保留标准库实现，不引入 Jinja2、前端构建工具或新的运行时依赖。

### 非目标

- 不修改 API 响应结构。
- 不修改 09:25 快照、shadow、PaperBook 或交易逻辑。
- 不在本 PR 做视觉增强、增量 DOM 或 SSE。
- 不保留旧内联 HTML/CSS/JS fallback。

## 2. 页面与资源边界

当前只有一个主页面：

- `/` 和 `/index.html` 返回同一 dashboard 页面。
- 其它未知路径仍返回原有 `404` 文本响应。
- 页面数据全部由浏览器通过现有 API 获取，模板不包含服务端变量注入。

目标目录：

```text
templates/
└── dashboard.html

static/
├── dashboard.css
└── dashboard.js
```

模板和静态资源位于仓库根目录，不属于 `src/` Python 包。

## 3. 路径与模板加载

仓库根目录必须基于模块位置计算，不依赖进程当前工作目录：

```python
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "templates" / "dashboard.html"
STATIC_ROOT = REPO_ROOT / "static"
```

模块启动时读取模板一次并缓存。模板不存在时立即抛出：

```text
FileNotFoundError: Dashboard template not found: <absolute path>
```

不在首次 HTTP 请求时延迟失败。`PAGE` 可保留为兼容别名，但内容来自模板缓存。

## 4. 静态路由安全约定

只允许显式登记的资源：

```python
ALLOWED_STATIC = {"dashboard.css", "dashboard.js"}
```

不动态扫描 `static/` 目录。未来新增资源必须显式加入白名单。

请求处理顺序：

1. 从 `urlsplit(self.path).path` 取得原始路径。
2. 用 `urllib.parse.unquote()` 解码一次。
3. 拒绝 NUL 和 Windows 反斜杠路径分隔符。
4. 对相对路径做 URL 级规范化，得到 canonical 相对路径。
5. canonical 路径不在 `ALLOWED_STATIC` 时返回 403。
6. 将 canonical 路径解析为 `Path`，用 `Path.is_relative_to(STATIC_ROOT.resolve())` 做目录边界检查。
7. 白名单资源不存在或不是普通文件时返回 404。
8. 文件存在时返回资源内容和对应 MIME。

状态码约定：

| 情形 | 状态码 |
|---|---:|
| 不在白名单 | 403 |
| URL 路径穿越 | 403 |
| 白名单内但文件不存在 | 404 |
| 合法资源 | 200 |
| `If-None-Match` 命中 | 304 |

只注册以下 MIME：

- `dashboard.css`：`text/css; charset=utf-8`
- `dashboard.js`：`application/javascript; charset=utf-8`

其它后缀不通过静态路由暴露。

## 5. ETag 与缓存

每次资源请求使用 `os.stat()` 读取 `st_mtime_ns` 和 `st_size`，构造稳定的弱依赖 ETag，例如：

```text
"<mtime_ns_hex>-<size_hex>"
```

不使用进程内 ETag 缓存，不加全局锁。拆分阶段保留：

```text
Cache-Control: no-cache
```

这样开发环境仍会验证资源是否变化，同时命中 ETag 时返回 304，不重复传输内容。

## 6. 提交边界

### Commit 1：资源拆分

- 新增 `templates/dashboard.html`
- 新增 `static/dashboard.css`
- 新增 `static/dashboard.js`
- 修改 `src/dashboard.py`
- 删除 `PAGE` 内联 HTML/CSS/JS
- 增加静态路由、模板缓存、ETag 和安全检查
- 不混入视觉或 API 行为变化

Commit 1 的验证为手动烟测：模块导入、页面读取、CSS/JS 请求、API 路由抽查。

### Commit 2：契约测试

新增 `tests/test_dashboard_static.py`，覆盖：

1. CSS 200 与 MIME。
2. JS 200 与 MIME。
3. 白名单外资源 403。
4. 不存在的白名单资源 404。
5. `%2E%2E%2F` 编码穿越 403。
6. 多重斜杠/穿越路径 403。
7. `subdir/../dashboard.css` 规范化后的合法资源访问。
8. 缺失模板抛出带完整路径的 `FileNotFoundError`。
9. 页面无内联 `<style>` 和内联 `<script>`，且引用 `/static/dashboard.js`。

## 7. 回滚

- 仅测试失败：回滚 Commit 2，保留已手动验证的拆分。
- 拆分行为异常：回滚 Commit 1 和 Commit 2，恢复原内联页面。
- 不保留双路径 fallback，避免两套页面长期漂移。

## 8. 后续独立工作

拆分合并后另开视觉 PR，负责：

- 深色金融驾驶舱视觉语言。
- 冰青 ready、铜橙冻结、A 股红涨绿跌的 CSS 变量。
- 顶部冻结状态栏、迟到信号、数据新鲜度和 shadow 模式展示。

SSE、增量 DOM 和统一 API envelope 不属于本设计。
