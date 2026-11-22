"""host_adapter — 唯一允许接触 Hermes 宿主内部的模块。

边界规则（WBS F1 / DD-001 §11.1）：
    本文件之外，任何模块不得出现：
      · 直接 sqlite3.connect(<宿主库>)
      · 调用 host.request(...) / gateway JSON-RPC
      · 引用 Hermes 内部常量
    越界由 scripts/check_boundary.py 扫描拦截（S10）。

设计要点：
    1. 所有宿主库一律 **只读** 打开（mode=ro URI），无写句柄 —— S13
    2. 列名一律 PRAGMA table_info 探测，绝不硬编码 —— 实测 sessions 主键是 id 不是 session_id
    3. 宿主版本不匹配时降级为只读并给出中文原因，绝不让插件拖垮宿主 —— S8
    4. 插件自身异常 fail-open：适配层抛错由调用方兜住，不向上冒泡到宿主启动路径
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# 本模块打开过的只读连接（供 close_all / 幂等 close 使用）。
# 必须定义在 open_ro 之前：模块级函数体在调用时才解析名字，但放在这里更直观。
_OPEN_ROS: set = set()


# ── 宿主路径 ────────────────────────────────────────────────────────────────
# 数据根：Windows 是 %LOCALAPPDATA%\hermes，其他平台是 ~/.hermes。
# 允许用 HERMES_HOME 覆盖（测试与远程 profile 用）。


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def projects_db() -> Path:
    return hermes_home() / "projects.db"


def state_db() -> Path:
    return hermes_home() / "state.db"


def kanban_dir() -> Path:
    return hermes_home() / "kanban" / "boards"


def profiles_dir() -> Path:
    return hermes_home() / "profiles"


# ── 只读连接 ────────────────────────────────────────────────────────────────


def open_ro(path: Path) -> sqlite3.Connection:
    """以只读模式打开 SQLite（连接登记在 _OPEN_ROS，可用 close_all() 统一关闭）。

    - 用 URI 的 mode=ro 而不是普通 connect：连接本身无法执行写，这是 S13 的
      第一条判据（只读句柄），也让 check_boundary 的意图在代码里可读。
    - 为什么还要登记：sqlite3.Connection 的 `with` 只结束事务、**不关连接**
      （实测 py3.14：`with con: pass` 之后 `con.execute()` 照样能用），
      插件里遍布 `with open_ro(...)`，句柄会一直挂在宿主库文件上（Windows 表现为
      文件被占用）。登记后由 close_all() 统一释放，插件卸载/测试收尾各调一次。
    - 另：py3.14 的 `sqlite3.Connection.close` 是只读属性，无法在实例上包一层
      幂等 close，所以"重复关闭要静默"只能在调用方约定（with + close_all），
      不能靠打补丁实现。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"宿主库不存在：{p}")
    uri = f"file:{p.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    _OPEN_ROS.add(con)
    return con


def close_all() -> int:
    """关闭本模块打开过的全部连接，返回关闭个数。幂等：可重复调用。

    供测试收尾与插件卸载使用。
    """
    n = 0
    for con in list(_OPEN_ROS):
        try:
            con.close()
            n += 1
        except sqlite3.Error:
            pass
        finally:
            _OPEN_ROS.discard(con)
    return n


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    """探测真实列名。表不存在返回空 list，绝不猜。"""
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info([{table}])")]
    except sqlite3.DatabaseError:
        return []


def pick_column(cols: list[str], *candidates: str) -> str | None:
    """按候选顺序取第一个真实存在的列名。

    实测坑：sessions 的主键叫 id，不叫 session_id；时间列叫 started_at，
    不叫 created_at。硬编码任何一个都会静默返回空结果。
    """
    lowered = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def require_column(cols: list[str], *candidates: str, table: str, purpose: str) -> str:
    """取不到就抛明确错误，而不是静默返回空。"""
    got = pick_column(cols, *candidates)
    if got is None:
        raise HostSchemaError(
            f"{table} 表缺少预期列（{purpose}）；候选 {list(candidates)}；实际列 {cols}"
        )
    return got


class HostSchemaError(RuntimeError):
    """宿主库结构与插件预期不符。这类错误必须显式报出，不能吞成空结果。"""


# ── 降级告警（Q-03）────────────────────────────────────────────────────────
# 「库不存在 / 表不存在」是 fresh 首启的正常态 → 静默返回 []。
# 「表在、但部分必需列缺」不是正常态 → 字段填 None **并且**留一条人话告警，
# 由 store.overview() 汇总进 warnings[]，界面显示但不阻塞。
# 告警只属于「本次读取」：每次 read_sessions() 开头清空，读侧不累积历史噪音。

_WARNINGS: list[str] = []


def _warn(msg: str) -> None:
    if msg not in _WARNINGS:
        _WARNINGS.append(msg)


def last_warnings() -> tuple[str, ...]:
    """上一次读取产生的降级告警（只读快照）。"""
    return tuple(_WARNINGS)


def clear_warnings() -> None:
    del _WARNINGS[:]


# ── 宿主版本与兼容区间 ──────────────────────────────────────────────────────

_COMPAT_CACHE: dict | None = None


def host_version() -> str:
    """读宿主版本。

    读取顺序（前一步明确「读数了但没有」，就不再往下猜）：
      1. gateway_state.json 的 code_version —— 网关实际跑的那份，最贴近真实
      2. `hermes --version` —— 状态文件缺失时的兜底
      3. "unknown" —— 两者都没有

    关键：第 1 步只要文件存在且能解析，就以它的结论为准。
    文件存在但里面没有 code_version（例如被测试写成了 {}）时**直接返回 unknown**，
    不再去 spawn `hermes --version` —— 否则测试隔离环境会被宿主真实版本穿透，
    「无法判断版本」这个分支永远测不到。真实安装里 code_version 始终存在，不影响正常路径。
    """
    st = hermes_home() / "gateway_state.json"
    if st.exists():
        try:
            data = json.loads(st.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            v = data.get("code_version")
            if isinstance(v, str) and v.strip():
                return v.strip()
            return "unknown"          # 状态文件是我们的权威来源，它没有就是没有
    try:
        out = subprocess.run(
            ["hermes", "--version"], capture_output=True, text=True, timeout=8
        )
        m = re.search(r"(\d+\.\d+\.\d+)", (out.stdout or "") + (out.stderr or ""))
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _parse_ver(v: str) -> tuple[int, ...] | None:
    m = re.match(r"(\d+)\.(\d+)\.?(\d+)?", v.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups(default="0"))


def load_compat(compat_file: Path) -> dict:
    """读 compat.yaml（不依赖 PyYAML，手写极简解析，避免插件引入额外依赖）。"""
    global _COMPAT_CACHE
    if _COMPAT_CACHE is not None:
        return _COMPAT_CACHE
    cfg = {"host": "hermes", "min_version": "0.0.0", "max_version": "999.999.999",
           "on_mismatch": "readonly"}
    if compat_file.exists():
        for line in compat_file.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    _COMPAT_CACHE = cfg
    return cfg


def compat_status(compat_file: Path) -> dict:
    """返回 {ok, host_version, min, max, reason, readonly}。

    判定与提示位置见 DD-001 §11.1 / FS-001 §2：不匹配时 **既提示又降级**（不是二选一）。
    """
    cfg = load_compat(compat_file)
    hv = host_version()
    lo, hi = _parse_ver(cfg.get("min_version", "0")), _parse_ver(cfg.get("max_version", "999"))
    cur = _parse_ver(hv)
    if cur is None:
        # 版本读不到时按「不确定」处理：降级但不说「不兼容」，避免误报横幅
        return {"ok": True, "host_version": hv, "min": cfg.get("min_version"),
                "max": cfg.get("max_version"), "reason": "无法读取宿主版本，按兼容处理",
                "readonly": False, "warn": True}
    ok = (lo is None or cur >= lo) and (hi is None or cur <= hi)
    reason = ""
    if not ok:
        reason = (f"宿主版本 {hv} 不在兼容区间 "
                  f"[{cfg.get('min_version')}, {cfg.get('max_version')}]，已进入只读模式")
    return {"ok": ok, "host_version": hv, "min": cfg.get("min_version"),
            "max": cfg.get("max_version"), "reason": reason,
            "readonly": (not ok) and cfg.get("on_mismatch", "readonly") == "readonly",
            "warn": False}


# ── 业务读取：projects / sessions / kanban ─────────────────────────────────

PROJECT_FIELDS = ("slug", "name", "id", "path", "primary_path", "root_path", "workdir",
                  "board_slug", "icon", "created_at", "updated_at", "archived")


def read_projects() -> list[dict]:
    """读全部项目。只读。

    注意：不用 `with open_ro(...)` —— 它的 with 不关连接（见 open_ro 文档），
    一律 try/finally 显式 close，避免句柄挂在宿主库上。

    实测坑（2026-11-21 本机）：项目主目录列叫 `primary_path`，**不叫 path**。
    候选里漏掉它会让 14 个项目的 path 全返回 None，A3 的 cwd 归属规则随之全部失效。
    `archived` 一并读：归档项目仍在表里，展示层要能区分。
    """
    con = open_ro(projects_db())
    try:
        cols = columns(con, "projects")
        if not cols:
            return []
        slug = require_column(cols, "slug", "name", table="projects", purpose="项目标识")
        name = pick_column(cols, "name", "title") or slug
        board = pick_column(cols, "board_slug", "board")
        path = pick_column(cols, "primary_path", "path", "root_path", "workdir", "dir")
        archived = pick_column(cols, "archived", "is_archived")
        sel = [slug, name]
        for extra in (board, path, archived):
            if extra and extra not in sel:
                sel.append(extra)
        sql = "SELECT " + ", ".join(f"[{c}]" for c in sel) + " FROM projects"
        rows = []
        for r in con.execute(sql):
            rows.append({
                "slug": r[slug],
                "name": r[name],
                "board_slug": r[board] if board else None,
                "path": r[path] if path else None,
                "archived": bool(r[archived]) if archived else False,
            })
        return rows
    finally:
        con.close()
        _OPEN_ROS.discard(con)


def read_sessions(limit: int | None = None) -> list[dict]:
    """读会话。只读。

    关键：主键列实测叫 id。时间列实测叫 started_at（66 列里没有 created_at）。

    Q-03 降级策略（不是二选一）：
      · 库文件不存在 / 表不存在 → 返回 []，**静默**（fresh 首启的正常态）
      · 主键列缺失 → HostSchemaError（连「是哪条会话」都答不了，不能继续）
      · 时间 / cwd / 标题类列缺失 → 该字段 None + 一条人话告警，其余字段照常返回
    """
    clear_warnings()
    db = state_db()
    if not db.exists():
        return []
    con = open_ro(db)
    try:
        cols = columns(con, "sessions")
        if not cols:
            return []
        pk = require_column(cols, "id", "session_id", table="sessions", purpose="会话主键")
        cwd = pick_column(cols, "cwd")
        started = pick_column(cols, "started_at", "created_at", "created", "timestamp")
        title = pick_column(cols, "title", "name", "summary")
        updated = pick_column(cols, "updated_at", "last_active_at", "last_activity_at")

        if not cwd:
            _warn("sessions 表缺 cwd，会话归属无法按目录判定")
        if not started:
            _warn("sessions 表缺 started_at，活动时间与 fresh 作用域不可用")
        if not updated:
            _warn("sessions 表缺 updated_at，最近活动时间取 started_at 代替")

        sel = [c for c in (pk, cwd, started, title, updated) if c]
        sql = "SELECT " + ", ".join(f"[{c}]" for c in sel) + " FROM sessions"
        if started:
            sql += f" ORDER BY [{started}] DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [
            {
                "id": r[pk],
                "cwd": r[cwd] if cwd else None,
                "started_at": r[started] if started else None,
                "title": r[title] if title else "",
                "updated_at": r[updated] if updated else None,
            }
            for r in con.execute(sql)
        ]
    finally:
        con.close()
        _OPEN_ROS.discard(con)



def read_boards() -> list[dict]:
    """列出看板目录。只读扫描，不开写连接。"""
    root = kanban_dir()
    if not root.is_dir():
        return []
    boards = []
    for d in sorted(root.iterdir()):
        db = d / "kanban.db"
        if db.is_file():
            boards.append({"slug": d.name, "db": str(db)})
    return boards


def read_board_tasks(slug: str) -> list[dict]:
    """读某看板的卡。只读 mode=ro。卡数用于 S13 的逐板计数比对。"""
    db = kanban_dir() / slug / "kanban.db"
    if not db.is_file():
        return []
    con = open_ro(db)
    try:
        cols = columns(con, "tasks")
        if not cols:
            return []
        pk = pick_column(cols, "id", "task_id") or "rowid"
        status = pick_column(cols, "status")
        title = pick_column(cols, "title", "name")
        project = pick_column(cols, "project_id", "project")
        sel = [c for c in (pk, status, title, project) if c]
        if not sel:
            return []
        sql = "SELECT " + ", ".join(f"[{c}]" for c in sel) + " FROM tasks"
        return [
            {"id": r[pk], "status": r[status] if status else None,
             "title": r[title] if title else "",
             "project_id": r[project] if project else None}
            for r in con.execute(sql)
        ]
    finally:
        con.close()
        _OPEN_ROS.discard(con)


# ── gateway JSON-RPC（新建 / 删除会话）─────────────────────────────────────
#
# 实测（2026-11-22，宿主 0.23.2）：本机网关**没有 HTTP 端口** ——
# gateway_state.json 里没有 port 字段，netstat 也没有 8000 在听。对外面是本地
# **WebSocket**，客户端要先经控制 socket 换一张 session-ticket 才能连上。
# 所以「读 port → 试 /rpc、/jsonrpc、/api/rpc」这套写法是自造的契约，永远连不上；
# 正确做法是用宿主自己发布的官方客户端 hermes_cli.gateway_client.connect_gateway。


class GatewayError(RuntimeError):
    pass


def _host_agent_root() -> Path | None:
    """Hermes 安装树（含 gateway/ 与 hermes_cli/ 的那一层）。

    注意 **不要只看环境里的 HERMES_HOME**：测试会把 HERMES_HOME 指到临时目录，
    而安装树永远在真实数据根下。同时看 `hermes_home()` 的原生值（%LOCALAPPDATA%\\hermes）。
    """
    env = os.environ.get("HERMES_AGENT_ROOT")
    if env and (Path(env) / "hermes_cli").is_dir():
        return Path(env)
    roots: list[Path] = []
    home = os.environ.get("HERMES_HOME")
    if home:
        roots.append(Path(home))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        roots.append(Path(local) / "hermes")
    else:
        roots.append(Path.home() / ".hermes")
    roots.append(hermes_home())
    for r in roots:
        cand = r / "hermes-agent"
        if (cand / "hermes_cli").is_dir():
            return cand
    return None


def _gateway_call(method: str, params: dict, timeout: float = 60.0) -> dict:
    """在官方客户端上发一次 JSON-RPC，同步等待结果。"""
    import asyncio

    root = _host_agent_root()
    if root is None:
        raise GatewayError("找不到宿安装树（HERMES_AGENT_ROOT / <hermes_home>/hermes-agent）")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from hermes_cli.gateway_client import connect_gateway
    except ImportError as e:                 # pragma: no cover - 安装树异常时才发生
        raise GatewayError(f"宿主网关客户端不可用：{e}") from e

    async def _run():
        async with connect_gateway() as client:
            return await client.rpc(method, _timeout=timeout, **params)

    try:
        return asyncio.run(_run())
    except GatewayError:
        raise
    except Exception as e:                   # noqa: BLE001 - 一律转成人话，交给路由层
        raise GatewayError(f"网关调用 {method} 失败：{type(e).__name__}: {e}") from e


def create_session(cwd: str, *, title: str | None = None,
                   timeout: float = 60.0) -> dict:
    """在指定 cwd 新建会话 —— B3 的唯一实现。

    实测硬约束（FS-001 §11.1~11.4）：
      ① cwd 必须是绝对路径且目录存在，否则宿主返回 invalid_params
      ② **必须显式传 cwd_explicit=True**，否则 TERMINAL_CWD 不保证落地
      ③ 绝不能传 cwd_explicit=False —— 会被 _profile_workspace_cwd 覆盖掉 cwd
    """
    p = Path(cwd)
    if not p.is_absolute() or not p.is_dir():
        raise GatewayError(f"cwd 必须是存在目录的绝对路径：{cwd!r}")
    params = {"cwd": str(p), "cwd_explicit": True, "source": "gui"}
    if title:
        params["title"] = title
    return _gateway_call("session.create", params, timeout=timeout)


def delete_session(session_id: str, *, timeout: float = 30.0) -> dict:
    """删掉一个会话（仅用于清理我们自己造出来的测试会话）。

    `session.mutate` 的契约要求四个必填字段：session_id / request_id /
    expected_revision / operation / payload；**缺 expected_generation 会直接 invalid_params**
    （这是 2026-11-22 实测踩到的坑，不是可选参数）。
    """
    import uuid

    info = _gateway_call("session.info", {"session_id": session_id}, timeout=timeout) or {}
    meta = info.get("info", {}) if isinstance(info, dict) else {}
    params = {
        "session_id": session_id,
        "request_id": uuid.uuid4().hex,
        "expected_revision": int(meta.get("revision", 0) or 0),
        "expected_generation": int(meta.get("execution_generation", 0) or 0),
        "operation": "delete",
        "payload": {},
    }
    return _gateway_call("session.mutate", params, timeout=timeout)


def session_cwd(session_id: str) -> str | None:
    """回查会话实际 cwd —— B3 验收用，不信返回值只信库。

    注意：库里存的是 **JSON 字符串**（`"C:\\\\Users\\\\..."` 双反斜杠），
    原样返回会让 `abspath()` 失败、与期望路径永远比不相等。这里解一层 JSON。
    """
    db = state_db()
    if not db.exists() or not session_id:
        return None
    con = open_ro(db)
    try:
        cols = columns(con, "sessions")
        if not cols:
            return None
        pk = require_column(cols, "id", "session_id", table="sessions", purpose="会话主键")
        cwd = pick_column(cols, "cwd")
        if not cwd:
            return None
        row = con.execute(
            f"SELECT [{cwd}] FROM sessions WHERE [{pk}] = ?", (session_id,)
        ).fetchone()
    finally:
        con.close()
        _OPEN_ROS.discard(con)
    return _unquote_path(row[0]) if row else None


def _unquote_path(value) -> str | None:
    """把库里存的 JSON/带引号路径还原成普通字符串。"""
    if value is None:
        return None
    s = str(value)
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    return s



# ── 系统提示注入（E2）唯一写入口 ────────────────────────────────────────────
#
# 宿主只承认一个「可写 prompt」的通道：ctx.register_system_prompt_section。
# 实测（宿主 0.23.2，hermes_cli/plugins_context.py:783）：
#   · id 必须匹配 ^[a-z0-9][a-z0-9._-]{0,127}$，**全局唯一**，重复注册抛 ValueError
#   · content 可以是 str 或 callable(session_info) -> str；callable 拿只读映射
#   · max_chars 上限 4000（DEFAULT/MAX_SYSTEM_PROMPT_SECTION_CHARS）
#   · 返回**空串**的 section 被宿主直接丢弃（plugins_dispatch._render_prompt_section_text），
#     这是「未归项目时不污染 prompt」的官方手段，不需要自己拼「无」字
#   · on_session_start 是纯观察钩子，返回值被 ignore —— 拿它注入必然无效
#
# 本文件是唯一允许碰宿主 SDK 的地方（S10），所以 register 只转调这里。

SECTION_ID = "hermes-cockpit.project"
SECTION_MAX_CHARS = 4000


def register_system_prompt_section(ctx, render) -> object:
    """把 *render* 挂成系统提示段落，返回宿主的 PluginRegistration 句柄。

    *render* 的签名为 ``render(session_info: Mapping[str, str]) -> str``。
    签名/参数不合规时宿主抛 ValueError/TypeError，这里**不吞**：注册失败必须让
    调用方（__init__.register）看见，否则会变成「装了但没注入」的静默失败。
    """
    return ctx.register_system_prompt_section(
        SECTION_ID, render, position="after_memory", max_chars=SECTION_MAX_CHARS)


# ── 时间工具 ────────────────────────────────────────────────────────────────


def to_epoch(value) -> float | None:
    """把宿主的时间戳统一成 epoch 秒。

    实测 started_at 是 unix 秒浮点（如 1795271116.479668）；其他宿主版本可能给
    ISO 字符串或毫秒整数 —— 三种都认，认不出返回 None（调用方按「无法判断」处理）。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    if not s:
        return None
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    except ValueError:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def now_iso() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
