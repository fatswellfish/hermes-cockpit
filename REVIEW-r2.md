# 助理侧独立复核（第二轮）— `core/progress.py`

> 审核人：助理。依据：`QUESTIONS-裁定.md` Q-07 规则表。
> 方法：自己跑，不采信 coder 测试。

## 一、符合裁定的部分

| 规则 | 结论 | 实测 |
|---|---|---|
| 阻塞 2/2 完成 → `blocked`（不被 100% 掩盖成 done） | ✅ | got `(blocked, 100)` |
| 阻塞 1/2 → `blocked, 50` | ✅ | 按比例不改写 |
| 阻塞 0/2 → `blocked, 0` | ✅ | |
| 子任务全完成 → `done, 100` | ✅ | |
| 部分完成 → `doing, round(p)` | ✅ | 1/3 → `doing, 33` |
| 全未完成 → `todo, 0` | ✅ | |
| 无子任务 → 保持手写值 | ✅ | `doing, 77` 原样返回 |
| **纯函数性**（入参不被污染） | ✅ | 调用前后 `str(task)` 一致 |
| 「无」/ 空 视为无阻塞 | ✅ | |

## 二、发现的缺陷

### D-07【一般·但会误报】「无阻塞」的自然写法漏判

**实测**：
```
阻塞="无"   → done, 100   ✅ 正确
阻塞="无。"  → blocked, 100  ❌ 错误（应 done）
```

**根因**：`_NO_BLOCKER` 是精确匹配集合
```python
_NO_BLOCKER = {"", "无", "没有", "none", "null", "-", "—", "－", "n/a"}
...
return text.lower() not in _NO_BLOCKER       # 整串精确比对
```
人在写 `**阻塞**：无。` 或 `**阻塞**：暂无` / `**阻塞**：无阻塞` 时都判成「有阻塞」，
任务会被错误地标成 `blocked` —— 这是界面上最显眼的红色信号，误报代价高。

**建议修法**：判定前做归一化，而不是加更多字面量。
```python
def _norm_no_blocker(text: str) -> str:
    t = text.strip().strip("。.，,；;！!？?、 ").strip()      # 剥首尾标点
    t = t.lower()
    for suf in ("阻塞", "阻碍", "风险"):                      # 剥「无阻塞」这类后缀
        if t == "无" + suf or t == "没有" + suf:
            return "无"
    return t
```
判定改为 `_norm_no_blocker(text) in _NO_BLOCKER`。
测试要覆盖：`无` / `无。` / `暂无` / `无阻塞` / `没有` / `N/A` / `—` / `None` / 空 / `卡在等 review`（真阻塞）。

### D-08【建议】`completion_ratio` 返回 `float | None`，但调用处用 `or 0.0`

```python
ratio = completion_ratio(subs) or 0.0
```
无子任务时 `completion_ratio` 返回 `None`，被 `or 0.0` 变成 `0.0`。
虽然 `not subs` 已在前面 return，不会走到这里，但**`0.0 or 0.0` 对 0 完成比例也成立**，
语义上把「无子任务」和「全部未完成」混成同一个值。建议显式判 `is None`，别依赖 `or`。

## 三、结论

**有条件通过**：D-07 必须修（会导致界面误报 blocked），D-08 一并改。改完 `progress.py` 可视为完成。
