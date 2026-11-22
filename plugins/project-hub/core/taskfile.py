"""taskfile — TASKS.md 的唯一解析实现（A2）。

格式（WBS A2 / TASKS.md「格式约定」节，严格照实现）：

    ## T-001 标题 · doing · 40% · proj:mms
    - [x] T-001.1 子任务已完成
    - [ ] T-001.2 子任务未完成
    > 更新：2026-11-21 22:30
    **截止**：2026-11-30
    **快照**：现在做到哪了
    **阻塞**：卡在哪

三条硬规则：
  1. **「格式约定」之后的内容不参与解析** —— 文档末尾的示例代码块会被误当成任务。
  2. 子任务行以 `- [ ]` / `- [x]` 开头，`T-nnn.m` 是子任务 id；`T-nnn` 是父任务 id。
  3. `serialize(parse(x))` 必须能被 `parse` 还原成逐字段相等的列表（往返测试锁死）。

## 落盘纪律（Q-06，违反即返工）

**落盘唯一入口是 `write_back(original, tasks)`；`serialize()` / `serialize_tasks()`
严禁用于写文件。**
  · `serialize_tasks()` 只出任务块，供展示 / 导出 / 从零生成；
  · `write_back()` 只替换任务区，文件头、前言、`---` 分隔线、块内自由正文、
    「格式约定」节**逐字节保留**；
  · 用 `serialize()` 整篇覆写已有文件会丢掉上面这些人的内容（D-01/D-05 实测）。

## 裸续行是正式格式（Q-05）

`**快照**` / `**阻塞**` 的值可以折行续写：

    **快照**：当前做到哪一步了，
      剩下的是验收和文档同步

  · 续行**以两个空格缩进开头**表示承接（实测 TASKS.md 的 T-002 快照被折成两行，
    共 176 字，必须完整保留）；
  · 续行归属**最近一次出现的字段**；空行 / 子任务行 / `> 更新` / `**会话**` 都会结束续写；
  · `write_back` 时**值没变就原样沿用那几行**，绝不把折行合并成一行（D-06）。

不做的事：不重算父进度（那是 B1 写操作的事，见 `progress.recompute`）、
不校验状态取值是否在白名单里（读侧不拒绝，写侧才校验）、不碰文件 IO
（IO 与并发保护在 A4 / store 层）。
"""
from __future__ import annotations

import re

# ── 字段契约 ────────────────────────────────────────────────────────────────

FIELDS = ("id", "title", "status", "progress", "project", "subtasks",
          "due", "snapshot", "blocker", "updated", "session")

# 子任务结构：{id, title, done}
SUBTASK_FIELDS = ("id", "title", "done")

STATUSES = ("todo", "doing", "done", "blocked")

# 文档末尾「格式约定」节的标题识别（之后的内容全部不解析）
STOP_HEADING_RE = re.compile(r"^#{1,6}\s*格式约定")

# ## T-001 标题 · doing · 40% · proj:mms
TASK_HEADING_RE = re.compile(r"^##\s+(?P<rest>.+?)\s*$")

# 头部里以「 · 」切分（也接受全角/半角混写）
HEAD_SPLIT_RE = re.compile(r"\s*[·•]\s*|\s+\|\s+")

# 任务 id：T-001 / T-001.2 / 任意字母前缀-nnn
TASK_ID_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*-)(\d+)(?:\.(\d+))?$")

SUBTASK_RE = re.compile(
    r"^\s*[-*]\s+\[(?P<mark>[ xX✓✔])\]\s+(?P<rest>.+?)\s*$"
)

PROGRESS_RE = re.compile(r"^(\d{1,3})\s*%\s*$")
PROJ_RE = re.compile(r"^proj\s*[:：]\s*(?P<slug>.+?)\s*$", re.IGNORECASE)

# 行内字段：**截止**：xxx / **快照**：xxx / **阻塞**：xxx
FIELD_LINE_RE = re.compile(
    r"^\s*\*\*(?P<key>截止|快照|阻塞|更新时间|更新)\*\*\s*[:：]\s*(?P<val>.*?)\s*$"
)
# 引用行：> 更新：2026-11-21 22:30
UPDATE_LINE_RE = re.compile(r"^\s*>\s*(?P<key>更新|更新时间)\s*[:：]\s*(?P<val>.*?)\s*$")
# 会话：**会话**：<session_id> 或 > 会话：<id>
SESSION_LINE_RE = re.compile(r"^\s*(?:>\s*)?\*\*会话\*\*\s*[:：]\s*(?P<val>.*?)\s*$")

KEY_TO_FIELD = {"截止": "due", "快照": "snapshot", "阻塞": "blocker",
                "更新": "updated", "更新时间": "updated"}


class TaskFileError(ValueError):
    """TASKS.md 结构不可解析（例如同一个任务 id 出现两次）。"""


def _empty_task() -> dict:
    return {"id": "", "title": "", "status": "todo", "progress": 0,
            "project": None, "subtasks": [], "due": None, "snapshot": None,
            "blocker": None, "updated": None, "session": None}


def _parse_heading(rest: str) -> dict | None:
    """解析 `## T-001 标题 · doing · 40% · proj:mms`。

    返回 None 表示这个二级标题不是任务（例如 `## 模块 A`、`## 附录`）。
    """
    parts = [p.strip() for p in HEAD_SPLIT_RE.split(rest) if p.strip()]
    if not parts:
        return None

    # 第一段前半是 id，后半是标题：「T-001 标题」
    first = parts[0]
    m = re.match(r"^(?P<id>[A-Za-z][A-Za-z0-9_]*-?\d+(?:\.\d+)?)\s*(?P<title>.*)$", first)
    if not m:
        return None
    task_id = m.group("id")
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*-?\d+$", task_id):
        return None
    title = m.group("title").strip()

    task = _empty_task()
    task["id"] = task_id
    task["title"] = title

    for seg in parts[1:]:
        pm = PROGRESS_RE.match(seg)
        if pm:
            task["progress"] = int(pm.group(1))
            continue
        jm = PROJ_RE.match(seg)
        if jm:
            task["project"] = jm.group("slug")
            continue
        if seg.lower() in STATUSES:
            task["status"] = seg.lower()
            continue
        # 除 status / 百分比 / proj:slug 之外的剩余段落一律并入标题尾部。
        # 理由：这些段只出现在标题行上，并入标题后仍原样落回标题行，
        # serialize 不改变行的结构，往返不丢信息。
        if title:
            title = f"{title} · {seg}"
            task["title"] = title
        else:
            task["title"] = seg
    return task


def _parse_subtask_id(text: str) -> tuple[str, str]:
    """从子任务正文里切出 id。`T-001.1 子任务` -> ("T-001.1", "子任务")。

    返回空 id 表示这一行没有显式写 id，由调用方按「已有序号最大值 +1」补号。
    """
    m = re.match(r"^(?P<id>[A-Za-z][A-Za-z0-9_]*-?\d+\.\d+)\s*(?P<title>.*)$", text)
    if m:
        return m.group("id"), m.group("title").strip()
    return "", text.strip()


def _subtask_seq(sid: str) -> int | None:
    """取子任务 id 的序号部分：`T-001.7` -> 7；`X-a.12` -> 12；取不出返回 None。"""
    if not sid:
        return None
    m = re.match(r"^[A-Za-z][A-Za-z0-9_\-]*\.(\d+)$", sid.strip())
    return int(m.group(1)) if m else None


def _next_free_seq(parent_id: str, used_ids: set[str]) -> int:
    """给「无显式 id」的子任务分配父任务内最小可用序号。

    取「该父任务已有全部子任务 id 的最大序号 + 1」，而不是按出现顺序自增：
    混写 `T-001.1 显式` 与无 id 行时，按出现顺序编号会撞上已显式写出的号。
    不做「回填空缺」——回填会让同一个文件在不同轮解析里给出不同的 id。
    """
    prefix = f"{parent_id}."
    seqs = [s for s in (_subtask_seq(i) for i in used_ids if i.startswith(prefix)) if s]
    return max(seqs, default=0) + 1


def parse(text: str) -> list[dict]:
    """解析 TASKS.md 全文，返回任务列表。遇到「格式约定」标题即停止。"""
    tasks: list[dict] = []
    cur: dict | None = None
    cur_fence = False          # 当前是否在 ``` 代码块内（代码块里的 ## 不算任务）
    _last_field: str | None = None   # 最近一次赋值成功的字段（用于裸续行归属）
    seen: set[str] = set()
    # 每个父任务已占用的子任务 id（显式 + 自动分配的都算），用于 D-03 的不撞号编号
    used_sub_ids: dict[str, set[str]] = {}

    for raw in text.splitlines():
        line = raw.rstrip()

        # 代码块围栏：里面的内容一律不解析（示例块保护）
        if line.lstrip().startswith("```"):
            cur_fence = not cur_fence
            continue
        if cur_fence:
            continue

        # 「格式约定」之后全部不解析
        if STOP_HEADING_RE.match(line.lstrip()):
            break

        heading = TASK_HEADING_RE.match(line)
        if heading:
            task = _parse_heading(heading.group("rest"))
            _last_field = None
            if task is None:
                cur = None      # 非任务二级标题：结束当前任务块
                continue
            if task["id"] in seen:
                raise TaskFileError(f"任务 id 重复：{task['id']}")
            seen.add(task["id"])
            tasks.append(task)
            cur = task
            used_sub_ids[task["id"]] = set()
            continue

        if cur is None:
            continue

        stripped = line.strip()
        if not stripped:
            continue
        # 任务之间的分隔线
        if re.match(r"^-{3,}$", stripped):
            continue

        sub = SUBTASK_RE.match(line)
        if sub:
            done = sub.group("mark").strip().lower() in ("x", "✓", "✔")
            sid, stitle = _parse_subtask_id(sub.group("rest"))
            if not sid:
                # D-03：无显式 id 的行按「该父任务已用的最大序号 +1」编号，
                # 扫描范围含本次之前出现的显式 id，所以混写也不会撞号。
                sid = f"{cur['id']}.{_next_free_seq(cur['id'], used_sub_ids[cur['id']])}"
            elif sid in used_sub_ids[cur["id"]]:
                # Q-08：同一父任务内子任务 id 必须唯一。重复会让勾选/写回改错行。
                raise TaskFileError(f"子任务 id 重复：{cur['id']} 下的 {sid}")
            used_sub_ids[cur["id"]].add(sid)
            cur["subtasks"].append({"id": sid, "title": stitle, "done": done})
            _last_field = None      # 子任务行打断续行归属
            continue

        um = UPDATE_LINE_RE.match(line)
        if um:
            cur["updated"] = um.group("val") or None
            _last_field = None
            continue

        sm = SESSION_LINE_RE.match(line)
        if sm:
            cur["session"] = sm.group("val") or None
            _last_field = None
            continue

        fm = FIELD_LINE_RE.match(line)
        if fm:
            field = KEY_TO_FIELD.get(fm.group("key"))
            if field:
                val = fm.group("val") or None
                if field == "updated":
                    cur["updated"] = val
                    _last_field = None
                else:
                    # 同一字段多行时续接（快照/阻塞常写成多行）
                    if cur[field]:
                        cur[field] = f"{cur[field]}\n{val}" if val else cur[field]
                    else:
                        cur[field] = val
                    _last_field = field
            continue

        # 字段行未闭合就换行的续行：上一行是 **快照**/**阻塞** 的裸续写。
        # 实测 TASKS.md 里就有（T-002 的快照被折成两行，第二行既不是字段也不是子任务）。
        # 续进哪个字段由「最近一次出现的字段」决定，子任务/更新/会话行会把它清空，
        # 因此不会把正文错接到别的字段上。
        if _last_field is not None:
            cur[_last_field] = (f"{cur[_last_field]}\n{stripped}"
                                if cur[_last_field] else stripped)
            continue
        # 其它行（自由正文）不参与解析，也不产生歧义

    return _normalize(tasks)


def _normalize(tasks: list[dict]) -> list[dict]:
    """补全字段并稳定排序键，保证两次解析结果可直接 ==。"""
    out = []
    for t in tasks:
        item = {k: t.get(k) for k in FIELDS}
        item["id"] = t["id"]
        item["title"] = t.get("title", "")
        item["status"] = (t.get("status") or "todo").lower()
        item["progress"] = int(t.get("progress") or 0)
        item["subtasks"] = [
            {"id": s["id"], "title": s["title"], "done": bool(s["done"])}
            for s in (t.get("subtasks") or [])
        ]
        out.append(item)
    return out


# ── 序列化 ──────────────────────────────────────────────────────────────────


def _fmt_heading(task: dict) -> str:
    segs = [task["status"], f"{task['progress']}%"]
    if task.get("project"):
        segs.append(f"proj:{task['project']}")
    head = f"## {task['id']}"
    if task.get("title"):
        head += f" {task['title']}"
    return head + "".join(f" · {s}" for s in segs)


FORMAT_HEADING = "## 格式约定（以下为示例，不参与解析）"

# 任务区的下边界：第一个「格式约定」二级标题所在行（找不到就用文末）
STOP_BLOCK_RE = re.compile(r"^(?P<prefix>#+\s*格式约定[^\n]*)$", re.MULTILINE)

# 任务之间的人写分隔线（write_back 原样保留）
_MD_SEP_RE = re.compile(r"^\s*-{3,}\s*$")


def serialize_tasks(tasks: list[dict]) -> str:
    """只把任务列表序列化成「任务块」（不含文件头，也不含「格式约定」节）。

    每个任务块输出顺序固定：
        标题行 → 子任务 → > 更新 → **截止** → **快照** → **阻塞** → **会话**
    块之间空一行；返回值不带首尾空行。
    """
    blocks: list[str] = []
    for t in tasks:
        lines = [_fmt_heading(t)]
        for s in t.get("subtasks") or []:
            mark = "x" if s.get("done") else " "
            sid = s.get("id") or ""
            title = s.get("title") or ""
            body = f"{sid} {title}".strip()
            lines.append(f"- [{mark}] {body}")
        if t.get("updated"):
            lines.append(f"> 更新：{t['updated']}")
        for key, field in (("截止", "due"), ("快照", "snapshot"), ("阻塞", "blocker")):
            val = t.get(field)
            if val:
                # 多行字段：首行跟字段名，后续行原样续行（parse 会续接）
                first, *rest = str(val).split("\n")
                lines.append(f"**{key}**：{first}")
                lines.extend(rest)
        if t.get("session"):
            lines.append(f"**会话**：{t['session']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def write_back(original_text: str, tasks: list[dict]) -> str:
    """把任务列表写回**原文件**，只替换任务区，其余字节原样保留。

    TASKS.md 是人机共写文件（DD-001 §五 唯一写入口），落盘**只准走这里**；
    `serialize()` 严禁用于写文件。保证：
      · 文件头 / 前言 / 说明注释 —— 逐字节不变
      · 任务块**内部**人的排版不变：折行的「快照/阻塞」保持原折行（D-06）、
        块内自由正文与空行原样保留（D-05）
      · 任务之间的分隔线 `---` 与位置不变（D-05）
      · 「## 格式约定」这一节及之后原样保留，绝不重复追加

    语义：**按 id 就地替换**。原文件有、tasks 里也有的任务 → 只重写这一个块；
    原文件没有的新任务 → 追加到任务区末尾；tasks 里已不存在的任务 → 从文件里删掉。
    幂等：write_back(write_back(x, t), t) == write_back(x, t)。
    """
    new_by_id = {t["id"]: t for t in tasks}
    lines = original_text.splitlines(keepends=True)
    out: list[str] = []
    seen: set[str] = set()
    in_fence = False
    i = 0

    while i < len(lines):
        bare = lines[i].rstrip("\r\n")

        if bare.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(lines[i])
            i += 1
            continue
        if in_fence:
            out.append(lines[i])
            i += 1
            continue

        if STOP_HEADING_RE.match(bare.lstrip()):
            _append_new_tasks(out, tasks, seen)
            out.extend(lines[i:])                     # 约定节及之后原样搬走
            break

        m = TASK_HEADING_RE.match(bare)
        parsed = _parse_heading(m.group("rest")) if m else None
        if parsed is None:
            out.append(lines[i])                      # 文件头 / 前言 / 非任务标题
            i += 1
            continue

        end = _block_end(lines, i)
        tid = parsed["id"]
        seen.add(tid)
        if tid in new_by_id:
            out.extend(_render_block(new_by_id[tid], lines[i:end]))
        # 不在 tasks 里 = 调用方要删掉这个任务 → 连块一起丢
        i = end

    else:
        _append_new_tasks(out, tasks, seen)

    return "".join(out)


def _block_end(lines: list[str], start: int) -> int:
    """任务块结束位置：下一个二级标题、分隔线 `---`、或「格式约定」。

    空行不算结束 —— 块内空行是人的排版，属于这个任务。
    """
    in_fence = False
    i = start + 1
    while i < len(lines):
        bare = lines[i].rstrip("\r\n")
        if bare.lstrip().startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if not in_fence:
            if (STOP_HEADING_RE.match(bare.lstrip())
                    or _MD_SEP_RE.match(bare)
                    or TASK_HEADING_RE.match(bare)):
                return i
        i += 1
    return len(lines)


def _append_new_tasks(out: list[str], tasks: list[dict], seen: set[str]) -> None:
    """把 tasks 里文件还没有的任务追加到任务区末尾（各自一个块，块间空行）。"""
    fresh = [t for t in tasks if t["id"] not in seen]
    if not fresh:
        return
    if out and not "".join(out).endswith("\n\n"):
        out.append("\n" if "".join(out).endswith("\n") else "\n\n")
    for n, t in enumerate(fresh):
        if n:
            out.append("\n")
        out.append("\n".join(_render_block(t, [])) + "\n")


def _parse_subtask_from_lines(lines: list[str]) -> list[dict]:
    """把原块里的子任务行还原成 [{id, title, done}]，用来判断「有没有被人改过」。

    编号规则与 parse 一致（无 id 的行取该块已用最大序号 +1），保证同一份块两次判断结果相同。
    """
    out: list[dict] = []
    used: set[str] = set()
    for raw in lines[1:]:
        m = SUBTASK_RE.match(raw.rstrip("\r\n"))
        if not m:
            continue
        sid, title = _parse_subtask_id(m.group("rest"))
        if not sid:
            sid = f"{_parent_of(lines[0])}.{_next_free_seq(_parent_of(lines[0]), used)}"
        used.add(sid)
        out.append({"id": sid, "title": title,
                    "done": m.group("mark").strip().lower() in ("x", "✓", "✔")})
    return out


def _parent_of(heading_line: str) -> str:
    m = TASK_HEADING_RE.match(heading_line.rstrip("\r\n"))
    parsed = _parse_heading(m.group("rest")) if m else None
    return parsed["id"] if parsed else ""


def _render_block(task: dict, old: list[str]) -> list[str]:
    """渲染一个任务块。old 是原文的这一个块 —— 用来继承人手写的排版（D-05/D-06）。

    继承规则（只在语义没变时继承，改过的字段按新值输出）：
      · 子任务行：id 与 done 都没变 → 原样保留这一行（含人手写的缩进/勾选符号）
      · **快照**/**阻塞**：值没变 → 原样保留（含折行续写与其后的空行）
      · 其它未知行（自由正文、缩进续写、块内空行）→ 原样保留
    顺序规则：标题行固定第一；老块里没有的字段按
      「子任务 → > 更新 → 截止 → 快照 → 阻塞 → 会话」补在后面（只补缺的，不重排已有的）。
    """
    nl = _newline_of(old)

    # 老块按「行 → 归属」分类，未识别的行原样保留
    old_sub = {s["id"]: s for s in ((_parse_subtask_from_lines(old)))}
    want_ids = {s["id"] for s in (task.get("subtasks") or [])}
    want_subs = list(task.get("subtasks") or [])
    old_sub_lines: dict[str, str] = {}
    body: list[str] = []          # 原块去掉标题行后的行（保留原文顺序）
    for raw in old[1:]:
        bare = raw.rstrip("\r\n")
        sm = SUBTASK_RE.match(bare)
        if sm:
            sid, _ = _parse_subtask_id(sm.group("rest"))
            if sid:
                old_sub_lines[sid] = raw
        body.append(raw)

    # 1) 子任务：先按新任务里的顺序输出，能继承就继承原行
    sub_lines: list[str] = []
    for s in want_subs:
        old_line = old_sub_lines.get(s["id"])
        prev = old_sub.get(s["id"])
        if old_line is not None and prev is not None and prev.get("done") == s.get("done") \
                and (prev.get("title") or "") == (s.get("title") or ""):
            sub_lines.append(old_line if old_line.endswith(("\n", "\r\n")) else old_line + nl)
        else:
            mark = "x" if s.get("done") else " "
            body_text = f"{s['id'] or ''} {s.get('title') or ''}".strip()
            sub_lines.append(f"- [{mark}] {body_text}{nl}")

    # 2) 字段行与新值：值没变就沿用原文那几行（含折行），变了就按新值输出
    field_render = _render_fields(task, old, nl)

    # 3) 组装：标题 + 子任务 → 其余原文行（空行/自由正文原样，字段整段沿用）
    rendered: list[str] = [_fmt_heading(task) + nl]
    rendered.extend(sub_lines)
    leftover_new_fields = dict(field_render)
    consumed: set[str] = set()        # 已经整段沿用过的字段（不再逐行重复输出）
    deferred_blank: list[str] = []    # 标题与第一个子任务之间的空行：留到子任务之后再输出
    cur: str | None = None
    seen_real_content = False
    for idx, raw in enumerate(body):
        bare = raw.rstrip("\r\n")
        sm = SUBTASK_RE.match(bare)
        if sm and _parse_subtask_id(sm.group("rest"))[0]:
            cur = None
            seen_real_content = True
            continue                          # 子任务已按新顺序输出过
        if sm and not _parse_subtask_id(sm.group("rest"))[0]:
            # 无显式 id 的子任务行：new task 里同名同状态且有 id 的，就是它的新形态
            title = _parse_subtask_id(sm.group("rest"))[1]
            done = sm.group("mark").strip().lower() in ("x", "✓", "✔")
            if any((w.get("title") or "") == title and bool(w.get("done")) == done
                   for w in want_subs if w["id"] in want_ids - set(old_sub)):
                cur = None
                seen_real_content = True
                continue                      # 已在新子任务里输出过，别再当自由正文重复一遍
        key = _classify_line(bare)
        if key is not None:
            cur = key
            seen_real_content = True
            if key in leftover_new_fields:
                rendered.extend(leftover_new_fields.pop(key))
                consumed.add(key)
            continue
        if not bare.strip() and not seen_real_content:
            deferred_blank.append(raw)        # 标题行后、子任务前的空行
            continue
        if cur in consumed:
            continue                          # 「已沿用字段」的续行，别再输出一遍
        rendered.append(raw)                  # 自由正文 / 块内空行，原样保留
        if bare.strip():
            seen_real_content = True
    for key in ("updated", "due", "snapshot", "blocker", "session"):
        if key in leftover_new_fields:
            rendered.extend(leftover_new_fields[key])

    # 把「标题后空行」重新插到子任务之后（人在标题与子任务之间留的呼吸空间不丢）
    if deferred_blank and sub_lines:
        at = 1 + len(sub_lines)
        rendered[at:at] = deferred_blank

    return rendered


def _classify_line(bare: str) -> str | None:
    """把块内一行归到字段名；认不出返回 None（= 自由正文，原样保留）。"""
    if _MD_SEP_RE.match(bare):
        return "sep"
    if TASK_HEADING_RE.match(bare):
        return "heading"
    if UPDATE_LINE_RE.match(bare):
        return "updated"
    if SESSION_LINE_RE.match(bare):
        return "session"
    fm = FIELD_LINE_RE.match(bare)
    if fm:
        return KEY_TO_FIELD.get(fm.group("key"))
    return None


def _render_fields(task: dict, old: list[str], nl: str) -> dict[str, list[str]]:
    """按新值渲染字段行。值未变时直接沿用原文里的那几行（保住折行与勾选习惯）。"""
    result: dict[str, list[str]] = {}
    old_chunks: dict[str, list[str]] = {}
    cur: str | None = None
    for raw in old[1:]:
        bare = raw.rstrip("\r\n")
        key = _classify_line(bare)
        if key is not None:
            cur = key
        elif cur is None:
            continue          # 前面还没出现过任何字段：这是自由正文，不属于任何字段
        # key 为 None 但已经在某个字段之后 → 裸续行，归给最近一次出现的字段（与 parse 一致）
        chunk_key = key if key is not None else cur
        old_chunks.setdefault(chunk_key, []).append(raw if raw.endswith(("\n", "\r\n")) else raw + nl)

    def value_lines(key: str, value) -> list[str]:
        if not value:
            return []
        first, *rest = str(value).split("\n")
        label = {"due": "截止", "snapshot": "快照", "blocker": "阻塞"}[key]
        return [f"**{label}**：{first}{nl}"] + [f"{r}{nl}" for r in rest]

    # 更新行
    if task.get("updated"):
        old_upd = old_chunks.get("updated", [])
        if old_upd and _old_update_value(old_upd) == task["updated"]:
            result["updated"] = old_upd
        else:
            result["updated"] = [f"> 更新：{task['updated']}{nl}"]
    # 截止 / 快照 / 阻塞
    for key in ("due", "snapshot", "blocker"):
        val = task.get(key)
        if not val:
            continue
        old_lines = old_chunks.get(key, [])
        if old_lines and _old_field_value(old_lines) == val:
            result[key] = old_lines
        else:
            result[key] = value_lines(key, val)
    # 会话
    if task.get("session"):
        old_sess = old_chunks.get("session", [])
        if old_sess and any(task["session"] in ln for ln in old_sess):
            result["session"] = old_sess
        else:
            result["session"] = [f"**会话**：{task['session']}{nl}"]
    return result


def _old_update_value(lines: list[str]) -> str | None:
    m = UPDATE_LINE_RE.match(lines[0].rstrip("\r\n"))
    return (m.group("val") or None) if m else None


def _old_field_value(lines: list[str]) -> str | None:
    """把原文里的字段块还原成值（含裸续行），用于判断「值有没有变」。"""
    m = FIELD_LINE_RE.match(lines[0].rstrip("\r\n"))
    if not m:
        return None
    parts = [m.group("val") or ""]
    for raw in lines[1:]:
        bare = raw.rstrip("\r\n")
        fm = FIELD_LINE_RE.match(bare)
        parts.append((fm.group("val") if fm else bare) or "")
    return "\n".join(parts) if len(parts) > 1 else (parts[0] or None)


def _newline_of(lines: list[str]) -> str:
    for raw in lines:
        if raw.endswith("\r\n"):
            return "\r\n"
        if raw.endswith("\n"):
            return "\n"
    return "\n"


def serialize(tasks: list[dict]) -> str:
    """把任务列表序列化成**完整文档**：任务块 + 末尾「格式约定」节。

    ⛔ **严禁用于写文件。** 落盘唯一入口是 `write_back()`（见模块文档 / Q-06）。
    本函数只用于「从零生成一份 TASKS.md」（文件还不存在时），因为它会丢掉文件头；
    对已有文件用它覆写就是 D-01 那次事故。
    保证 parse(serialize(parse(x))) == parse(x)。
    """
    body = serialize_tasks(tasks)
    suffix = FORMAT_HEADING + "\n" + _FORMAT_EXAMPLE
    return (body + "\n\n" + suffix) if body else suffix


_FORMAT_EXAMPLE = """```markdown
## T-001 标题 · doing · 40% · proj:项目slug
- [x] T-001.1 子任务
- [ ] T-001.2 子任务
> 更新：2026-11-21 22:30
**截止**：2026-11-30
**快照**：当前进展
**阻塞**：卡点
```
"""
