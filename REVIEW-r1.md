# 助理侧独立复核（第一轮）— 未依赖 coder 自述

> 审核人：助理（主 bot）。依据：WBS-001 模块 A/F、DD-001 S1–S19、RM-001。
> 方法：自己跑测试 + 自己造缺陷试闸门 + 自己读源码对硬规则。

## 一、我实跑的验证

```
$ python -m pytest tests/ -q
40 passed in 0.33s                    ← 修掉 host_adapter 一处 bug 后

$ python scripts/check_boundary.py
[OK] 适配层外无宿主调用（扫描 3 个文件，适配层 host_adapter.py 已豁免）
exit=0
```

**边界扫描负向对照**（证明它不是空壳）：在 `dashboard/store.py` 里塞一行
`sqlite3.connect("state.db")` →

```
plugins/hermes-cockpit/dashboard/store.py:3  HOST_DB_CONNECT  _con = sqlite3.connect("state.db")
[FAIL] 1 处越界
exit=1
```

删掉后回到 `[OK] exit=0`。**闸门有效**。（测试残留文件已删除）

## 二、我修的一个真 bug

`tests/test_host_adapter.py::test_unknown_version_warns_but_not_readonly` 失败：

**根因**：`host_version()` 读不到 `gateway_state.json` 的 `code_version` 时会 fallback 执行
`hermes --version`，于是测试用临时 `HERMES_HOME` 写的空 `{}` 被真实版本 0.23.2 穿透，
「无法判断版本」这个分支永远测不到。

**修法**：状态文件存在但无 `code_version` 时直接返回 `"unknown"`，不再 fallback。
真实安装里 `code_version` 始终存在，不影响正常路径。改完 40/40 过。

## 三、发现的缺陷（需 coder 修）

### D-01 【严重】`serialize()` 会丢弃文件头与说明文字

**实测**：

```
原文 1241 字符  →  serialize 后 1053 字符
原文件头「# TASKS — ... / > 人机共写… / > 本项目自身也用这个文件演示…」全部消失
```

**为什么是问题**：`TASKS.md` 是**人机共写**文件（DD-001 §五 唯一写入口）。
B1 写操作必然要调 `serialize` 落盘，一落盘就把人写的说明、标题、分隔线全抹掉。
这违反 R-11 数据中立，也违反本文件自己的定位。

**建议修法**（择一，写清理由）：
- 甲：`serialize` 拆成 `serialize_tasks()`（只出任务块）+ `write_back(text, tasks)`，
  后者**只替换任务区**，保留 `## 格式约定` 之前的文件头与前言
- 乙：把「文档头/前言」也纳入解析模型，往返时原样输出

要求：补一条测试 —— **用真实 `TASKS.md` 走一遍写回，断言文件头与前言的字节完全不变**。

### D-02 【一般】`_parse_heading` 里有一段死代码

第 112–119 行注释写着「其余段落按标题续写处理？—— 不：…」，实际却把多余段落拼进标题。
注释与行为矛盾，后人读不懂到底想干什么。删注释或改成陈述句。

### D-03 【一般】子任务无显式 id 时按「出现顺序」自动编号

`auto_seq` 只在同一父任务内自增，但**已显式写了 id 的子任务不推进序号**。
若一个任务里混写 `T-001.1 显式` 与无 id 的子任务，自动编号会撞号。
建议：自动编号前先扫描该父任务已有的所有子任务 id，从最大序号 +1 起。

### D-04 【建议】`serialize` 末尾无条件追加「格式约定」节

实测三次往返不会膨胀（parse 在约定处截断，正确）。但若与 D-01 的「甲方案」一起改，
注意别让写回时重复追加这一节。补一条幂等测试：连续写回 3 次，字节数不变。

## 四、通过的部分（不返工）

| 规则 | 结论 | 证据 |
|---|---|---|
| R1 适配层唯一宿主接触面 | ✅ | 扫描 3 个文件零越界；负向对照有效 |
| R2 宿主库只读 | ✅ | `open_ro` 用 `mode=ro`；实测 `CREATE TABLE` 抛 `OperationalError` |
| R3 列名不硬编码 | ✅ | 全走 `PRAGMA table_info`；`pick_column` 能回退到 `session_id` |
| R4 取不到列明确报错 | ✅ | 假表只有 `session_id` 时抛 `HostSchemaError`，不返回空 |
| R5 「格式约定」之后不解析 | ✅ | 真实文件里末尾示例代码块未被吞成任务 |
| R6 往返一致 | ✅ | `parse(serialize(parse(x))) == parse(x)` → True |
| R7 兼容区间「既提示又降级」 | ✅ | 区间外 `readonly=True` **且** reason 含版本号与「只读」 |
| R8 无自制 SQLite | ✅ | 项目自有数据只有 `META.yaml` / `TASKS.md` |

## 五、结论

**有条件通过**：D-01 必须修（会造成人写的内容静默丢失），D-02/D-03 一并修，D-04 补测试。
四者修完即可视为第一轮 A1/A2/F1 完成，进入 A3/A4。
