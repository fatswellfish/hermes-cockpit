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
| **F1** 适配层 | `dashboard/host_adapter.py` + `scripts/check_boundary.py` | ✅ 9 文件零越界，负向对照有效 |
| **F2** 兼容区间 | `dashboard/compat.yaml` + `compat_status()` | ✅ 判 0.23.2 兼容；区间外降级只读且有原因 |
| **F4** 架构文档 | `docs/ARCHITECTURE.md`（S17/S18） | ✅ 已写 |
| **A1** 数据访问层 | `host_adapter` 列名探测 + `mode=ro` | ✅ 66 列实测，`started_at` 非 `created_at` |
| **A2** 任务解析器 | `core/taskfile.py`（parse / serialize / write_back） | ✅ 块外逐字节保留、折行保持、3 次写回字节稳定 |
| **A3** 聚合视图 | `dashboard/store.py` `overview()` | ✅ 真实数据 14 项目 / 1503 会话 / 1479 未归属 |
| **A4** 并发写 | `dashboard/fileio.py` mtime + 409 | ✅ 两进程竞争一成功一 409 |
| **B1-B4** 写操作 | `plugin_api.py` 8 条路由 | ✅ 含 `expect_mtime` 闭环（不带戳一律 428） |
| **C1-C5** 桌面界面 | `desktop-plugins/hermes-cockpit/plugin.js` | ✅ 三栏 + 5 个注册点，渲染冒烟 11/11 |
| **D1** 配置结构 | `META.yaml` | ✅ 六个顶层块，`setup.at` 单向门 |
| **D2** 首启引导 | 后端 `POST /setup/init` | ⚠️ **只有后端，前端无引导界面 → 见 GAP-01** |
| **E1** 归属解析 | `store.py:55/60/240` | ✅ `manual > cwd 命中`，无归属返回 None |
| **E2** 系统提示注入 | `core/inject.py` + adapter `register_system_prompt_section` + `__init__.py::register` | ✅ 注册/渲染/6 条硬要求全绿（细节见下节） |
| **E3** 跑题提醒 | `core/drift.py` + skill 文本 | 🔄 不受本轮影响，按原任务书做 |

## E2 落地实录（第六轮裁定后，2026-11-22 22:xx 实测）

**三处改动，边界不破：**
- `plugin.yaml` 的 `name` 是 `Hermes Cockpit` → 宿主取 **name 当 registry key**，
  所以 `hermes plugins enable hermes-cockpit`（目录名）报 `No plugin named`；
  正确写法是 `hermes plugins enable "Hermes Cockpit"`。
- `__init__.py::register(ctx)` 只转调 `host_adapter.register_system_prompt_section`；
  判定逻辑在 `core/inject.py`（纯函数，数据全经回调注入）。
- `check_boundary.py` 新增 `HOST_REGISTER` 类型 + 一处**精确豁免**：
  只认插件 `__init__.py` 里的 `def register(ctx)` 与 `_adapter().register_*` 转调行；
  直接写 `ctx.register_*` 仍违规，`on_session_start`/`register_hook` 禁令不变。

**宿主侧实测事实（0.23.2）：**
| 事实 | 证据 |
|---|---|
| 通道签名 | `plugins_context.py:783` `register_system_prompt_section(id, content: str|Callable, position="after_memory", max_chars=4000)` |
| id 固定 | 重复 id 抛 `ValueError`；实测用固定 `hermes-cockpit.project` |
| 渲染点 | `agent/system_prompt.py:803`，after_memory → volatile 段 |
| 空串 | 返回空串的 section 被**丢弃**（`_render_prompt_section_text`），不会留空标题 |
| 超长 | 超 `max_chars` 的 section **整段丢弃**（不是截断）⇒ 自己在 80 字内卡住 |
| 生效时机 | enabled 变更**不需要重启 App**：`reload_gateway_plugins()` 控制 socket 重发现即可；段落对**下一个新会话**生效 |
| 系统提示落盘 | `sessions.system_prompt` 全表 0 非空（1507 行）⇒ **不能用它当注入证据**，证据只能用会话输出 |

**证据（真会话原文，22:xx）：**
```
## Plugin Context: hermes-cockpit.project
<!-- hermes-plugin-section-chars:56 -->

Hermes Cockpit 已接入本项目。（探针：…）
```
（探针文本随后已换成真判定逻辑；现真实数据下按设计返回空串，见下）

**当前真实数据下 E2 = 空串（设计使然，不是 bug）：**
- `META.yaml` 的 `reminders.enabled: false`（D7 默认关）；
- 即使打开：14 个项目的 `primary_path` 全在 `C:\dev\knowledge\...`，
  `C:\dev\software-projects\hermes-cockpit` 等真实代码目录**一个都命中不了**；
  `session_map` 里的 `demo` 在宿主项目表里不存在 ⇒ 也不归属。
- 要看到段落需 `scripts/probe_e2_inject.py --apply`（临时开开关→真会话→还原，默认干跑）。

**测试基线**：`py -3.10 -m pytest`（本机 `python` 是 3.14 **没装 pytest**；
只有 `Programs\Python\Python310` 有 pytest 9.2.0 与 fastapi）。

## 已知缺口（不自己改，派给 coder）

### GAP-01【阻塞使用】D2 首启引导没有前端入口

- 后端 `POST /setup/init` 已实现（`plugin_api.py:350/454/595`）
- 前端 `plugin.js` **只有「只读（引导未完成）」提示，没有「从零开始 / 导入已有」选择界面**
- 后果：用户打开面板看到只读灰按钮，**没有任何操作能完成引导** → D12 决策不可达
- 判定依据：`grep -n "setup|引导|从零开始|导入已有" plugin.js` 只命中提示文案，无交互元素

### GAP-02【阻塞使用】装到宿主后任务全空 —— 数据目录错位

**实测证据**（同一份代码，两个位置跑出不同结果）：

| 运行位置 | tasks | orphan_tasks | stamp |
|---|---|---|---|
| 项目目录 `C:\dev\software-projects\hermes-cockpit` | 有 | 有 T-001/T-002 | 有 |
| 宿主副本 `AppData\Local\hermes\plugins\hermes-cockpit` | **0** | **[]** | **null** |

**根因**：`TASKS.md` / `META.yaml` 只在项目目录存在，宿主副本里没有这两个文件
（`ls` 确认 both 不存在）。插件按自身位置找数据 → 装到哪就找哪 → 宿主里找不到 → 任务全空。
我当时的安装方式 `cp -r` 整个目录，把代码和数据混在一起搬，这是错的。

**修法方向**（派给 coder，不自行改）：
- 插件启动时先读 `plugin.yaml` 的 `data_dir`，读不到再回落项目目录
- 或 `TASKS.md` / `META.yaml` 缺失时**自动初始化**（而非静默返回空）
- **必须**补测试：在「无 TASKS.md 的临时目录」下启动，断言**报错或自动初始化**，不许静默返回 0

## 插件启用状态（2026-11-22 实测）

```
$ hermes plugins list
│ Hermes Cockpit │ enabled │     ← 本会话内启用
│ todo-panel     │ enabled │
```

- **CLI 认的名字是 `Hermes Cockpit`（带空格大写，取自 plugin.yaml 的 name），不是 `hermes-cockpit`** ——
  用目录名 `hermes plugins enable hermes-cockpit` 会报 `No plugin named`
- 启用后宿主提示「Gateway reloaded plugins; nothing of this plugin needs a session or restart」，
  **不需要重启 App**
- `config.yaml:104-114` 的 `plugins.enabled` 白名单是权威列表，新增插件必须加进去

## 已实测验证的宿主链路（启用后）

| 环节 | 结果 | 怎么测的 |
|---|---|---|
| 插件被发现 | ✅ | 宿主 `_discover_dashboard_plugins()` 返回含 `hermes-cockpit` / tab `/cockpit` |
| `router` 存在 | ✅ 真 `APIRouter` | `importlib` 加载后 `getattr(mod,"router")` |
| 路由注册 | ✅ **8 条** | `FastAPI().include_router(router, prefix="/api/plugins/hermes-cockpit")` |
| 后端取数 | ✅ 14 项目 / 1508 会话 / 1484 未归属 | `handle("GET","/overview",None)` 直调 |
| 桌面 App 内视觉 | ❌ **未验** | 需人工看界面 |


## 测试基线（每次交付都要重跑，四条各自独立裸跑）

| 命令 | 当前 |
|---|---|
| `py -3.10 -m pytest` | **215 passed / 1 skipped / 33 subtests**（E2 新增 37 条） |
| `node desktop-plugins/hermes-cockpit/__check.mjs` | 16/16 |
| `node desktop-plugins/hermes-cockpit/__render_check.mjs` | 11/11 |
| `py -3.10 scripts/check_boundary.py` | exit 0（10 文件） |

> ⚠️ 不许把四条拼进 `&&` 链再 `tail` —— 那样拿到的是 `tail` 的退出码，会假绿。
> ⚠️ 裸 `python` 是 3.14，**没装 pytest**：直接 `python -m pytest` 会 `No module named pytest`。
> 必须用 `C:\Users\Martin WANG\AppData\Local\Programs\Python\Python310\python.exe`。



## coder 第一轮任务（进行中）

F1 边界扫描脚本 / A1 数据访问层测试 / A2 任务解析器
任务书：`C:\Users\Martin WANG\AppData\Local\hermes\cache\scratch\coder-brief-r1.md`
reviewer 任务书：`...\reviewer-brief-r1.md`

## C 模块（桌面三栏）第五轮产出（2026-11-22 11:1x）

**文件**：`desktop-plugins/hermes-cockpit/plugin.js`（819 行）+ `__check.mjs` + `__render_check.mjs`

**后端 `/overview` 扩了 4 个字段**（前端要用，改了就得同步 `tests/test_store.py` 的字段集合断言）：
`projects[].path` / `assigned_sessions[]`（带 project 与 via: cwd|manual）/ `orphan_tasks[]` /
`stats.{unassigned_sessions, assigned_sessions, orphan_tasks}`。
`session_map` 人工归类现在**真的生效**（manual > cwd），此前只是写进 META.yaml 没人读。

**实测数据**：14 项目 / 1503 会话 / 24 已归属 / 1479 未归属 / 2 任务（均为孤儿）/ 14 看板，payload 301KB。

**验证**：`pytest` 173 passed · `check_boundary.py` exit 0 · `__check.mjs` 14/14 · `__render_check.mjs` 11/11。

**★ 桩测试的三个坑（下次别重踩）**：
`module.register` 的 loader 跑在独立 worker，① 拿不到主线程 globalThis；② `{data}` 走 structuredClone，函数传不过去、普通对象还会退化；③ 生成的桩模块是独立作用域，引用 loader 变量即 ReferenceError。
⇒ 桩源码必须**自包含**（每个导出直接内联函数体）。

## 未解决 / 待办

- **bot 端点配置过期**：属管理员 bot 职责，主 bot 只出清单（已记入 `TASKS.md` T-002）
- **AGENTS.md / .hermes/STATE.md 无法写入**：系统保护要求用户审批，超时即拒。
  规范已改为写进 coder 任务书；状态本子改放项目根 `STATE.md`
- 桌面界面（C 模块）尚未开工
- S1–S19 尚未逐项验收

## 验收口径

`DD-001 §六` 的 **S1–S19 全绿**才算第一版完成，不接受部分通过。
S 项 → 主责模块对应表见 `WBS-001` 末尾。

---

## 2026-11-23 00:2x —— 本轮突破（对外交付前必读）

### 用户要求（原话）
> 「我要睡觉了，你今晚一定要把这个插件搞定，并且上传github，我这边需要明天用这套系统，你测试好奥，发烟测试，点击测试，视觉测试都要到位」

### 两个真根因（都已修）

**① 取数字面量是相对路径 → 打包版必坏**
桌面 App 页面是 `file:///.../app.asar.unpacked/dist/index.html`，裸 `fetch('/api/plugins/...')`
被当成本地文件 → `net::ERR_FILE_NOT_FOUND`。这就是「读不到清单 / Failed to fetch」的真因，**后端一直是好的**。
已加 `apiFetch()`：优先 `window.hermesDesktop.api({path, method, body, priority:'foreground'})`（Electron IPC，
与协议无关），返回 fetch Response 形状，上层 409/403/428 分支零改动；无 electron 时回落 fetch。
> `ctx.rest` **不存在**（曾在别处写错过），真实通道只有 `window.hermesDesktop.api`。
> 排查口诀：插件取数失败先开 DevTools ▸ Network，看到 `ERR_FILE_NOT_FOUND` 就是相对路径写法错了，
> 别去查端口 / 鉴权 / 路由。

**② 插件 id 被一次 duplicate 冲突永久污染**
23:20 一个临时副本 `__cockpit_tmp` 和正式插件同 id → `duplicate id "hermes-cockpit"` → 加载失败。
之后**即使删掉副本、文件字节完全正确、reload 多次，`hermes-cockpit` 也不再出现入口**；
把同一份代码改名成 `zz-probe` 立刻出现 ⇒ 结论是 **id 本身被卡住，reload 不自清**。
处置：**全局改名为 `project-hub`**（三处 id 必须一致）：
- `plugins/project-hub/plugin.yaml` → `id: project-hub`
- `desktop-plugins/project-hub/plugin.js` → `const ID = 'project-hub'`
- `plugins/project-hub/dashboard/manifest.json` → `"name": "project-hub"`
- `scripts/install.sh` 的 PLUG_SRC/PLUG_DST/DESK_SRC/DESK_DST 同步

> 铁律：桌面插件 id 一旦被污染，**换名比修复快**；发布后不要复用旧 id。

### 桌面侧重扫机制（实测）
- `runtime-loader.ts`：`DISK_POLL_MS = 5000`，但**仅当 `document.visibilityState === 'visible'` 才轮询**；
  `watchDirectory` 成功时完全走文件事件，不走轮询。
- 手动 rescan = 命令面板（**Ctrl+K**）→ 输入 `reload plugin` → 选 **Reload desktop plugins**（→ `discoverRuntimePlugins()`）。
  这条命令可靠；**不要靠按 F12/像素点 DevTools 去操作**（AX 树常只返回外壳，实测多轮无变化）。

### 当前卡点（唯一，重启网关即解）
界面报 `404: {"detail":"Plugin not found"}`。
原因：**Python 插件模块只在网关进程启动时导入一次**，改目录名后旧进程里没有
`/api/plugins/project-hub/`。后端代码本身已验（进程内 8 条路由、14 项目 / 1516 会话 / 2 任务 / 14 看板全对）。
解：`python scripts/restart_gateway.py`（先 `--dry-run`），会断开当前会话。

### 重启后按序做
1. `python cache/scratch/probe_ph.py` + App 里点「项目中枢」确认不再 404
2. 三层测试：发烟（`__check.mjs` / `__render_check.mjs` / pytest / `check_boundary.py`）、
   点击（切项目 / 新建会话 / 勾任务 / 会话归类）、视觉（截图 + 27B `http://192.168.31.128:8006/v1`，model `qwen3.8-27b`）
3. 上传 GitHub

### 未解的既有问题（不影响明天用）
项目表 14 个 slug 多为自动名（`project` / `project-2` / `project-12`…），且**不含 `hermes-cockpit`** ⇒
`TASKS.md` 里 `proj: hermes-cockpit` 的 T-001/T-002 落在「未归属任务」组（界面已单列，不会消失）。

