"""E2 判定逻辑单测 —— 第六轮裁定 §四 的 6 条硬要求，逐条锁死。

被测对象：plugins/hermes-cockpit/core/prompt_inject.py（纯函数，不碰宿主）。
宿主侧只有两处需要实测：section id 固定、注册成功 —— 那两处在
tests/test_inject_register.py 用桩 ctx 验，与这里分开跑。

跑法（本机 python 是 3.14 但没装 pytest，用 3.10 解释器）：
    py -3.10 -m pytest tests/test_inject.py -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "hermes-cockpit" / "core"))

import prompt_inject  # noqa: E402

PROJECTS = [
    {"slug": "kids", "name": "儿童项目", "path": "C:/dev/software-projects/kids"},
    {"slug": "kids-sub", "name": "儿童子项目", "path": "C:/dev/software-projects/kids/sub"},
]


def _meta(enabled: bool, session_map: dict | None = None) -> dict:
    return {"reminders": {"enabled": enabled}, "session_map": session_map or {}}


def _render(session_id, cwd, *, enabled=True, session_map=None, projects=None):
    return prompt_inject.build_section(
        session_id,
        session_cwd=lambda sid: cwd,
        read_projects=lambda: PROJECTS if projects is None else projects,
        load_meta=lambda: _meta(enabled, session_map),
    )


class TestRequirement3UnassignedIsEmptyString(unittest.TestCase):
    """要求 3：未归项目 → 空串（不是「当前项目：无」）。"""

    def test_no_cwd_match_returns_empty(self):
        self.assertEqual(_render("s1", "C:/Windows"), "")

    def test_no_cwd_at_all_returns_empty(self):
        self.assertEqual(_render("s1", None), "")

    def test_unknown_session_id_returns_empty(self):
        self.assertEqual(_render("", "C:/dev/software-projects/kids"), "")

    def test_manual_map_pointing_at_unknown_slug_returns_empty(self):
        """session_map 里的项目在宿主里不存在 ⇒ 视为未归属，不硬塞。"""
        out = _render("s1", "C:/Windows", session_map={"s1": {"project": "ghost"}})
        self.assertEqual(out, "")

    def test_empty_string_is_not_placeholder_text(self):
        out = _render("s1", "C:/Windows")
        self.assertNotIn("无", out)
        self.assertNotIn("None", out)


class TestRequirement4SwitchOff(unittest.TestCase):
    """要求 4：reminders.enabled=false → 空串（即使会话明确归属）。"""

    def test_disabled_returns_empty_even_when_assigned(self):
        out = _render("s1", "C:/dev/software-projects/kids", enabled=False)
        self.assertEqual(out, "")

    def test_disabled_beats_manual_map(self):
        out = _render("s1", "C:/Windows", enabled=False,
                      session_map={"s1": {"project": "kids"}})
        self.assertEqual(out, "")

    def test_enabled_with_assignment_renders(self):
        out = _render("s1", "C:/dev/software-projects/kids", enabled=True)
        self.assertIn("kids", out)
        self.assertIn("儿童项目", out)


class TestAssignmentPriority(unittest.TestCase):
    """归属优先级与 E1 同口径：manual > cwd，cwd 取最深目录。"""

    def test_manual_beats_cwd(self):
        out = _render("s1", "C:/dev/software-projects/kids",
                      session_map={"s1": {"project": "kids-sub"}})
        self.assertIn("kids-sub", out)

    def test_cwd_deepest_project_wins(self):
        out = _render("s1", "C:/dev/software-projects/kids/sub/file.txt")
        self.assertIn("kids-sub", out)

    def test_manual_of_other_session_does_not_leak(self):
        out = _render("s2", "C:/Windows", session_map={"s1": {"project": "kids"}})
        self.assertEqual(out, "")


class TestRequirement5FailOpen(unittest.TestCase):
    """要求 5：任何异常 → 空串，绝不让会话起不来。"""

    def test_meta_loader_raises(self):
        out = prompt_inject.build_section(
            "s1", session_cwd=lambda s: "C:/dev/software-projects/kids",
            read_projects=lambda: PROJECTS,
            load_meta=lambda: (_ for _ in ()).throw(RuntimeError("META.yaml 坏了")))
        self.assertEqual(out, "")

    def test_project_reader_raises(self):
        out = prompt_inject.build_section(
            "s1", session_cwd=lambda s: "C:/dev/software-projects/kids",
            read_projects=lambda: (_ for _ in ()).throw(OSError("projects.db 被占用")),
            load_meta=lambda: _meta(True))
        self.assertEqual(out, "")

    def test_cwd_reader_raises(self):
        out = prompt_inject.build_section(
            "s1", session_cwd=lambda s: (_ for _ in ()).throw(RuntimeError("库锁了")),
            read_projects=lambda: PROJECTS, load_meta=lambda: _meta(True))
        self.assertEqual(out, "")

    def test_none_session_info_is_safe(self):
        out = prompt_inject.render_for_session(
            None, session_cwd=lambda s: "x", read_projects=lambda: [],
            load_meta=lambda: _meta(True))
        self.assertEqual(out, "")


class TestSectionLength(unittest.TestCase):
    """80 字硬上限：超限会被宿主**整段丢弃**，所以必须自己截断。"""

    def test_rendered_section_never_exceeds_limit(self):
        long_projects = [{"slug": "x" * 60, "name": "很长的项目名" * 5,
                          "path": "C:/dev/software-projects/kids"}]
        out = _render("s1", "C:/dev/software-projects/kids", projects=long_projects)
        self.assertLessEqual(len(out), prompt_inject.SECTION_MAX_CHARS)
        self.assertTrue(out)

    def test_limit_is_eighty(self):
        self.assertEqual(prompt_inject.SECTION_MAX_CHARS, 80)

    def test_whitespace_is_collapsed(self):
        out = prompt_inject.format_for_prompt("  当前   项目：x \n ")
        self.assertEqual(out, "当前 项目：x")


class TestRenderForSession(unittest.TestCase):
    """宿主 callable 的入口：只读 session-info 取 session_id，其余不采信。"""

    def test_uses_session_id_from_info(self):
        out = prompt_inject.render_for_session(
            {"session_id": "s1", "cwd": "C:/evi"},   # 故意给错 cwd：归属只认库里的
            session_cwd=lambda sid: "C:/dev/software-projects/kids",
            read_projects=lambda: PROJECTS,
            load_meta=lambda: _meta(True))
        self.assertIn("kids", out)

    def test_missing_key_returns_empty(self):
        out = prompt_inject.render_for_session(
            {}, session_cwd=lambda s: "C:/dev/software-projects/kids",
            read_projects=lambda: PROJECTS, load_meta=lambda: _meta(True))
        self.assertEqual(out, "")


class TestAssignByCwd(unittest.TestCase):
    """路径比对的三个易错点：大小写、分隔符、前缀陷阱（abc vs abcd）。"""

    def test_case_and_separators_insensitive(self):
        self.assertEqual(
            prompt_inject.assign_by_cwd("c:\\dev\\Software-Projects\\KIDS\\x", PROJECTS), "kids")

    def test_sibling_prefix_is_not_a_match(self):
        self.assertIsNone(prompt_inject.assign_by_cwd("C:/dev/software-projects/kids-old", PROJECTS))

    def test_project_itself_matches(self):
        self.assertEqual(prompt_inject.assign_by_cwd("C:/dev/software-projects/kids", PROJECTS), "kids")


if __name__ == "__main__":
    unittest.main()
