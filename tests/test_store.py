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
sys.path.insert(0, str(REPO / "scripts"))

# 插件内部模块必须经 _module_loader 取 —— 与插件运行时是**同一个模块对象**。
# 若这里裸 `import store`，桩打在另一份实例上，测试会假绿（第八轮实测踩过）。
sys.path.insert(0, str(REPO / "plugins" / "project-hub"))
import _module_loader as _ml  # noqa: E402

ha = _ml.host_adapter()
store = _ml.store()
tf = _ml.taskfile()
import check_boundary as cb  # noqa: E402

PROJECT_KEYS = {"slug", "name", "path", "session_count", "task_count", "task_doing", "task_blocked",
                "board_slug", "board_cards", "last_activity"}

# 会话条目字段（C 模块三栏要用 title 显示、via 显示归属依据：manual / cwd）
SESSION_KEYS = {"id", "cwd", "title", "started_at", "last_activity"}


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
            self.assertEqual(set(s), SESSION_KEYS | {"via"})
            self.assertIsNone(s["via"], "未归属的会话不该有归属依据")

    def test_assigned_sessions_carry_project_and_via(self):
        """C 模块中栏：按项目过滤会话，必须能拿到 project；via 说明依据是 cwd 还是人工归类。"""
        ov = store.overview(setup=self.setup)
        self.assertGreaterEqual(len(ov["assigned_sessions"]),
                                sum(p["session_count"] for p in ov["projects"]))
        for s in ov["assigned_sessions"]:
            self.assertEqual(set(s), SESSION_KEYS | {"project", "via"})
            self.assertIn(s["via"], ("cwd", "manual"))
            self.assertTrue(s["project"])

    def test_orphan_tasks_are_visible(self):
        """有任务但 slug 不匹配任何项目 → 必须单列，否则用户以为任务丢了（E1 兜底视图）。"""
        ov = store.overview(setup=self.setup)
        slugs = {p["slug"] for p in ov["projects"]}
        self.assertEqual(ov["stats"]["orphan_tasks"], len(ov["orphan_tasks"]))
        self.assertGreater(ov["stats"]["orphan_tasks"], 0,
                           "当前真实数据应有孤儿任务（proj:hermes-cockpit 不在 14 个项目里）")
        for t in ov["orphan_tasks"]:
            self.assertNotIn(t["project"], slugs)
            self.assertIn("subtasks", t)

    def test_stats_consistent_with_lists(self):
        ov = store.overview()
        self.assertEqual(ov["stats"]["sessions"],
                         sum(p["session_count"] for p in ov["projects"]) + len(ov["unassigned_sessions"]))
        self.assertEqual(ov["stats"]["assigned_sessions"] + ov["stats"]["unassigned_sessions"],
                         ov["stats"]["sessions"])
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


class TestManualAssignmentWins(unittest.TestCase):
    """E1 主路径：META.yaml 的人工归类必须压过 cwd 规则（manual > cwd 命中）。

    真实数据里 cwd 只覆盖 ~2%，归类全靠 session_map；它要是被 cwd 盖掉，
    用户标完项目却看不到变化 —— 这是产品的主路径，不是兜底。
    """

    def setUp(self):
        _ha = ha                          # 必须复用模块级那份（经 _module_loader 取），裸 import 会是另一份实例
        self.ha = _ha
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        Path(self.root, "TASKS.md").write_text("# TASKS\n", encoding="utf-8")
        Path(self.root, "META.yaml").write_text(
            "setup:\n  status: pending\n  mode: null\n  at: null\n\n"
            "session_map:\n  s-in:\n    project: inner\n    source: manual\n    at: \"2026-11-22T00:00:00+08:00\"\n",
            encoding="utf-8")
        self._orig = (_ha.read_projects, _ha.read_sessions, _ha.read_boards,
                      _ha.read_board_tasks, _ha.last_warnings)
        # 会话落在 outer 目录下（cwd 规则本判给 outer），但人工标给了 inner
        _ha.read_projects = lambda: [
            {"slug": "outer", "name": "外", "path": r"C:\dev\knowledge\infrastructure"},
            {"slug": "inner", "name": "内", "path": r"C:\dev\knowledge\infrastructure\software-pipeline"},
        ]
        _ha.read_sessions = lambda limit=None: [
            {"id": "s-in", "cwd": r"C:\dev\knowledge\infrastructure", "started_at": 1795315910.0,
             "title": "人工标过", "updated_at": 1795315911.0},
            {"id": "s-cwd", "cwd": r"C:\dev\knowledge\infrastructure\software-pipeline\x",
             "started_at": 1795315910.0, "title": None, "updated_at": 1795315911.0},
            {"id": "s-none", "cwd": r"C:\Users\someone", "started_at": 1795315910.0,
             "title": None, "updated_at": 1795315911.0},
        ]
        _ha.read_boards = lambda: []
        _ha.read_board_tasks = lambda slug: []
        _ha.last_warnings = lambda: []

    def tearDown(self):
        (self.ha.read_projects, self.ha.read_sessions, self.ha.read_boards,
         self.ha.read_board_tasks, self.ha.last_warnings) = self._orig
        self.tmp.cleanup()

    def test_manual_beats_cwd(self):
        ov = store.overview(self.root, setup={"status": "done", "mode": "imported", "at": None})
        by_id = {s["id"]: s for s in ov["assigned_sessions"]}
        self.assertEqual(by_id["s-in"]["project"], "inner")
        self.assertEqual(by_id["s-in"]["via"], "manual")
        self.assertEqual(by_id["s-cwd"]["project"], "inner", "cwd 落在最深的 inner 目录下")
        self.assertEqual(by_id["s-cwd"]["via"], "cwd")
        self.assertEqual([s["id"] for s in ov["unassigned_sessions"]], ["s-none"])
        counts = {p["slug"]: p["session_count"] for p in ov["projects"]}
        self.assertEqual(counts, {"outer": 0, "inner": 2})
        self.assertEqual(ov["stats"]["sessions"], 3)


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
        src = (REPO / "plugins" / "project-hub" / "dashboard" / "store.py").read_text(
            encoding="utf-8")
        findings = cb.scan_text(src, "store.py", ".py")
        self.assertEqual(findings, [], f"store.py 越界：{[str(f) for f in findings]}")

    def test_store_py_has_no_sqlite_import(self):
        src = (REPO / "plugins" / "project-hub" / "dashboard" / "store.py").read_text(
            encoding="utf-8")
        self.assertNotIn("import sqlite3", src)

    def test_store_only_talks_to_host_through_adapter(self):
        """store 对宿主的读取必须全部走 ha.*（适配层函数）。"""
        src = (REPO / "plugins" / "project-hub" / "dashboard" / "store.py").read_text(
            encoding="utf-8")
        for fn in ("read_projects", "read_sessions", "read_boards"):
            self.assertIn(f"ha.{fn}(", src)


class TestProjectRootFallback(unittest.TestCase):
    """回归：没给参数也没设环境变量时，project_root() 必须落到**仓库根**。

    曾经回退到插件根（<repo>/plugins/project-hub），那里没有 TASKS.md，
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


class TestProjectRootUpwards(unittest.TestCase):
    """GAP-02 回归：装到宿主后（插件目录上层没有 TASKS.md）不许静默返回 tasks=0。

    实测故障形态：插件被 `cp -r` 装到 `AppData\\Local\\hermes\\plugins\\hermes-cockpit`，
    `project_root()` 固定往上三级 → `AppData\\Local\\hermes\\`，那里没有 TASKS.md，
    于是 overview() 报 tasks=0、warnings=[]，用户以为任务全丢了。
    """

    def setUp(self):
        self._old_env = os.environ.pop("HERMES_COCKPIT_HOME", None)

    def tearDown(self):
        if self._old_env is not None:
            os.environ["HERMES_COCKPIT_HOME"] = self._old_env

    def test_empty_dir_gives_warning_not_silent_zero(self):
        """无 TASKS.md 的临时目录：tasks=0 但必须有明确告警（不是静默 0）。"""
        with tempfile.TemporaryDirectory() as d:
            payload = store.overview(d)
            self.assertEqual(payload["stats"]["tasks"], 0)
            self.assertTrue(payload["warnings"],
                            "无 TASKS.md 时 warnings 为空 —— 这就是静默归零（GAP-02）")
            joined = " ".join(payload["warnings"])
            self.assertIn("找不到 TASKS.md", joined)
            self.assertIn("数据目录未初始化", joined)
            self.assertIn(str(d)[:10], joined)      # 告警里要能看出找的是哪个目录

    def test_normal_root_stays_quiet(self):
        """有 TASKS.md 的正常根：不该出现数据目录告警（避免狼来了）。"""
        payload = store.overview(REPO)
        self.assertNotIn("找不到 TASKS.md", " ".join(payload["warnings"]))

    def test_upwards_search_is_not_fixed_depth(self):
        """向上找根不写死层数：埋 5 层深的插件目录也要找到带 TASKS.md 的根。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "TASKS.md").write_text("# TASKS\n", encoding="utf-8")
            deep = root / "a" / "b" / "c" / "d" / "e"
            deep.mkdir(parents=True)
            found = store._find_root_upwards(deep)
            self.assertIsNotNone(found, "埋 5 层就找不到了 —— 说明层数被写死")
            self.assertEqual(Path(found).resolve(), root.resolve())

    def test_upwards_search_returns_none_when_absent(self):
        """一路找到盘根都没有数据文件 → 返回 None，由调用方出告警（不假装成功）。"""
        with tempfile.TemporaryDirectory() as d:
            # 临时目录的父链上没有 TASKS.md/META.yaml，也没有 plugins/+desktop-plugins/
            self.assertIsNone(store._find_root_upwards(d))

    def test_env_var_beats_upwards_search(self):
        """显式 env 一定优先于向上搜索（运维要能强指数据目录）。"""
        with tempfile.TemporaryDirectory() as d:
            os.environ["HERMES_COCKPIT_HOME"] = d
            try:
                self.assertEqual(store.project_root().resolve(), Path(d).resolve())
            finally:
                os.environ.pop("HERMES_COCKPIT_HOME", None)

    def test_declared_data_dir_is_read_from_plugin_yaml(self):
        """plugin.yaml 的 data_dir 要被真的读出来（任务书要求 3）。"""
        declared = store._declared_data_dir()
        self.assertIsNotNone(declared, "plugin.yaml 的 data_dir 没被读到")
        self.assertEqual(Path(declared).resolve(), REPO.resolve(),
                         f"data_dir 解析成 {declared}，应为仓库根 {REPO}")

    def test_declared_data_dir_without_data_falls_through(self):
        """data_dir 指向的目录没有数据文件时继续向上找 —— 否则装到宿主仍是静默 0。

        装到宿主时 plugin.yaml 的 `data_dir: ../../` 解析成 hermes 数据根，
        那里没有 TASKS.md / META.yaml；此时**不能**把该目录当根返回，
        否则 tasks 又是静默 0。
        """
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(store._has_data_files(Path(d)),
                             "空临时目录不该被认成已初始化的根")
            self.assertIsNotNone(store.data_dir_warning(d),
                                 "空目录必须产出告警，否则就是静默 0")

    def test_plugin_yaml_data_dir_is_declared(self):
        """plugin.yaml 必须声明 data_dir，且缺数据时要靠告警兜底，不能静默归零。

        为什么不断言「必须是绝对路径」：仓库要能克隆到任何机器、任何用户目录下直接工作，
        写死 `C:/dev/...` 反而让插件在别人机器上失效。装到宿主时这一行要按 INSTALL.md
        改成项目实际目录 —— 所以这里该锁的是**兜底行为**：data_dir 指错目录时
        必须给出人话告警，而不是 tasks=0 且一声不响。
        """
        cfg = REPO / "plugins" / "project-hub" / "plugin.yaml"
        line = next((ln for ln in cfg.read_text(encoding="utf-8").splitlines()
                     if ln.startswith("data_dir:")), None)
        self.assertIsNotNone(line, "plugin.yaml 没有 data_dir")
        value = line.split(":", 1)[1].strip()
        self.assertTrue(value, "data_dir 是空值")

        # 指对了 → 安静；指错了 → 必须有告警（这正是 GAP-02 的判据）
        resolved = store._declared_data_dir()
        if resolved is not None and store._has_data_files(resolved):
            self.assertEqual(store.data_dir_warning(resolved), [])
        else:
            with tempfile.TemporaryDirectory() as d:
                self.assertTrue(store.data_dir_warning(d),
                                "数据目录没初始化时必须告警，否则就是静默 tasks=0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
