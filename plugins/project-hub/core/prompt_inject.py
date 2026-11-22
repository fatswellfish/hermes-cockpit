"""inject — E2 系统提示注入的判定逻辑（纯函数，与宿主无关）。

宿主侧唯一写入口：dashboard/host_adapter.py::register_system_prompt_section。
本模块只决定「注入什么文本」，不碰任何宿主对象 —— 数据读取全部经回调注入
（session_cwd / read_projects / load_meta），便于测试，也让本模块零依赖。

硬要求（第六轮裁定 §四）：
  1. section id 固定 `hermes-cockpit.project` —— 在适配层，本模块不参与
  2. register() 不实现业务逻辑 —— 在 __init__.py，本模块是被转调的对象
  3. 未归项目 → 返回**空串**（不是「当前项目：无」）—— 空串不污染 prompt
  4. reminders.enabled=false → 同样返回空串
  5. fail-open：任何异常返回空串，不让会话起不来
  6. enabled 状态变更需重启 Hermes —— 见 __init__.py / STATE.md

宿主行为（实测 hermes_cli/plugins_dispatch.py::_render_prompt_section_text）：
返回空串的 section 被**丢弃**；返回非空但 > max_chars 的 section 也被丢弃（不是截断），
所以字数必须自己卡住 —— 超了就截断，绝不把整段塞到超限。

归属规则（E1 的同一套优先级）：manual（META.yaml 的 session_map）> cwd 命中。
cwd 命中取**最深的那个项目目录**（与 store.assign_session 同一口径）。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Mapping

# 80 字硬上限（裁定 §三 Q1 第 3 条引用的约束）。
SECTION_MAX_CHARS = 80


# ── 路径归一与归属（与 store.assign_session 同口径，但不依赖 store 的模块级导入）─────


def _norm_path(p: str | os.PathLike | None) -> str | None:
    if not p:
        return None
    try:
        s = os.path.abspath(str(p))
    except (OSError, ValueError):
        s = str(p)
    s = s.replace("\\", "/").rstrip("/")
    return s.lower() or None


def _is_under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + "/")


def assign_by_cwd(cwd: str | os.PathLike | None, projects: list[dict]) -> str | None:
    """cwd 落在哪个项目目录下 ⇒ 该项目 slug；定不了返回 None。多项目嵌套取最深。"""
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


# ── 渲染 ────────────────────────────────────────────────────────────────────


def _clip(text: str, limit: int = SECTION_MAX_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_section(
    session_id: str | None,
    *,
    session_cwd: Callable[[str], str | None],
    read_projects: Callable[[], list[dict]],
    load_meta: Callable[[], dict],
) -> str:
    """核心判定。任何异常都收敛成空串（要求 5）。"""
    try:
        meta = load_meta() or {}
        if not (meta.get("reminders") or {}).get("enabled"):
            return ""                                   # 要求 4：开关关闭
        if not session_id:
            return ""

        projects = read_projects() or []
        slugs = {p.get("slug") for p in projects if p.get("slug")}

        manual = (meta.get("session_map") or {}).get(session_id) or {}
        slug = manual.get("project") if manual.get("project") in slugs else None
        if slug is None:
            slug = assign_by_cwd(session_cwd(session_id), projects)
        if slug is None:
            return ""                                   # 要求 3：未归项目 = 空串

        proj = next((p for p in projects if p.get("slug") == slug), {})
        name = (proj.get("name") or slug).strip()
        body = f"当前项目：{name}（{slug}）。工作请围绕该项目；任务清单见项目 TASKS.md。"
        return _clip(body)
    except Exception:                                   # noqa: BLE001 - 要求 5：fail-open
        return ""


def format_for_prompt(text: str) -> str:
    """宿主渲染前的最后一道保险：空串原样返回（宿主会丢弃该 section）。"""
    if not text:
        return ""
    return _clip(text.strip())


def render_for_session(
    session_info: Mapping[str, Any] | None,
    *,
    session_cwd: Callable[[str], str | None],
    read_projects: Callable[[], list[dict]],
    load_meta: Callable[[], dict],
) -> str:
    """宿主 callable 的落点：从只读 session-info 里取 session_id，交给 build_section。"""
    try:
        sid = (session_info or {}).get("session_id") or None
        return format_for_prompt(build_section(
            sid, session_cwd=session_cwd, read_projects=read_projects, load_meta=load_meta))
    except Exception:                                   # noqa: BLE001 - 要求 5
        return ""
