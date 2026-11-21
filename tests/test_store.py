"""A3 聚合视图验收测试 —— dashboard/store.py。

覆盖（WBS A3）：
  · overview() 返回 14 个项目，每个项目三个计数非 null/非负、字段齐全
  · unassigned_sessions 非空（真实宿主里大量会话 cwd 为空或落在非项目目录）
  · 归属规则：cwd 落在项目目录下即归该项目；嵌套目录取最深；定不了进 unassigned
  · 任务按 TASKS.md 的 proj:<slug> 归到项目
  · 边界：store.py 不得直接开宿主库（改由 scripts/check_boundary.py 判，这里复用它的扫描器）

跑法：python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "hermes-cockpit" / "dashboard"))
sys.path.insert(0, str(REPO / "scripts"))

import host_adapter as ha  # noqa: E402
import store  # noqa: E402
import taskfile as tf  # noqa: E402
import check_boundary as cb  # noqa: E402

PROJECT_KEYS = {"slug", "name", "session_count", "task_count", "task_doing", "task_blocked",
                "board_slug", "board_cards", "last_activity"}


class TestOverviewRealHost(unittest.TestCase):
    """在真实宿主上跑 overview()（只读，不改任何东西）。"""

    def setUp(self):
        if not (ha.projects_db().exists() and ha.state_db().exists()):
            self.skipTest("宿主 projects.db / state.db 不存在")
        # 真实 setup 里的 mode 决定作用域：fresh 时老会话本来就看不见，
        # 用「全量视野」跑这组断言，否则测的是作用域而不是聚合。
        self.setup = {"status": "done", "mode": "imported", "at": None}

    def test_fourteen_projects(self):
        ov = store.overview(setup=self.setup)
        self.assertEqual(len(ov["projects"]), 14)
        self.assertEqual(ov["stats"]["projects"], 14)

    def test_every_project_has_three_counts_not_null(self):
        for p in store.overview(setup=self.setup)["projects"]:
            for key in ("task_count", "task_doing", "task_blocked"):
                self.assertIn(key, p, f"{p['slug']} 缺 {key}")
                self.assertIsNotNone(p[key], f"{p['slug']}.{key} 为 None")
                self.assertIsInstance(p[key], int)
                self.assertGreaterEqual(p[key], 0)
            self.assertIsInstance(p["session_count"], int)
            self.assertGreaterEqual(p["session_count"], 0)
            self.assertEqual(set(p), PROJECT_KEYS)

    def test_unassigned_sessions_non_empty(self):
        ov = store.overview(setup=self.setup)
        self.assertTrue(ov["unassigned_sessions"], "unassigned_sessions 为空 —— 归属规则可能把一切都硬塞进项目")
        for s in ov["unassigned_sessions"]:
            self.assertEqual(set(s), {"id", "cwd", "started_at", "last_activity"})

    def test_stats_consistent_with_lists(self):
        ov = store.overview()
        self.assertEqual(ov["stats"]["sessions"],
                         sum(p["session_count"] for p in ov["projects"]) + len(ov["unassigned_sessions"]))
        self.assertGreaterEqual(ov["stats"]["boards"], 0)
        self.assertGreaterEqual(ov["stats"]["tasks"], 0)

    def test_projects_path_is_probed_not_hardcoded(self):
        """回归：项目主目录列实测叫 primary_path，硬编码 path 会让归属规则全废。"""
        rows = ha.read_projects()
        self.assertTrue(all(r.get("path") for r in rows),
                        "read_projects 返回的主目录全为 None —— 列名探测退化了")


class TestAssignmentRule(unittest.TestCase):
    """归属规则本身（纯函数，不依赖宿主）。"""

    PROJECTS = [
        {"slug": "outer", "path": r"C:\dev\knowledge\infrastructure"},
        {"slug": "inner", "path": r"C:\dev\knowledge\infrastructure\software-pipeline"},
    ]

    def test_exact_path(self):
        self.assertEqual(store.assign_session(r"C:\dev\knowledge\infrastructure", self.PROJECTS), "outer")

    def test_nested_takes_deepest(self):
        self.assertEqual(
            store.assign_session(r"C:\dev\knowledge\infrastructure\software-pipeline\dashboard",
                                 self.PROJECTS), "inner")

    def test_case_and_slash_insensitive(self):
        self.assertEqual(
            store.assign_session("c:/dev/knowledge/infrastructure/SOFTWARE-PIPELINE/x", self.PROJECTS),
            "inner")

    def test_sibling_prefix_is_not_a_match(self):
        """`infrastructure2` 不能被 `infrastructure` 命中（按路径分量比较）。"""
        self.assertIsNone(store.assign_session(r"C:\dev\knowledge\infrastructure2", self.PROJECTS))

    def test_empty_cwd_unassigned(self):
        self.assertIsNone(store.assign_session(None, self.PROJECTS))
        self.assertIsNone(store.assign_session("", self.PROJECTS))

    def test_unrelated_cwd_unassigned(self):
        self.assertIsNone(store.assign_session(r"C:\Users\someone", self.PROJECTS))


class TestTasksAttribution(unittest.TestCase):
    """任务按 TASKS.md 的 proj:<slug> 归到项目（用临时目录造的项目文件）。"""

    TASKS = """# TASKS

> 说明

## T-001 甲 · doing · 10% · proj:alpha
- [ ] T-001.1 a

## T-002 乙 · blocked · 0% · proj:beta
- [ ] T-002.1 b

## T-003 丙 · blocked · 0% · proj:alpha
- [ ] T-003.1 c

## T-004 无项目 · doing · 0%
- [ ] T-004.1 d
"""

    def test_counts_by_proj_slug(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "TASKS.md").write_text(self.TASKS, encoding="utf-8")
            text, tasks = store.read_tasks(d)
        self.assertTrue(text.startswith("# TASKS"))
        self.assertEqual(len(tasks), 4)
        by_proj = {}
        for t in tasks:
            by_proj.setdefault(t["project"], []).append(t)
        self.assertEqual(len(by_proj["alpha"]), 2)
        self.assertEqual(len(by_proj["beta"]), 1)
        self.assertEqual(sum(1 for t in by_proj["alpha"] if t["status"] == "blocked"), 1)
        self.assertIn(None, by_proj)          # 没有 proj 段的任务不归属任何项目

    def test_missing_tasks_file_is_empty_not_error(self):
        with tempfile.TemporaryDirectory() as d:
            text, tasks = store.read_tasks(d)
        self.assertEqual(text, "")
        self.assertEqual(tasks, [])

    def test_env_var_selects_root(self):
        old = os.environ.get("HERMES_COCKPIT_HOME")
        with tempfile.TemporaryDirectory() as d:
            os.environ["HERMES_COCKPIT_HOME"] = d
            try:
                self.assertEqual(store.project_root(), Path(d))
            finally:
                if old is None:
                    os.environ.pop("HERMES_COCKPIT_HOME", None)
                else:
                    os.environ["HERMES_COCKPIT_HOME"] = old


class TestStoreBoundary(unittest.TestCase):
    """硬约束：store.py 不得直接开宿主库（复用 S10 扫描器，不另写一套判据）。"""

    def test_store_py_is_clean(self):
        src = (REPO / "plugins" / "hermes-cockpit" / "dashboard" / "store.py").read_text(
            encoding="utf-8")
        findings = cb.scan_text(src, "store.py", ".py")
        self.assertEqual(findings, [], f"store.py 越界：{[str(f) for f in findings]}")

    def test_store_py_has_no_sqlite_import(self):
        src = (REPO / "plugins" / "hermes-cockpit" / "dashboard" / "store.py").read_text(
            encoding="utf-8")
        self.assertNotIn("import sqlite3", src)

    def test_store_only_talks_to_host_through_adapter(self):
        """store 对宿主的读取必须全部走 ha.*（适配层函数）。"""
        src = (REPO / "plugins" / "hermes-cockpit" / "dashboard" / "store.py").read_text(
            encoding="utf-8")
        for fn in ("read_projects", "read_sessions", "read_boards"):
            self.assertIn(f"ha.{fn}(", src)


class TestProjectRootFallback(unittest.TestCase):
    """回归：没给参数也没设环境变量时，project_root() 必须落到**仓库根**。

    曾经回退到插件根（<repo>/plugins/hermes-cockpit），那里没有 TASKS.md，
    于是 overview() 静默返回 tasks=0 —— 不报错，只是所有任务计数全空。
    这种「假 0」比抛异常难查得多，所以用真实仓库结构锁死它。
    """

    def test_fallback_is_repo_root_with_tasks_md(self):
        old = os.environ.pop("HERMES_COCKPIT_HOME", None)
        try:
            root = store.project_root()
        finally:
            if old is not None:
                os.environ["HERMES_COCKPIT_HOME"] = old
        self.assertEqual(root.resolve(), REPO.resolve(),
                         f"project_root() 回退到 {root}，不是仓库根 {REPO}")
        self.assertTrue((root / "TASKS.md").is_file(), "回退目录里必须真的有 TASKS.md")

    def test_overview_sees_real_tasks(self):
        """用真实仓库根的 TASKS.md 数任务：不能是 0（假空比报错更难发现）。"""
        old = os.environ.pop("HERMES_COCKPIT_HOME", None)
        try:
            _, tasks = store.read_tasks()
        finally:
            if old is not None:
                os.environ["HERMES_COCKPIT_HOME"] = old
        real = tf.parse((REPO / "TASKS.md").read_text(encoding="utf-8"))
        self.assertEqual(len(tasks), len(real))
        self.assertGreater(len(tasks), 0, "真实 TASKS.md 里没有解析出任务 —— 根目录找错了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
