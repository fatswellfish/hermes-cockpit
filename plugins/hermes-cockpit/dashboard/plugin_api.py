"""plugin_api — B 模块路由层（纯 REST，不碰宿主）。

宿主访问全部经 `host_adapter`（S10 / scripts/check_boundary.py 扫描）；本文件不许
`import sqlite3`，也不许自己拼宿主路径（`ctx.rest` 是前端调**这里**，与本层无关）。

端点（WBS B1~B4）：

| 方法   | 路径             | 作用                                              |
|--------|------------------|---------------------------------------------------|
| GET    | /overview        | store.overview()                                  |
| POST   | /tasks/toggle    | 勾选/取消子任务 → recompute → write_back 落盘      |
| POST   | /tasks/add       | 新增任务                                          |
| POST   | /tasks/update    | 改状态/快照/阻塞/截止                              |
| DELETE | /tasks/{id}      | 删任务                                            |
| POST   | /sessions/assign | 会话归类 → 写 META.yaml session_map（**不改 cwd**） |
| POST   | /sessions/new    | 项目内新建会话（**必须 cwd_explicit=True**）        |
| POST   | /setup/init      | 首启引导（**先写 at 再写 mode**）                   |

三条硬要求：
  1. **并发保护**：所有写操作都要带 `If-Unmodified-Since`（= 文件戳）或 `expect_mtime`，
     与磁盘上的实际状态不一致就返回 **409**，绝不静默覆盖（fileio.StaleWrite → 409）。
  2. **作用域**：`mode=fresh` 且 `setup.at` 为空时作用域为空（Q-04）；fresh 下对存量会话
     归类 → **403**，越界不写盘。
  3. **不信返回值**：`/sessions/new` 建完必须回查 `sessions.cwd`，不一致就报错并删除
     刚建的那个会话（测试造出来的东西不能留在真库里）。

响应约定：错误一律 `{"detail": "人话", "code": "<机器码>"}`，HTTP 状态码 4xx/5xx。
  · 409 stale          —— 文件被别人改过，请刷新
  · 403 out_of_scope   —— fresh 作用域外的会话不可归类
  · 409 scope_blocked  —— 引导状态异常 / 引导未完成，先走 /setup/init
  · 502 gateway_error  —— 宿主网关拒绝或不可达（建会话）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# 插件目录内的相对导入：desktop 宿主把本文件当作独立模块加载，包名不一定可用，
# 所以与 store.py 同样的办法显式补 sys.path。
_HERE = Path(__file__).resolve().parent
_PLUGIN_ROOT = _HERE.parent
for _p in (_HERE, _PLUGIN_ROOT / "core"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:                                        # pragma: no cover - 宿主里必然可用
    from fastapi import APIRouter, Body, Request
    from fastapi.responses import JSONResponse
    router = APIRouter()
except Exception:                           # pragma: no cover - 无 fastapi 时供纯逻辑测试导入
    APIRouter = None                        # type: ignore[assignment]
    router = None                           # type: ignore[assignment]
    JSONResponse = None                     # type: ignore[assignment]

import fileio as fio  # noqa: E402
import host_adapter as ha  # noqa: E402
import meta as meta_mod  # noqa: E402
import progress as pg  # noqa: E402
import store  # noqa: E402
import taskfile as tf  # noqa: E402

STATUSES = tf.STATUSES


# ── 错误 ────────────────────────────────────────────────────────────────────


class ApiError(Exception):
    """带 HTTP 状态码的业务错误。逻辑层抛它，路由层转成响应（便于无 fastapi 时单测）。"""

    def __init__(self, status: int, code: str, detail: str):
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _err(status: int, code: str, detail: str) -> dict:
    return {"detail": detail, "code": code}


# ── 并发保护 ────────────────────────────────────────────────────────────────


def _check_stamp(path: Path, expect_mtime: Any, if_unmodified_since: Any) -> Any:
    """校验客户端手上的戳，返回当前戳。**两种戳按各自精度还原**：

    · `expect_mtime` 给二元组 `(ident, sha256)` —— 与磁盘精确比对（fileio 的戳）。
      Windows 上连续两次 os.replace 可能落在同一时钟 tick（fileio 实测 70/200），
      只比 mtime 会把「别人刚改过」判成「没变」，所以内容哈希必须一起比。
    · `If-Unmodified-Since` 是 HTTP 语义的**毫秒**时间戳（客户端可能给纳秒/毫秒两种），
      两边都折成**秒级浮点**再比 —— 文件系统把纳秒截成 100ns 粒度，
      直接相等会让同一个文件在自己的戳上判自己 stale。
    """
    current = fio._raw_stamp(path)
    if expect_mtime is None and if_unmodified_since is None:
        raise ApiError(428, "precondition_required",
                       "写操作必须带 If-Unmodified-Since 或 expect_mtime（拒绝盲写）")

    if expect_mtime is not None:
        if isinstance(expect_mtime, (list, tuple)) and len(expect_mtime) == 2:
            if (str(expect_mtime[0]), str(expect_mtime[1])) != current:
                raise ApiError(409, "stale", "文件已被他人修改，请刷新后重试")
            return current
        given = expect_mtime
    else:
        given = if_unmodified_since

    try:
        want = float(given)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_precondition", f"戳格式不对：{given!r}") from None
    if want > 1e11:                     # 纳秒
        want /= 1e9
    elif want > 1e9:                    # 毫秒
        want /= 1e3
    got = int(current[0].split(":mtime=")[-1]) / 1e9
    if abs(got - want) > 0.001:         # 1ms 内的抖动视为同一版（HTTP 语义就是毫秒）
        raise ApiError(409, "stale", "文件已被他人修改，请刷新后重试")
    return current


def _read_tasks_with_stamp(root: Path) -> tuple[str, list[dict], Any]:
    p = root / store.TASKS_FILENAME
    if not p.is_file():
        raise ApiError(404, "no_taskfile", f"{p} 不存在")
    text, stamp = fio.read_with_stamp(p)
    return text, tf.parse(text), stamp


def _write_tasks(root: Path, original: str, tasks: list[dict], expect: Any) -> Any:
    """落盘唯一入口：taskfile.write_back（**绝不调 serialize**，Q-06）。"""
    p = root / store.TASKS_FILENAME
    try:
        return fio.write_if_unchanged(p, tf.write_back(original, tasks), expect)
    except fio.StaleWrite as e:
        raise ApiError(409, "stale", str(e)) from e


# ── 作用域 ──────────────────────────────────────────────────────────────────


def _setup(root: Path) -> dict:
    return store.setup_state(root)


def _guard_writable(root: Path, setup: dict | None = None) -> dict:
    """写入口的前置条件：引导异常（fresh 缺 at）时拒绝写，避免造出越界数据。"""
    setup = setup if setup is not None else _setup(root)
    warns = store.scope_warnings(setup)
    if warns:
        raise ApiError(409, "scope_blocked", warns[0])
    return setup


def _in_fresh_scope_excluded(session_started: Any, setup: dict) -> bool:
    """该会话是否在当前作用域之外（Q-04：fresh 下存量会话不可归类）。"""
    start = store.scope_at(setup)
    if start is None:
        return False
    return not store.in_scope({"started_at": session_started}, start)


# ── 任务变换（纯函数，路由只做校验与落盘）──────────────────────────────────


def _find(tasks: list[dict], task_id: str) -> dict:
    for t in tasks:
        if t["id"] == task_id:
            return t
    raise ApiError(404, "task_not_found", f"任务不存在：{task_id}")


def _next_task_id(tasks: list[dict], project: str | None) -> str:
    """新任务 id：沿用文件里已有的数字前缀（T- / D- …），取全局最大序号 +1。"""
    best_prefix, best = "T-", 0
    for t in tasks:
        m = tf.TASK_ID_RE.match(t["id"])
        if not m:
            continue
        best_prefix = best_prefix if t.get("project") != project else m.group(1)
        best = max(best, int(m.group(2)))
    return f"{best_prefix}{best + 1:03d}"


def toggle_subtask(tasks: list[dict], task_id: str, sub_id: str,
                   done: bool | None = None) -> list[dict]:
    """勾选/取消子任务 → 重算父任务（Q-07）。返回新任务列表（入参不变）。"""
    out = [dict(t) for t in tasks]
    task = _find(out, task_id)
    subs = [dict(s) for s in (task.get("subtasks") or [])]
    hit = next((s for s in subs if s["id"] == sub_id), None)
    if hit is None:
        raise ApiError(404, "subtask_not_found", f"子任务已不在该任务下：{sub_id}")
    hit["done"] = (not hit["done"]) if done is None else bool(done)
    task["subtasks"] = subs
    idx = out.index(task)
    out[idx] = pg.recompute(task)
    return out


def add_task(tasks: list[dict], payload: dict) -> tuple[list[dict], str]:
    """新增任务（id 可省略；省略时自动编号）。返回 (新任务列表, 新 id)。"""
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ApiError(400, "bad_request", "新增任务必须给 title")
    task_id = str(payload.get("id") or "").strip() or _next_task_id(tasks, payload.get("project"))
    if any(t["id"] == task_id for t in tasks):
        raise ApiError(409, "duplicate_id", f"任务 id 已存在：{task_id}")
    subs = []
    for i, s in enumerate(payload.get("subtasks") or [], 1):
        if isinstance(s, str):
            subs.append({"id": f"{task_id}.{i}", "title": s, "done": False})
        else:
            subs.append({"id": str(s.get("id") or f"{task_id}.{i}"),
                         "title": str(s.get("title") or ""),
                         "done": bool(s.get("done"))})
    task = tf._empty_task()
    task.update({
        "id": task_id, "title": title,
        "status": str(payload.get("status") or "todo").lower(),
        "progress": int(payload.get("progress") or 0),
        "project": payload.get("project"),
        "subtasks": subs,
        "due": payload.get("due"),
        "snapshot": payload.get("snapshot"),
        "blocker": payload.get("blocker"),
    })
    task = pg.recompute(task)
    out = [dict(t) for t in tasks] + [task]
    return out, task_id


_UPDATABLE = ("status", "progress", "snapshot", "blocker", "due", "title", "project")


def update_task(tasks: list[dict], task_id: str, payload: dict) -> list[dict]:
    """改状态/快照/阻塞/截止。空串一律写回 None（字段行被删掉而不是留个空标签）。"""
    out = [dict(t) for t in tasks]
    task = _find(out, task_id)
    touched = False
    for key in _UPDATABLE:
        if key not in payload:
            continue
        val = payload[key]
        if key == "status":
            val = str(val).lower()
            if val not in STATUSES:
                raise ApiError(400, "bad_request", f"status 只能取 {list(STATUSES)}")
            task["status"] = val
        elif key == "progress":
            try:
                task["progress"] = max(0, min(100, int(val)))
            except (TypeError, ValueError):
                raise ApiError(400, "bad_request", f"progress 必须是 0~100 的整数：{val!r}") from None
        else:
            task[key] = (str(val) if val not in (None, "") else None)
        touched = True
    if not touched:
        raise ApiError(400, "bad_request", f"没有可更新的字段，接受 {list(_UPDATABLE)}")
    idx = out.index(task)
    # 子任务存在时按 Q-07 重算；调用方显式改 status 时以调用方为准（人工干预优先）
    if "status" not in payload:
        out[idx] = pg.recompute(task)
    return out


def delete_task(tasks: list[dict], task_id: str) -> list[dict]:
    _find(tasks, task_id)
    return [dict(t) for t in tasks if t["id"] != task_id]


# ── 会话与设置 ──────────────────────────────────────────────────────────────


def assign_session(root: Path, session_id: str, project: str,
                   source: str = "manual", now: str | None = None) -> dict:
    """会话归类：只写 META.yaml 的 session_map，**不改 cwd、不动宿主任何数据**。

    Q-04：fresh 下作用域外的会话 → 403（越界不写盘）。
    """
    setup = _guard_writable(root)
    if _in_fresh_scope_excluded(_session_started(session_id), setup):
        raise ApiError(403, "out_of_scope",
                       "该会话在 fresh 作用域之外（早于 setup.at），不可归类")
    if source not in ("manual", "import-confirmed", "suggested-confirmed"):
        raise ApiError(400, "bad_request", f"source 取值非法：{source}")
    path = store.meta_path(root)
    try:
        meta_mod.set_session_project(path, session_id, project, source=source, at=now)
    except meta_mod.MetaWriteError as e:
        raise ApiError(400, "meta_write_failed", str(e)) from e
    entry = meta_mod.find_session(path, session_id)
    return {"session_id": session_id, "project": project, "source": source,
            "entry": entry, "cwd_changed": False}


def _session_started(session_id: str) -> Any:
    for s in ha.read_sessions():
        if s.get("id") == session_id:
            return s.get("started_at")
    return None


def new_session(root: Path, cwd: str | None = None, title: str | None = None,
                setup: dict | None = None) -> dict:
    """项目内新建会话 —— 调用 gateway `session.create`，**cwd_explicit=True** 必传。

    硬要求（FS-001 §11.4 / 任务书任务 4）：
      ① cwd 必须绝对路径且 is_dir()
      ② 必传 cwd_explicit: True（False 会被 profile workspace 覆盖）
      ③ **建完回查 sessions.cwd**，不一致就报错 —— 不信返回值
      ④ 回查不一致时把刚建的会话删掉（我们的测试产物不能留在真库里）
    """
    setup = _guard_writable(root, setup)
    target = Path(cwd) if cwd else Path(store.project_root(root))
    if not target.is_absolute() or not target.is_dir():
        raise ApiError(400, "bad_cwd", f"cwd 必须是存在目录的绝对路径：{str(target)!r}")

    try:
        result = ha.create_session(str(target), title=title)
    except ha.GatewayError as e:
        raise ApiError(502, "gateway_error", str(e)) from e

    session_id = (result or {}).get("session_id") or (result or {}).get("id")
    if not session_id:
        raise ApiError(502, "gateway_error", f"宿主没有返回 session_id：{result!r}")

    actual = ha.session_cwd(session_id)
    if not actual or os.path.normcase(os.path.abspath(actual)) != os.path.normcase(
            os.path.abspath(str(target))):
        # 落地失败：先删掉这个半成品会话，再报错（不让测试对象留在用户库里）
        deleted = False
        try:
            deleted = bool(ha.delete_session(session_id))
        except ha.GatewayError:
            deleted = False
        raise ApiError(502, "cwd_not_landed",
                       f"会话 {session_id} 的 cwd 未落地：期望 {str(target)!r}，库里是 {actual!r}"
                       f"（{'已删除该会话' if deleted else '删除失败，需人工清理'}）")

    return {"session_id": session_id, "cwd": actual, "requested_cwd": str(target),
            "cwd_explicit": True, "setup": setup.get("mode")}


def init_setup(root: Path, mode: str, at: str | None = None) -> dict:
    """首启引导：写 setup.status / mode。**先 at 后 mode**（Q-04 顺序硬约束）。"""
    mode = (mode or "").strip().lower()
    if mode not in ("fresh", "imported"):
        raise ApiError(400, "bad_request", "mode 只能取 fresh / imported")
    path = store.meta_path(root)
    at = at or ha.now_iso()
    try:
        meta_mod.set_setup(path, status="done", mode=mode, at=at)
    except meta_mod.MetaWriteError as e:
        # at 已存在（单向门）：只切 status/mode，不碰 at
        if "at 已存在" in str(e):
            meta_mod.set_setup(path, status="done", mode=mode)
        else:
            raise ApiError(400, "meta_write_failed", str(e)) from e
    cfg = meta_mod.load(path)["setup"]
    return {"setup": {"status": cfg.get("status"), "mode": cfg.get("mode"),
                      "at": cfg.get("at")},
            "warnings": store.scope_warnings(cfg)}


# ── 路由 ────────────────────────────────────────────────────────────────────


def _root(request_root: str | None = None) -> Path:
    return Path(store.project_root(request_root))


# ── 宿主入口 handle() ───────────────────────────────────────────────────────
#
# 本机那条宿主路径（`web_server_dashboard.py::_mount_one_plugin_api` 的
# `getattr(mod, "router")` 分支）要点的是 `router`（FastAPI APIRouter），我们两个都给。
# 但 `plugins.isolation: host` 与部分宿主形态会把 backend 当普通模块、调
# `handle(method, path, body) -> dict`（见本仓 PLUGIN-API-约定.md 与现役 todo-panel）。
# 所以同一个逻辑层出两种入口，「上层写在哪个函数里」不影响任何一条判据。

def _overview_with_stamp(root: Path) -> dict:
    """overview() + 并发戳。

    戳必须在**这一层**附加，不能进 store.overview()：store 是纯只读聚合层，
    不该引 fileio 的写路径依赖（S10 边界）。而写端点校验用的正是
    `_read_tasks_with_stamp()` 返回的同一个戳，两边同源才不会自相矛盾的 409。
    """
    payload = store.overview(root)
    try:
        _, _, stamp = _read_tasks_with_stamp(root)
        payload["stamp"] = list(stamp)
    except ApiError:
        payload["stamp"] = None      # 没有 TASKS.md：前端会拿到 428 并给出明确提示
    return payload


def _handle_route(method: str, path: str, body: dict | None) -> tuple[int, dict]:
    """把 (method, path, body) 路由到一个返回 (HTTP 状态码, dict) 的处理。"""
    m = (method or "GET").upper()
    p = "/" + (path or "/").strip("/")
    b = body or {}

    if m == "GET" and p == "/overview":
        return _handle(lambda: _overview_with_stamp(_root(b.get("root"))))

    if m == "POST" and p in ("/tasks/toggle", "/tasks/add", "/tasks/update"):
        def run_tasks():
            root = _root(b.get("root"))
            _guard_writable(root)
            text, tasks, stamp = _read_tasks_with_stamp(root)
            _check_stamp(root / store.TASKS_FILENAME, b.get("expect_mtime"),
                         b.get("if_unmodified_since"))
            if p == "/tasks/toggle":
                new = toggle_subtask(tasks, b.get("id"), b.get("subtask_id"), b.get("done"))
                return {"task": _find(new, b["id"]), "stamp": list(_write_tasks(root, text, new, stamp))}
            if p == "/tasks/add":
                new, task_id = add_task(tasks, b)
                return {"id": task_id, "task": _find(new, task_id),
                        "stamp": list(_write_tasks(root, text, new, stamp))}
            new = update_task(tasks, b.get("id"), b)
            return {"task": _find(new, b["id"]), "stamp": list(_write_tasks(root, text, new, stamp))}
        return _handle(run_tasks)

    if m == "DELETE" and p.startswith("/tasks/") and p.count("/") == 2:
        task_id = p.rsplit("/", 1)[-1]

        def run_delete():
            root = _root(b.get("root"))
            _guard_writable(root)
            text, tasks, stamp = _read_tasks_with_stamp(root)
            _check_stamp(root / store.TASKS_FILENAME, b.get("expect_mtime"),
                         b.get("if_unmodified_since"))
            new = delete_task(tasks, task_id)
            return {"deleted": task_id, "stamp": list(_write_tasks(root, text, new, stamp))}
        return _handle(run_delete)

    if m == "POST" and p == "/sessions/assign":
        def run_assign():
            sid, project = b.get("session_id"), b.get("project")
            if not sid or not project:
                raise ApiError(400, "bad_request", "必须给 session_id 与 project")
            return assign_session(_root(b.get("root")), sid, project,
                                  source=b.get("source") or "manual", now=b.get("at"))
        return _handle(run_assign)

    if m == "POST" and p == "/sessions/new":
        return _handle(lambda: new_session(_root(b.get("root")), cwd=b.get("cwd"),
                                           title=b.get("title")))

    if m == "POST" and p == "/setup/init":
        return _handle(lambda: init_setup(_root(b.get("root")), b.get("mode"), b.get("at")))

    return 404, _err(404, "unknown_route", f"未知路径 {m} {p}")


def handle(method: str, path: str, body: dict | None = None) -> dict:
    """宿主普通模块式入口：返回 JSON-ready dict，**不抛异常**。

    错误形态：`{"ok": False, "code": 409|403|..., "error": "人话"}`（HTTP 状态码放在
    `code` 里，因为这条路没有 HTTP 层）；成功形态：`{"ok": True, ...payload}`。
    """
    status, payload = _handle_route(method, path, body)
    if status >= 400:
        # 错误形态固定为：ok=False + code=HTTP 状态码 + error=人话 + reason=机器码
        # （code 位必须留整数：前端与测试都按它判 409/403/428）
        return {"ok": False, "code": status,
                "error": payload.get("detail", ""),
                "reason": payload.get("code", "")}
    return {"ok": True, **payload}


def _precondition(request: Any, body: dict | None) -> tuple[Any, Any]:
    """从 query / body 里取两个可选的戳（都给了就以 expect_mtime 为准）。"""
    body = body or {}
    header = None
    if request is not None:
        try:
            header = request.headers.get("If-Unmodified-Since")
        except Exception:                   # pragma: no cover - 非请求对象
            header = None
    return body.get("expect_mtime"), header


def _handle(fn):
    """把 ApiError 转成 (状态码, JSON) —— 逻辑层因此可以脱离 fastapi 单测。"""
    try:
        return 200, fn()
    except ApiError as e:
        return e.status, _err(e.status, e.code, e.detail)


if router is not None:                      # pragma: no cover - 需要 fastapi

    @router.get("/overview")
    def overview(request: Request, root: str | None = None):
        # root 允许由外部指定（测试沙盒与多项目根场景）；不传就走默认根
        status, payload = _handle(lambda: _overview_with_stamp(
            _root(root or request.query_params.get("root"))))
        return JSONResponse(payload, status_code=status)

    @router.post("/tasks/toggle")
    def tasks_toggle(request: Request, body: dict = Body(...)):
        expect, header = _precondition(request, body)

        def run():
            root = _root(body.get("root"))
            _guard_writable(root)
            text, tasks, stamp = _read_tasks_with_stamp(root)
            _check_stamp(root / store.TASKS_FILENAME, expect, header)
            new = toggle_subtask(tasks, body.get("id"), body.get("subtask_id"),
                                 body.get("done"))
            new_stamp = _write_tasks(root, text, new, stamp)
            task = _find(new, body["id"])
            return {"task": task, "stamp": list(new_stamp)}

        status, payload = _handle(run)
        return JSONResponse(payload, status_code=status)

    @router.post("/tasks/add")
    def tasks_add(request: Request, body: dict = Body(...)):
        expect, header = _precondition(request, body)

        def run():
            root = _root(body.get("root"))
            _guard_writable(root)
            text, tasks, stamp = _read_tasks_with_stamp(root)
            _check_stamp(root / store.TASKS_FILENAME, expect, header)
            new, task_id = add_task(tasks, body)
            new_stamp = _write_tasks(root, text, new, stamp)
            return {"id": task_id, "task": _find(new, task_id), "stamp": list(new_stamp)}

        status, payload = _handle(run)
        return JSONResponse(payload, status_code=status)

    @router.post("/tasks/update")
    def tasks_update(request: Request, body: dict = Body(...)):
        expect, header = _precondition(request, body)

        def run():
            root = _root(body.get("root"))
            _guard_writable(root)
            text, tasks, stamp = _read_tasks_with_stamp(root)
            _check_stamp(root / store.TASKS_FILENAME, expect, header)
            task_id = body.get("id")
            new = update_task(tasks, task_id, body)
            new_stamp = _write_tasks(root, text, new, stamp)
            return {"task": _find(new, task_id), "stamp": list(new_stamp)}

        status, payload = _handle(run)
        return JSONResponse(payload, status_code=status)

    @router.delete("/tasks/{task_id}")
    def tasks_delete(task_id: str, request: Request, body: dict = Body(default=None)):
        expect, header = _precondition(request, body)

        def run():
            root = _root((body or {}).get("root"))
            _guard_writable(root)
            text, tasks, stamp = _read_tasks_with_stamp(root)
            _check_stamp(root / store.TASKS_FILENAME, expect, header)
            new = delete_task(tasks, task_id)
            new_stamp = _write_tasks(root, text, new, stamp)
            return {"deleted": task_id, "stamp": list(new_stamp)}

        status, payload = _handle(run)
        return JSONResponse(payload, status_code=status)

    @router.post("/sessions/assign")
    def sessions_assign(body: dict = Body(...)):
        def run():
            sid = body.get("session_id")
            project = body.get("project")
            if not sid or not project:
                raise ApiError(400, "bad_request", "必须给 session_id 与 project")
            return assign_session(_root(body.get("root")), sid, project,
                                  source=body.get("source") or "manual",
                                  now=body.get("at"))

        status, payload = _handle(run)
        return JSONResponse(payload, status_code=status)

    @router.post("/sessions/new")
    def sessions_new(body: dict = Body(...)):
        def run():
            return new_session(_root(body.get("root")), cwd=body.get("cwd"),
                               title=body.get("title"))

        status, payload = _handle(run)
        return JSONResponse(payload, status_code=status)

    @router.post("/setup/init")
    def setup_init(body: dict = Body(...)):
        def run():
            return init_setup(_root(body.get("root")), body.get("mode"), body.get("at"))

        status, payload = _handle(run)
        return JSONResponse(payload, status_code=status)
