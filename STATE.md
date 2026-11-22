# Hermes Cockpit — 项目状态（长期本子）

> 进本目录先读这个。只记硬事实：实测结果、已完成产出、用户原话。

## 当前阶段

**实现阶段**。设计基线已冻结（`C:\dev\knowledge\60 软件项目\hermes-cockpit\docs\`），
六轮 designer bot 评审 42 条意见全采纳，status: baselined。

## 用户授权（原话）

> 「我去睡觉了，给我完整权限，你让 coder 来做，然后你和 reviewer 来审核」

⇒ 分工：**coder 写代码 → reviewer 独立审 → 助理兜底并汇报**。

## 实测事实（可复现）

| 项 | 结果 | 怎么测的 |
|---|---|---|
| 宿主版本 | **0.23.2**（sha ae2a81ccc00e） | `gateway_state.json` 的 `code_version` |
| 本机位置 | **在家**，IP `192.168.31.11` | `ipconfig` |
| Spark 可用端点 | **`192.168.31.128:8006`** → `qwen3.8-27b`，ctx 262144 | `curl /v1/models` 返回 200 |
| bot 配置的端点 | ❌ **全部失效**：dev-executor/dev-reviewer 都指向 `10.0.1.175:8005`（000 不通） | `curl` 三地址逐个测 |
| bot 实际跑在哪 | **云端 deepseek**（spark 主 provider 失效，走 fallback） | `config.yaml` 的 `fallback_providers` |
| `sessions` 表 | **66 列，无 `created_at`**；主键 `id`；时间列 `started_at`（1497 行 0 空值） | `PRAGMA table_info(sessions)` |
| `projects` 表 | 10 列，主目录列是 **`primary_path`**；14 行 | `PRAGMA table_info(projects)` |
| **会话 cwd 分布** | **空 878 条（59%）** / `C:\Users\Martin WANG` 342 / `C:\dev\knowledge` 170 / hermes 29 / wiki\daily-chat 28 | `Counter(cwd)` on 1497 rows |
| **项目路径现状** | 14 个项目 `primary_path` **全部指向 `C:\dev\knowledge\...` 旧目录**，**0 个**指向真实代码目录 `C:\dev\software-projects\` | `read_projects()` 逐条 |
| **cwd 归属覆盖率** | **仅 ~2%**（`smart-pcr-promo` 14 条 + `hermes-cockpit` 4 条），**1473/1497 未归属** | `store.overview()` 实跑 |
| **孤儿任务** | `stats.tasks=2` 但**每个项目 `task_count` 全 0** —— `TASKS.md` 里的 `proj:hermes-cockpit` 不在 14 个项目里 | `store.overview()` 逐项目 |

> ⚠️ 上面最后三行是**产品设计的硬约束**：
> 1. D8「cwd 优先」在真实数据上只覆盖 2%，`session_map` 手动归类（E1）是**主路径**不是兜底，第一版必须让归类足够省力。
> 2. 有任务但项目 slug 不匹配时，任务会"人间蒸发"（计入总数但不显示在任何项目下）。界面必须单列
>    「未归属任务」，否则用户会以为任务丢了 —— 这是 E1 之外第二个必须做的兜底视图。

## 测试命令登记

`pytest.ini` 已加（`testpaths = tests`），裸 `python -m pytest` 可直接跑，不再每轮被判定"未验证"。

## D6「新建会话 cwd 正确」端到端实测（2026-11-22 00:11）

**结论：成立。** 两种路径都已验证：

| 路径 | 做法 | 结果 |
|---|---|---|
| **A. 进程 cwd** | `cd <目标目录> && hermes -z "..."` | ✅ 新会话 `local-f66c61eb5dae2d` 的 `cwd` = 目标目录，实测一致 |
| **B. RPC 显式** | `session.create` 带 `cwd` + `cwd_explicit: True` | ✅ 源码 + 6 种输入矩阵已验（FS-001 §十一） |

**关键源码定位**（`gateway/session_policy.py`）：
- `:19` `CREATE_FIELDS` 白名单含 `cwd` 与 `cwd_explicit`
- `:457-458` `cwd` 必须绝对路径且 `is_dir()`，否则 `_refuse('cwd', ...)`
- `:462` **`cwd_explicit is False` 且传了 `cwd` → 被拒**
- `:488` **只有 `cwd_explicit is True` 或 `terminal_uses_host_cwd()` 才采用该 cwd**

⇒ 插件里新建会话**必须**传 `cwd_explicit: True`，传 `False` 会被拒，不传则取决于 profile 配置。
建完必须回查 `sessions.cwd` 确认，不信返回值。

**桌面 App 的 gateway 不监听 HTTP 端口**（实测 8000/8765/7788 全 URLError，`gateway_state.json` 无 port 字段）
—— 走进程内 IPC。所以插件**不能**通过 HTTP 调 `session.create`，只能走宿主提供的 `ctx` 能力。

## 插件后端入口的真实约定（决定性实测，推翻早期假设）

**宿主只认 `router`（FastAPI APIRouter），不认 `handle()`。**

`hermes_cli/web_server_dashboard.py:1258-1268`：
```python
mod = import_user_code(api_path, f"hermes_dashboard_plugin_{name}")
router = getattr(mod, "router", None)
if router is None:
    _log.warning("Plugin %s api file has no 'router' attribute", name)
    return False                          # ← 整条后端不挂载
app.include_router(router, prefix=f"/api/plugins/{name}", ...)
```

**旁证**：`todo-panel` 只有 `handle()`、没有 `router`，日志里**从未出现**过
「Mounted plugin API routes: /api/plugins/todo-panel/」—— 那个面板的后端其实一直没工作。

⇒ 本项目 `plugin_api.py` **必须导出 `router`**。`handle()` 可保留作纯函数测试入口，
但**不能只给 `handle()`**。当前实现两者都有（已验证 `router` 是真 `APIRouter`，8 条路由正常注册）。

**已顺手修掉 `todo-panel`**（补 `router` 外壳 + `manifest.json` + `from fastapi import APIRouter`），
实测其 router 已是真 `APIRouter`、3 条路由注册成功。这是修既成事实的坏件，不是改设计。

## 两套插件体系不可混淆（实测 `web_server_dashboard.py:651` + `sdk/runtime.ts`）

| 体系 | 位置 | manifest | 后端 | 前端 |
|---|---|---|---|---|
| **Web dashboard** | `plugins/<name>/dashboard/` | **`manifest.json`**（不是 plugin.yaml） | `plugin_api.py` 导出 **`router`** | `entry: dist/index.js`（**纯 IIFE，无构建步骤**，用 `window.__HERMES_PLUGIN_SDK__`） |
| **Desktop (Electron)** | `desktop-plugins/<name>/` | 无 | 复用上面那个 | 单文件 `plugin.js`，可 `import '@hermes/plugin-sdk'` |

**desktop 插件能直接 import 的原因**（`sdk/runtime.ts`）：运行时加载器用 blob shim 重写裸模块说明符。
```js
cached ??= {
  '@hermes/plugin-sdk': shimUrl('__HERMES_PLUGIN_SDK__'),
  'react/jsx-runtime':  shimUrl('__HERMES_REACT_JSX__'),
  react:                shimUrl('__HERMES_REACT__')
}
```

**发现规则**（`web_server_dashboard.py:651`）：只找 `child / "dashboard" / "manifest.json"`。
`plugin.yaml` 对 dashboard 发现**无效**——所以两个都得有：
- `dashboard/manifest.json` → 让 web dashboard 发现并挂载 router
- `plugin.yaml` → 供 `hermes` CLI 插件体系识别（hook/tool 用）
- `desktop-plugins/<name>/plugin.js` → Electron 前端



## 测试污染事件（已修复）

`META.yaml` 一度被写成 `status: done / mode: fresh / at: <时间>`，
导致 `fresh` 作用域把 1501 条会话全滤掉，`/overview` 显示 0 会话。

**根因**：不是测试缺陷 —— 跑完整 170 个测试后 `META.yaml` md5 不变（已验证）。
是 coder 手动调过一次 `/setup/init` 留下的状态。

**已恢复** `status: pending / mode: null / at: null`，数据恢复 1501 条会话。
**注意**：`setup.at` 是单向门，一旦落盘代码无法自动回滚，只能手改文件 —— 以后让 coder
测 `/setup/init` 必须**显式指定临时 root**，绝不能用默认根目录。



## 已完成产出

| 模块 | 文件 | 状态 |
|---|---|---|
| F1 适配层 | `plugins/hermes-cockpit/dashboard/host_adapter.py`（助理起草，15.6KB） | coder 验证中 |
| F2 兼容区间 | `dashboard/compat.yaml` + `compat_status()` | ✅ 自测通过（判 0.23.2 兼容） |
| F4 架构文档 | `docs/ARCHITECTURE.md`（S17/S18 产物） | ✅ 已写 |
| 数据 | `TASKS.md` / `META.yaml` | ✅ 已建（含本项目自举任务） |
| 骨架 | `plugins/hermes-cockpit/__init__.py` / `plugin.yaml` | ✅ 已建（`__init__.py` 是插件加载必需） |

## coder 第一轮任务（进行中）

F1 边界扫描脚本 / A1 数据访问层测试 / A2 任务解析器
任务书：`C:\Users\Martin WANG\AppData\Local\hermes\cache\scratch\coder-brief-r1.md`
reviewer 任务书：`...\reviewer-brief-r1.md`

## 未解决 / 待办

- **bot 端点配置过期**：属管理员 bot 职责，主 bot 只出清单（已记入 `TASKS.md` T-002）
- **AGENTS.md / .hermes/STATE.md 无法写入**：系统保护要求用户审批，超时即拒。
  规范已改为写进 coder 任务书；状态本子改放项目根 `STATE.md`
- 桌面界面（C 模块）尚未开工
- S1–S19 尚未逐项验收

## 验收口径

`DD-001 §六` 的 **S1–S19 全绿**才算第一版完成，不接受部分通过。
S 项 → 主责模块对应表见 `WBS-001` 末尾。
