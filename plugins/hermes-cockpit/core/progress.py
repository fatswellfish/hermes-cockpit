"""progress — 父任务的 status / progress 重算（Q-07，B1 前置）。

**纯函数**：不读盘、不写盘、不改入参，返回一个新 dict。重算只发生在「勾选 / 增删子任务之后」，
解析时不做（`taskfile.parse` 保持纯解析，已由测试锁死）。

规则表（QUESTIONS-裁定.md Q-07，逐行对应，顺序即优先级）：

| 条件                                   | status    | progress          |
|----------------------------------------|-----------|-------------------|
| 有 `**阻塞**` 且非「无」                | `blocked` | 按子任务比例（不改百分比） |
| 子任务全部完成（≥1 个）                 | `done`    | 100               |
| 子任务完成比例 p（0 < p < 100）         | `doing`   | round(p)          |
| 子任务全未完成且没有 doing 标记         | `todo`    | 0                 |
| 无子任务                                | 保持手写值不变      | 保持手写值不变      |

三条硬规则：
  1. **有阻塞就 blocked，优先级最高** —— 人最需要看到的信号，不能被 100% 盖住。
  2. **手写百分比在有子任务时会被覆盖** —— 子任务是事实源；无子任务时才尊重手写值。
  3. 比例用 `round()`（银行家舍入，Python 内建），与裁定表一致；不做四舍五入以外的加工。

「没有 doing 标记」的判据：所有子任务都未完成、且标题里没有任何 `doing` / `进行中` 标记
（历史文件里有人把「进行中」写在子任务标题上，这里认它，相当于该子任务在 doing）。

不做的事：不校验 status 是否在白名单（写侧才校验）；不改标题、不改子任务列表。
"""
from __future__ import annotations

# 「无阻塞」的写法：空、None、「无」、「没有」，以及 em dash 一类占位。
_NO_BLOCKER = {"", "无", "没有", "none", "null", "-", "—", "－", "n/a"}

# 子任务标题里表示「正在做」的标记
_DOING_MARKS = ("doing", "进行中", "在做")


def has_blocker(task: dict) -> bool:
    """`**阻塞**` 有实质内容即为真。空 / 「无」/ 「无。」/ 「暂无」/ 占位符都算没有阻塞。

    ⚠️ D-07（REVIEW-r2 实测）：只做精确集合匹配时，人写 `**阻塞**：无。` / `暂无` /
    `无阻塞` 都被判成「有阻塞」，任务在界面上被钉成红色 blocked —— 误报代价最高。
    所以判定前**先归一化再比**：剥首尾标点、「暂无 → 无」、剥「无阻塞/没有阻碍」这类后缀。
    """
    val = task.get("blocker")
    if val is None:
        return False
    return _norm_no_blocker(str(val)) not in _NO_BLOCKER


def _norm_no_blocker(text: str) -> str:
    """把「无阻塞」的各种自然写法归一成 `无` / 占位符，供集合判定。"""
    t = text.strip().strip("。.，,；;！!？?、· \t")
    t = t.lower()
    if t in ("暂无", "暂时没有", "目前没有", "无。", "尚未"):
        return ""
    for suffix in ("阻塞", "阻碍", "风险", "卡点"):
        for prefix in ("无", "没有", "暂无"):
            if t == prefix + suffix:
                return ""
    return t


def completion_ratio(subtasks: list[dict]) -> float | None:
    """子任务完成百分比（0~100 的浮点）。无子任务返回 None（调用方据此保留手写值）。"""
    subs = subtasks or []
    if not subs:
        return None
    done = sum(1 for s in subs if s.get("done"))
    return 100.0 * done / len(subs)


def _has_doing_mark(subtasks: list[dict]) -> bool:
    for s in subtasks:
        title = str(s.get("title") or "").lower()
        if any(mark in title for mark in _DOING_MARKS):
            return True
    return False


def recompute(task: dict) -> dict:
    """按 Q-07 规则表重算一个任务的 status / progress，返回**新** dict（入参不变）。"""
    out = dict(task)
    subs = list(task.get("subtasks") or [])

    # 规则 5：无子任务 —— status 与 progress 都保持手写值
    if not subs:
        return out

    # D-08：显式区分「没有子任务」与「子任务全未完成」，不靠 `or 0.0`
    ratio = completion_ratio(subs)
    ratio = 0.0 if ratio is None else ratio
    done_count = sum(1 for s in subs if s.get("done"))

    # 规则 1：阻塞优先，百分比按子任务比例（不改百分比 = 不用 100 盖住它）
    if has_blocker(task):
        out["status"] = "blocked"
        out["progress"] = int(round(ratio))
        return out

    # 规则 2：全部完成
    if done_count == len(subs):
        out["status"] = "done"
        out["progress"] = 100
        return out

    # 规则 3：部分完成
    if ratio > 0:
        out["status"] = "doing"
        out["progress"] = int(round(ratio))
        return out

    # 规则 4：一个都没完成 —— 有 doing 标记算 doing，否则 todo
    if _has_doing_mark(subs):
        out["status"] = "doing"
        out["progress"] = 0
    else:
        out["status"] = "todo"
        out["progress"] = 0
    return out
