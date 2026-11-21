# 宿主插件后端约定（实测源码 + 参照可用插件 todo-panel）

> 来源：`hermes_cli/web_server_dashboard.py:529-620, 1053-1060, 1233-1236`
> 参照：`C:\Users\Martin WANG\AppData\Local\hermes\plugins\todo-panel\dashboard\plugin_api.py`（能正常加载的现役插件）
> **这些是实现第四轮的硬事实，写错整个插件加载不了。**

---

## 1. 后端入口是 `handle()` 函数，不是 Flask/FastAPI 路由

宿主把 `plugin_api.py` 当**普通 Python 模块**导入（`plugin_isolation.import_user_code`），
然后调用模块里的 `handle(method, path, body)`：

```python
def handle(method: str, path: str, body: dict | None) -> dict:
    if path in ("/overview", "/overview/") and method == "GET":
        return overview()
    if path == "/tasks/toggle" and method == "POST":
        return toggle_subtask((body or {}).get("id"), bool((body or {}).get("done", True)))
    return {"ok": False, "error": f"未知路径 {method} {path}"}
```

**没有装饰器、没有 app 对象、没有 `@route`。** 调用点：
```python
# web_server_dashboard.py:1053-1060
host, dashboard_dir, api_file = target
... name, dashboard_dir, api_file, request.method, "/" + path, query, headers, body ...
```

**返回值**：一个 `dict`，序列化成 JSON 返回给前端。错误用 `{"ok": False, "error": "..."}`。

**路径**：`path` 已去掉前缀，形如 `/overview`（带前导斜杠）。**建议同时容忍带尾斜杠的写法**
（`path in ("/overview", "/overview/")`），因为前端拼接规则不完全可控。

## 2. `plugin.yaml` 的 `api` 字段

```yaml
api: dashboard/plugin_api.py     # 相对插件根目录（不是相对 dashboard/）
```

源码 `_safe_plugin_api_relpath(api_field, dashboard_dir=dashboard_dir)`：
- 必须**相对路径**
- 解析后必须**仍在插件目录内**（拒绝绝对路径与 `../` 越界，GHSA-5qr3-c538-wm9j）
- 越界时：插件静态资源照常加载，但**后端 api 被拒绝挂载**（只打 warning，不报错 —— 静默失效）

⇒ 我写的 `api: dashboard/plugin_api.py` 正确（与 todo-panel 一致）。

## 3. 三栏界面不是后端的事

`plugin.yaml` 里可声明 `tab:`（`path` / `position` / `hidden` / `override`）与 `slots:`，
前端组件通过 `window.registerSlot(pluginName, slotName, Component)` 注册。
**后端只管数据，布局在 JS 侧。**

## 4. 编写硬要求（基于以上）

| 要求 | 说明 |
|---|---|
| 不得引入 Flask / FastAPI / aiohttp | 宿主不跑你的 web server，它只调 `handle()` |
| `handle()` 必须**纯函数式**：不启动线程、不监听端口 | 每次请求被调用一次 |
| 未知路径返回 `{"ok": False, "error": ...}`，**不抛异常** | 抛异常会让整次请求 500 |
| 写操作必须自己检查 `expect_mtime`，不一致返回 **`{"ok": False, "code": 409, "error": ...}`** | 宿主不知道你的并发语义 |
| `fresh` 越界归类返回 **`{"ok": False, "code": 403, "error": ...}`** | 同上 |
| 所有耗时操作**不要阻塞超过几秒** | 前端 4 秒轮询一次 `/overview` |

## 5. 本轮任务书里那 8 个"端点"怎么落地

第四轮任务书列了 8 个 REST 端点。**照上表把它们写成 `handle()` 里的分支**，不要写成路由注册。
路径与语义不变，只是载体从"路由装饰器"换成"if 分支"。

```python
def handle(method, path, body):
    body = body or {}
    route = (method.upper(), path.rstrip("/") or "/")
    if route == ("GET", "/overview"):         return overview()
    if route == ("POST", "/tasks/toggle"):    return toggle(body)
    if route == ("POST", "/tasks/add"):       return add_task(body)
    if route == ("POST", "/tasks/update"):    return update_task(body)
    if route == ("POST", "/sessions/assign"): return assign_session(body)
    if route == ("POST", "/sessions/new"):    return new_session(body)
    if route == ("POST", "/setup/init"):      return setup_init(body)
    if route[0] == "DELETE" and route[1].startswith("/tasks/"):
        return delete_task(route[1].rsplit("/", 1)[-1])
    return {"ok": False, "error": f"未知路径 {method} {path}"}
```

**测试方式**：直接 `import plugin_api; plugin_api.handle("GET", "/overview", None)`，
不需要起 HTTP server。这比起 server 测更可靠，也更符合宿主真实调用方式。
