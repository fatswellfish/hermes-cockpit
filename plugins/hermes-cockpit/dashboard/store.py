"""store — A3 聚合视图（overview）。

把四类上游数据卷成「现在什么情况」一张表：
    项目（宿主）· 会话（宿主，按 cwd 归属）· 任务（TASKS.md，按 proj:<slug> 归属）· 看板（只读计数）

三条硬约束（违反任一即返工）：
  1. **本文件不直接开宿主库** —— 宿主一律经 host_adapter（S10，scripts/check_boundary.py 扫描）
  2. **只读聚合** —— 本模块没有任何写路径；写操作在 B1 及以后，走 fileio + taskfile.write_back
  3. **归属解析失败静默降级** —— cwd 定不了的会话进 unassigned_sessions，不猜、不硬塞

本模块只依赖 Markdown / YAML 与适配层，不引入自有数据库（R-11 数据中立）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 适配层在自己的目录里；core/taskfile.py 在 core/。两者都要 import，
# 而插件加载方式不固定（desktop 插件 / 测试 / CLI），所以显式补 sys.path。
_HERE = Path(__file__).resolve().parent
_PLUGIN_ROOT = _HERE.parent
for _p in (_HERE, _PLUGIN_ROOT / "core"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import host_adapter as ha  # noqa: E402
import meta as meta_mod  # noqa: E402
import taskfile as tf  # noqa: E402

TASKS_FILENAME = "TASKS.md"
META_FILENAME = "META.yaml"



# ── 路径归一与归属 ──────────────────────────────────────────────────────────


def _norm_path(p: str | os.PathLike | None) -> str | None:
    """把路径归一成可比对的形式：绝对化 → 反斜杠统一为正斜杠 → 去尾斜杠 → 小写。

    Windows 上大小写不敏感，所以统一小写比对；返回 None 表示没给路径。
    注意 `os.path.normcase` 在 POSIX 上不变换大小写，这里显式 lower 保证跨平台一致。
    """
    if not p:
        return None
    try:
        s = os.path.abspath(str(p))
    except (OSError, ValueError):
        s = str(p)
    s = s.replace("\\", "/").rstrip("/")
    return s.lower() or None


def _is_under(child: str, parent: str) -> bool:
    """child 是否等于 parent 或落在 parent 目录下（按路径分量比较，避免 abc 命中 abcd）。"""
    return child == parent or child.startswith(parent + "/")


def assign_session(cwd: str | None, projects: list[dict]) -> str | None:
    """会话归属：cwd 落在哪个项目目录下就归哪个项目；定不了返回 None。

    多个项目目录互相嵌套时（实测 pc-dashboard 在 software-pipeline 里面），
    取**最深的那个目录** —— 会话在那个子目录里跑，归它自己的项目更准确。
    META.yaml 的 session_map 人工映射留到 E1 再做，这里只做 cwd 一条规则。
    """
    c = _norm_path(cwd)
    if not c:
        return None
    best_slug, best_len = None, -1
    for proj in projects:
        root = _norm_path(proj.get("path"))
        if not root or not _is_under(c, root):
            continue
        if len(root) > best_len:
            best_slug, best_len = proj.get("slug"), len(root)
    return best_slug


# ── 任务（TASKS.md，人机共写）──────────────────────────────────────────────


def project_root(explicit: str | os.PathLike | None = None) -> Path:
    """TASKS.md 所在的项目根目录。

    优先级：显式参数 > 环境变量 HERMES_COCKPIT_HOME > 仓库根（本文件往上三级）。

    「往上三级」不是随手写的：本文件在 <repo>/plugins/hermes-cockpit/dashboard/store.py，
    而 ARCHITECTURE.md 的数据层把 TASKS.md / META.yaml 放在**仓库根**（与 plugins/ 平级）。
    回退到插件根（往上两级）会找不到 TASKS.md，overview() 就静默返回 tasks=0 ——
    实测如此：真实文件里有 2 个任务，overview() 报 tasks: 0。
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("HERMES_COCKPIT_HOME")
    if env:
        return Path(env)
    return _PLUGIN_ROOT.parent.parent


def read_tasks(root: str | os.PathLike | None = None) -> tuple[str, list[dict]]:
    """读 TASKS.md，返回（原文, 任务列表）。文件不存在时返回 ("", [])，不报错。"""
    p = Path(project_root(root)) / TASKS_FILENAME
    if not p.is_file():
        return "", []
    text = p.read_text(encoding="utf-8")
    return text, tf.parse(text)


def read_sessions_detailed(limit: int | None = None) -> tuple[list[dict], list[str]]:
    """同 read_sessions()，但**不把缺列静默成空**（Q-03）。

    返回 (会话列表, 告警列表)。`sessions` 表「部分必需列缺失」时：
    对应字段填 None，并给出一条人话告警（例：「sessions 表缺 started_at，活动时间不可用」）。
    库/表都不存在 → ([], [])，静默 —— 那是 fresh 首启的正常态。
    """
    sessions = ha.read_sessions(limit=limit)
    warnings = list(ha.last_warnings())
    return sessions, warnings


def _task_counts(tasks: list[dict]) -> dict:
    return {
        "task_count": len(tasks),
        "task_doing": sum(1 for t in tasks if t.get("status") == "doing"),
        "task_blocked": sum(1 for t in tasks if t.get("status") == "blocked"),
    }


# ── 引导状态与作用域（Q-04）────────────────────────────────────────────────


def meta_path(root: str | os.PathLike | None = None) -> Path:
    return Path(project_root(root)) / META_FILENAME


def setup_state(root: str | os.PathLike | None = None) -> dict:
    """读 META.yaml 的 setup 块；文件不存在时返回 meta.DEFAULTS 的默认值。"""
    p = meta_path(root)
    if not p.is_file():
        return dict(meta_mod.DEFAULTS["setup"])
    return meta_mod.load(p).get("setup") or {}


def scope_warnings(setup: dict) -> list[str]:
    """Q-04：`mode=fresh` 但 `at` 为空 → 引导状态异常（人话，界面直接显示）。"""
    if (setup.get("mode") or "").strip().lower() != "fresh":
        return []
    if setup.get("at"):
        return []
    return ["引导状态异常：fresh 模式缺时间锚点（setup.at 为空），请重走引导"]


def scope_at(setup: dict) -> float | None:
    """fresh 作用域的起点（epoch 秒）。None = 不过滤。

    Q-04 裁定：`mode=fresh` 且 `at` 为空时**作用域为空**（返回 inf）——
    宁可一条会话都不显示，也不能退化成「全量可见」的假 fresh。
    """
    if (setup.get("mode") or "").strip().lower() != "fresh":
        return None
    at = setup.get("at")
    if not at:
        return float("inf")             # 空作用域：任何真实时间戳都通不过 `>=`
    return ha.to_epoch(at)


def in_scope(session: dict, scope_start: float | None) -> bool:
    """会话是否落在作用域内。scope_start=None 表示不限制。"""
    if scope_start is None:
        return True
    started = ha.to_epoch(session.get("started_at"))
    if started is None:
        return False                    # 判不了时间就当它不在范围内（宁可少显示）
    return started >= scope_start


# ── 看板（只读计数）───────────────────────────────────────────────────────


def _board_card_count(slug: str, cache: dict[str, int]) -> int:
    if slug not in cache:
        cache[slug] = len(ha.read_board_tasks(slug))
    return cache[slug]


# ── 聚合 ────────────────────────────────────────────────────────────────────


def overview(root: str | os.PathLike | None = None,
             setup: dict | None = None) -> dict:
    """A3 聚合视图 + B1 写入口。签名固定，视图层只认这个 dict。

    {
      "projects": [{slug, name, session_count, task_count, task_doing, task_blocked,
                    board_slug, board_cards, last_activity}],
      "unassigned_sessions": [{id, cwd, started_at, last_activity}],
      "stats": {projects, sessions, tasks, boards},
      "setup": {status, mode, at},          # Q-04：界面要能显示引导状态
      "scope": {mode, since},               # 当前作用域（since=None 表示不限）
      "warnings": [人话]                     # Q-03 降级 + Q-04 引导异常，界面显示但不阻塞
    }

    计数语义（写死，避免视图层各自解释）：
      · session_count —— cwd 归属到本项目的会话数（不含 unassigned）
      · task_count / task_doing / task_blocked —— TASKS.md 里 proj:<slug> 的任务数与状态计数
      · board_slug    —— 宿主项目记录里的 board_slug；为空时取与项目 slug 同名的看板
      · board_cards   —— 该看板的卡片总数；无看板为 0
      · last_activity —— 该项目下最近一次会话活动时间（epoch 秒）；无会话为 None

    counts 一律是 int（无数据为 0），绝不返回 None —— 视图层直接拿来渲染。

    Q-03 / Q-04：warnings[] 收集「表在但部分列缺」的降级告警与「fresh 缺 at」的引导异常；
    fresh 缺锚点时作用域为空（看不到任何会话），而绝不退化成全量可见。
    setup 参数供测试注入，正常调用读 META.yaml。
    """
    if setup is None:
        setup = setup_state(root)
    warnings = scope_warnings(setup)
    scope_start = scope_at(setup)

    try:
        projects = ha.read_projects()
    except FileNotFoundError:
        projects = []                   # 库不存在 = fresh 首启的正常态，静默空
    try:
        sessions_raw = ha.read_sessions()
    except FileNotFoundError:
        sessions_raw = []
    boards = ha.read_boards()
    _, tasks = read_tasks(root)
    all_warnings = list(warnings) + list(ha.last_warnings())

    # 作用域先于归属：越界的会话既不进项目计数，也不进 unassigned
    sessions = [s for s in sessions_raw if in_scope(s, scope_start)]

    board_slugs = {b["slug"] for b in boards}
    card_cache: dict[str, int] = {}

    # 会话 → 项目
    per_project_sessions: dict[str, list[dict]] = {p["slug"]: [] for p in projects}
    unassigned: list[dict] = []
    for s in sessions:
        slug = assign_session(s.get("cwd"), projects)
        if slug is None:
            unassigned.append({
                "id": s.get("id"),
                "cwd": s.get("cwd"),
                "started_at": ha.to_epoch(s.get("started_at")),
                "last_activity": ha.to_epoch(s.get("updated_at")) or ha.to_epoch(s.get("started_at")),
            })
        else:
            per_project_sessions[slug].append(s)

    # 任务 → 项目（按 TASKS.md 的 proj:<slug>）
    per_project_tasks: dict[str, list[dict]] = {}
    for t in tasks:
        proj = t.get("project")
        if proj:
            per_project_tasks.setdefault(proj, []).append(t)

    out_projects = []
    for p in projects:
        slug = p["slug"]
        sess = per_project_sessions.get(slug, [])
        stamps = [x for x in (ha.to_epoch(s.get("updated_at")) or ha.to_epoch(s.get("started_at"))
                              for s in sess) if x is not None]
        board_slug = p.get("board_slug") or (slug if slug in board_slugs else None)
        out_projects.append({
            "slug": slug,
            "name": p.get("name") or slug,
            "session_count": len(sess),
            **_task_counts(per_project_tasks.get(slug, [])),
            "board_slug": board_slug,
            "board_cards": _board_card_count(board_slug, card_cache) if board_slug else 0,
            "last_activity": max(stamps) if stamps else None,
        })

    return {
        "projects": out_projects,
        "unassigned_sessions": unassigned,
        "stats": {
            "projects": len(projects),
            "sessions": len(sessions),
            "tasks": len(tasks),
            "boards": len(boards),
        },
        "setup": {"status": setup.get("status"), "mode": setup.get("mode"),
                  "at": setup.get("at")},
        "scope": {"mode": setup.get("mode"),
                  "since": (scope_start
                            if scope_start is not None and scope_start != float("inf")
                            else None)},
        "warnings": all_warnings,
    }

