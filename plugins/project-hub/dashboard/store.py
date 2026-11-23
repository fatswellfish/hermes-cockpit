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

import importlib.util
import os
import sys
from pathlib import Path

# 插件内部模块一律经 _module_loader 按**文件路径**加载，**不许补 sys.path 再裸 import**
# （第八轮修复）：宿主里 sys.modules 可能已被同名模块占住（实测宿主 venv 有
# pet/store.py、pm/progress.py、boto3/s3/inject.py 等），而 sys.path 的优先级
# 压不过已缓存的模块 —— 裸 import 会静默拿到别人的模块。
_HERE = Path(__file__).resolve().parent
_PLUGIN_ROOT = _HERE.parent
_LOADER_KEY = "hermes_cockpit_module_loader"
_ml = sys.modules.get(_LOADER_KEY)
if _ml is None:
    _loader_spec = importlib.util.spec_from_file_location(
        _LOADER_KEY, _PLUGIN_ROOT / "_module_loader.py")
    if _loader_spec is None or _loader_spec.loader is None:      # pragma: no cover
        raise ImportError(
            f"hermes-cockpit: 找不到模块加载器 {_PLUGIN_ROOT / '_module_loader.py'}")
    _ml = importlib.util.module_from_spec(_loader_spec)
    sys.modules[_LOADER_KEY] = _ml
    try:
        _loader_spec.loader.exec_module(_ml)
    except BaseException:
        sys.modules.pop(_LOADER_KEY, None)      # 不留半初始化空壳
        raise

ha = _ml.host_adapter()
meta_mod = _ml.meta()
tf = _ml.taskfile()

TASKS_FILENAME = "TASKS.md"         # 旧的单文件布局，仅作兼容回落
META_FILENAME = "META.yaml"

# 任务按来源分三份文件（用户 2026-11-23 定案）：
#   agent —— agent 自己写（做完一步勾一笔），人只看
#   work  —— 办公辅助 / 日常工作，人写
#   home  —— 家里要办的事，人写
# 顺序即界面上的展示顺序。
TASK_SOURCES = (
    ("TASKS.agent.md", "agent"),
    ("TASKS.work.md", "work"),
    ("TASKS.home.md", "home"),
)



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


def _has_data_files(d: Path) -> bool:
    """目录是否像一个已初始化的项目根（有 TASKS.md 或 META.yaml）。"""
    return (d / TASKS_FILENAME).is_file() or (d / META_FILENAME).is_file()


def _looks_like_root(d: Path) -> bool:
    """逐级找根时的候选判据：有数据文件，**或**同时有 plugins/ 与 desktop-plugins/。

    后一条是给插件源码仓用的：仓库刚 clone 下来、还没初始化 TASKS.md 时，
    它仍然是一个「根」，不该被继续往上找。装到宿主时两条都不成立，
    所以一直找到盘根，由调用方给出告警（见 data_dir_warning）。
    """
    if _has_data_files(d):
        return True
    return (d / "plugins").is_dir() and (d / "desktop-plugins").is_dir()


def _find_root_upwards(start: Path) -> Path | None:
    """从 start 向上逐级找项目根，**不限层数**，找到盘根都没有则返回 None。"""
    cur = Path(start).resolve()
    while True:
        if _looks_like_root(cur):
            return cur
        if cur.parent == cur:               # 到盘根了
            return None
        cur = cur.parent


def _declared_data_dir() -> Path | None:
    """读插件自己 plugin.yaml 的 data_dir（相对插件目录），存在则用它。

    用极简 YAML 行解析，不引 PyYAML（与 host_adapter.load_compat 同一策略）——
    这里只取 data_dir 一个标量，不需要通用解析器。
    """
    cfg = _PLUGIN_ROOT / "plugin.yaml"
    try:
        if not cfg.is_file():
            return None
        for line in cfg.read_text(encoding="utf-8").splitlines():
            s = line.split("#", 1)[0].rstrip()
            if not s.startswith("data_dir:"):
                continue
            val = s.split(":", 1)[1].strip().strip("'\"")
            if not val:
                return None
            p = Path(val)
            cand = (p if p.is_absolute() else (_PLUGIN_ROOT / p)).resolve()
            return cand if cand.is_dir() else None
    except OSError:
        return None
    return None


def project_root(explicit: str | os.PathLike | None = None) -> Path:
    """TASKS.md 所在的项目根目录。

    优先级：显式参数 > 环境变量 HERMES_COCKPIT_HOME > plugin.yaml 的 data_dir
            > 从插件目录向上逐级找（TASKS.md / META.yaml，**不写死层数**）> 插件目录

    「往上三级」是旧写法，只对仓库内布局有效：本文件在
    <repo>/plugins/hermes-cockpit/dashboard/store.py，而数据层把 TASKS.md / META.yaml
    放在**仓库根**（与 plugins/ 平级）。**装到宿主后**（AppData\\Local\\hermes\\plugins\\hermes-cockpit）
    往上三级是 AppData\\Local\\hermes\\，那里没有 TASKS.md → 静默返回 tasks=0（GAP-02 实测）。

    ⇒ 现在改成向上**逐级**找，找到即用；找不到也不假装成功：
    调用方用 `data_dir_warning()` 把「数据目录未初始化」写进 overview()["warnings"]。
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("HERMES_COCKPIT_HOME")
    if env:
        return Path(env)
    declared = _declared_data_dir()
    if declared is not None and _has_data_files(declared):
        return declared
    found = _find_root_upwards(_PLUGIN_ROOT)
    return found if found is not None else (declared or _PLUGIN_ROOT)


def data_dir_warning(root: str | os.PathLike | None = None) -> list[str]:
    """数据目录没初始化时给出人话告警（不许静默返回 0）。

    判据是**TASKS.md 不存在**：META.yaml 可以是空态，但一个「有看板有任务」的界面
    不能凭空把任务数显示成 0。告警写清目录，用户能据此去设 HERMES_COCKPIT_HOME。
    """
    try:
        r = Path(project_root(root))
    except (OSError, ValueError):
        return []
    if (r / TASKS_FILENAME).is_file():
        return []
    return [f"找不到 TASKS.md，数据目录未初始化：{r}"
            f"（请在 plugin.yaml 里指定 data_dir，或设置环境变量 HERMES_COCKPIT_HOME）"]


def read_tasks(root: str | os.PathLike | None = None) -> tuple[str, list[dict]]:
    """读任务文件，返回（原文, 任务列表）。文件不存在时不报错。

    任务按来源分成三份文件（用户 2026-11-23 定案），全部读入后合并：

        TASKS.agent.md   agent 自动化 —— agent 自己写，人只看
        TASKS.work.md    办公辅助 / 日常工作 —— 人写，agent 可读
        TASKS.home.md    家里要办的事 —— 人写，agent 可读

    每条的 `source` 标明来自哪类（文件内写了 `> source:` 就用写的，
    否则按文件名推），`source_file` 记具体文件名，便于写回原文件。

    兼容：三份都不存在时才回落到旧的单文件 `TASKS.md`，
    这样已有部署升级后不会突然「任务归零」。
    """
    base = Path(project_root(root))
    tasks: list[dict] = []
    texts: list[str] = []
    found = False

    for filename, default_source in TASK_SOURCES:
        p = base / filename
        if not p.is_file():
            continue
        found = True
        text = p.read_text(encoding="utf-8")
        texts.append(text)
        for task in tf.parse(text):
            if not task.get("source"):
                task["source"] = default_source
            task["source_file"] = filename
            tasks.append(task)

    if found:
        return "\n".join(texts), tasks

    legacy = base / TASKS_FILENAME
    if legacy.is_file():
        text = legacy.read_text(encoding="utf-8")
        tasks = tf.parse(text)
        for task in tasks:
            if not task.get("source"):
                task["source"] = "agent"
            task["source_file"] = TASKS_FILENAME
        return text, tasks

    return "", []


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

    # Q-03 同类兜底：数据目录没初始化时**不许静默返回 tasks=0**（GAP-02 实测：
    # 装到宿主后 root 指到 AppData\Local\hermes\，没有 TASKS.md，界面显示 0 个任务、
    # 0 条告警，用户以为任务丢了）。这里补一条人话告警，界面直接显示。
    all_warnings.extend(data_dir_warning(root))

    # 作用域先于归属：越界的会话既不进项目计数，也不进 unassigned
    sessions = [s for s in sessions_raw if in_scope(s, scope_start)]

    board_slugs = {b["slug"] for b in boards}
    card_cache: dict[str, int] = {}

    # 人工归类（META.yaml 的 session_map）优先于 cwd 规则：manual > cwd 命中。
    # 读不到就当没有 —— 归类是增强不是前置条件，不能因为它让 overview 整体失败。
    try:
        manual_map = meta_mod.session_map(meta_path(root)) or {}
    except Exception:                   # noqa: BLE001 - 配置坏了也要能看数据
        manual_map = {}

    # 会话 → 项目
    per_project_sessions: dict[str, list[dict]] = {p["slug"]: [] for p in projects}
    unassigned: list[dict] = []
    assigned_sessions: list[dict] = []
    for s in sessions:
        sid = s.get("id")
        manual = (manual_map.get(sid) or {}).get("project")
        slug = manual if manual in per_project_sessions else assign_session(s.get("cwd"), projects)
        if slug is None:
            unassigned.append({
                "id": sid,
                "cwd": s.get("cwd"),
                "title": s.get("title"),
                "started_at": ha.to_epoch(s.get("started_at")),
                "last_activity": ha.to_epoch(s.get("updated_at")) or ha.to_epoch(s.get("started_at")),
                "via": None,
            })
        else:
            per_project_sessions[slug].append(s)
            assigned_sessions.append({
                "id": sid,
                "cwd": s.get("cwd"),
                "title": s.get("title"),
                "project": slug,
                "via": "manual" if (manual == slug) else "cwd",
                "started_at": ha.to_epoch(s.get("started_at")),
                "last_activity": ha.to_epoch(s.get("updated_at")) or ha.to_epoch(s.get("started_at")),
            })

    # 任务 → 项目（按 TASKS.md 的 proj:<slug>）
    per_project_tasks: dict[str, list[dict]] = {}
    for t in tasks:
        proj = t.get("project")
        if proj:
            per_project_tasks.setdefault(proj, []).append(t)

    out_projects = []
    known_slugs = {p["slug"] for p in projects}
    for p in projects:
        slug = p["slug"]
        sess = per_project_sessions.get(slug, [])
        stamps = [x for x in (ha.to_epoch(s.get("updated_at")) or ha.to_epoch(s.get("started_at"))
                              for s in sess) if x is not None]
        board_slug = p.get("board_slug") or (slug if slug in board_slugs else None)
        out_projects.append({
            "slug": slug,
            "name": p.get("name") or slug,
            "path": p.get("path"),
            "session_count": len(sess),
            **_task_counts(per_project_tasks.get(slug, [])),
            "board_slug": board_slug,
            "board_cards": _board_card_count(board_slug, card_cache) if board_slug else 0,
            "last_activity": max(stamps) if stamps else None,
        })

    # 未归属任务：slug 不匹配任何项目 → 它在 task_count 里，却不显示在任何项目下，
    # 界面必须单列，否则用户以为任务丢了。project 为空的同样算未归属。
    orphan_tasks = [t for t in tasks if (t.get("project") or "") not in known_slugs]

    return {
        "projects": out_projects,
        "unassigned_sessions": unassigned,
        "assigned_sessions": assigned_sessions,
        "orphan_tasks": [{"id": t["id"], "title": t.get("title"),
                          "status": t.get("status"), "progress": t.get("progress"),
                          "project": t.get("project"), "blocker": t.get("blocker"),
                          "subtasks": t.get("subtasks") or []}
                         for t in orphan_tasks],
        "stats": {
            "projects": len(projects),
            "sessions": len(sessions),
            "tasks": len(tasks),
            "boards": len(boards),
            "unassigned_sessions": len(unassigned),
            "assigned_sessions": len(assigned_sessions),
            "orphan_tasks": len(orphan_tasks),
        },
        "setup": {"status": setup.get("status"), "mode": setup.get("mode"),
                  "at": setup.get("at")},
        "scope": {"mode": setup.get("mode"),
                  "since": (scope_start
                            if scope_start is not None and scope_start != float("inf")
                            else None)},
        "warnings": all_warnings,
    }

