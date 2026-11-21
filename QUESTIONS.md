# QUESTIONS — 实现期间设计文档未写清、我不自行发明规则的地方

> 规则：遇到设计没写清的地方停在这里记录，不自己发明。
> 每条标：**卡住的任务** / **我目前的做法（占位，非定案）** / **需要谁拍板**。
> 编号 Q-nn，答复后就地标注「已裁定：<结论>」并保留原问，不新开平行条目。

---

## 第一轮（F1 / A1 / A2）

### Q-01 适配层「唯一接触面」与 `ctx.rest` 的关系（影响 C 模块全部）

- **现象**：DD-001 §11.1 写「所有宿主私有调用（读三个库 / 调用 gateway RPC / 注入 hook）**只允许**出现在 `dashboard/host_adapter.py`」，
  但 WBS C1/C2 要求前端 `plugin.js` 通过 `ctx.rest('/overview')` 访问自己的 `plugin_api.py`。
  `ctx.rest` 本身是宿主 SDK 能力，严格按字面理解，`plugin.js` 里出现 `ctx.rest(` 也算"宿主私有调用"。
- **冲突点**：F1 扫描器不把 `ctx.rest` 列为违规（它不是数据/RPC 越界），但「注入 hook」这一项在
  字面上也没有约定 hook 注册代码放在哪。
- **我目前的做法**：`check_boundary.py` 只判 `sqlite3.connect(<宿主库>)`、`host.request(`、库名字面量、写 SQL、
  自拼宿主数据根；**不判 `ctx.rest`**。hook 注册文件若出现 `host.request(` 会被判越界，需先登记。
- **需要拍板**：hook 注册（E2）是否也并进 `host_adapter.py`？`ctx.rest` 是否明确豁免？

### Q-02 F1 是否要求「跑全绿即证明 S10」，还是必须带负向自证

- **现象**：WBS F1 验收写「故意在 store.py 里加一行 sqlite3.connect(state.db) → 扫描脚本报错」，只要求报出。
- **我目前的做法**：除了报出，还做了「删掉后回到 0」的双向自证（本轮报告里有命令与输出）。
- **需要拍板**：双向自证是否需要固化成一条测试（`tests/test_check_boundary.py`，用临时副本），
  后续 CI/评审时自动跑？本轮未加这个测试文件，因为它不在任务书要求里，等你点头再加。

### Q-03 「列名探测失败」与「表不存在」两种失败要不要区分

- **现象**：任务书要求「`require_column` 报明确错误而非静默返回空」。实测 `columns()` 对**不存在的表**返回 `[]`，
  而 `read_projects()` 等函数在 `cols == []` 时**直接 return []**，即「表不存在」是静默的。
- **我目前的做法**：只保证 `require_column`（列名候选全不存在时）抛 `HostSchemaError`；
  表不存在仍返回空列表，因为是"宿主还没建这个库/表"的正常场景（首启、fresh 模式）。
- **需要拍板**：表不存在是否也要报错/告警？（我倾向保持静默，但那属于设计判断，不自己定。）

### Q-04 fresh 作用域下 `setup.at` 为 null 时的行为

- **现象**：DD-001 S19 规定 `fresh` 模式只看 `started_at >= setup.at`，但 META.yaml 里 `at` 初值是 `null`，
  文档没写「mode 已是 fresh 但 at 还没写」这个中间态怎么处理。
  实测 `sessions` 有 1493 行、`started_at` 无空值、单位是 unix 秒浮点（例 1795274926.908091）。
- **我目前的做法**：未实现（E1 不在本轮）。提醒自己在 E1 时不要默认成「at 为空就当全量可见」。
- **需要拍板**：`at` 为空 + mode=fresh → ① 全部不可见 ② 全量可见 ③ 引导未完成不得进 fresh ？

### Q-05 `TASKS.md` 里「裸续行」算不算合法格式

- **现象**：真实 `TASKS.md` 的 T-002 快照折成两行，第二行 `10.0.1.175:8005（公司网段，000 不通）…`
  既不是字段行也不是子任务行。「格式约定」节没有定义这种裸续行。
- **我目前的做法**：续进「最近一次出现的字段」（实测 T-002 的 snapshot 已正确合并成两行）；
  子任务/更新/会话行会把续行归属清空，防止错接。
- **需要拍板**：这是正式格式（那要在「格式约定」里补一行），还是应该把 T-002 改成多行显式续写？
  我按"兼容现实文件"实现了，但**没有把它写进格式约定文档**（禁改基线）。

### Q-06 `serialize` 能不能重排 / 丢注释

- **现象**：任务书只要求 `parse(serialize(parse(x))) == parse(x)`，没有说序列化产物是否需要保留
  原文件里的分隔线、自由正文、字段顺序。
- **我目前的做法**：输出是**规范化重写**——固定字段顺序（标题→子任务→更新→截止→快照→阻塞→会话）、
  丢掉分隔线 `---` 与自由正文、末尾自动补「格式约定」节。
- **风险**：如果 B1 直接拿 `serialize()` 整篇覆写 `TASKS.md`，用户手写的正文会被抹掉。
- **需要拍板**：B1 写回是「整篇 serialize 覆写」还是「按任务块做局部替换」？（影响 S4 并发写与用户体验）

### Q-07 `progress` 与 `status` 的归属边界

- **现象**：WBS B1 说「勾选子任务后自动重算父任务百分比与状态」，但没定义状态怎么推
  （0%→todo？100%→done？有 blocker 时是否强制 blocked？），也没定义手写百分比何时被覆盖。
- **我目前的做法**：解析器**完全不改** progress/status，原样返回（并写了测试锁住这一点）。
- **需要拍板**：状态重算规则表（P0）。这条不定，B1 无法开工。

### Q-08 子任务 id 与父 id 的前缀规则

- **现象**：格式约定用 `T-nnn.m`，但没写非 T 前缀的任务（如 `D-001.2`、`M-005.10`）是否允许，
  也没写「父 id 为 T-005 的子任务写成 T-999.1」时以谁为准。
- **我目前的做法**：接受任意 `字母-数字` 前缀，子任务 id 若自带就原样保留（不校验是否与父 id 同前缀），
  未带 id 时按父 id + 序号自动补。重复父 id 抛 `TaskFileError`，但**重复子任务 id 不报错**。
- **需要拍板**：子任务 id 与父 id 前缀不一致时，是报错还是接受？

---

## 本轮用到、但属于「已实测事实」而非疑问的记录（供评审核对，不算问题）

| 事实 | 实测值 | 来源 |
|---|---|---|
| 宿主版本 | 0.23.2 | `gateway_state.json: code_version`；`hermes --version` 一致 |
| `projects` 表列 | id/slug/name/description/icon/color/board_slug/primary_path/created_at/archived | `PRAGMA table_info` |
| `projects` 行数 | 14 | `count(*)` |
| 项目主目录列名 | **`primary_path`**（不是 `path`/`root_path`） | `PRAGMA table_info` |
| `sessions` 列数 | 66，主键 `id`，时间列 `started_at`，**无 `created_at`** | `PRAGMA table_info` |
| `sessions` 行数 | 1493（`started_at` 无空值，unix 秒浮点） | `count(*)` / `min,max` |
| 看板目录 | 14 个，均有 `kanban.db`，`tasks` 表存在 | 目录扫描 |
| py3.14 `sqlite3.Connection` | `with con:` **不关连接**；`close` 是只读属性不能打补丁 | 本机实测 |

> ⚠️ 上表最后一行意味着：`host_adapter.py` 里所有 `with open_ro(...)` 都会泄漏句柄。
> 本轮已改为 `try/finally: con.close()`，并加了回归测试
> （`test_read_helpers_leave_no_open_connection`）。这是**改动了你起草的适配层**，
> 只动了连接生命周期，未改任何读取语义与列名探测逻辑 —— 请你确认这个改动可接受。
