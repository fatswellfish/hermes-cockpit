"""Q-03 / Q-04 验收 —— store.overview() 的降级告警与 fresh 作用域。

全部在**临时宿主目录**里跑（`HERMES_HOME` 指向 tempfile），不碰真库：
  · Q-03：`sessions` 表存在但部分必需列缺 → 字段 None + warnings[] 人话；
          库/表都不存在 → 空且静默（fresh 首启的正常态）；
          主键列缺失 → HostSchemaError（不能降级成「没有会话」）
  · Q-04：`mode=fresh` 且 `at` 空 → 作用域为空 + 告警（绝不退化成全量可见）；
          `set_setup` 先写 at 再写 mode（磁盘上不出现「mode 已 fresh、at 还空」）

跑法：python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "project-hub" / "dashboard"))
sys.path.insert(0, str(REPO / "plugins" / "project-hub" / "core"))

import host_adapter as ha  # noqa: E402
import meta as meta_mod  # noqa: E402
import store  # noqa: E402


class _TempHost:
    """一个临时 hermes home：projects.db + state.db 由本类按需造。"""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cockpit-q03-"))
        self.home = self.dir / "hermes"
        self.home.mkdir()
        self.cockpit = self.dir / "cockpit"
        self.cockpit.mkdir()
        (self.cockpit / "TASKS.md").write_text(
            "## T-001 示例 · doing · 10% · proj:demo\n- [x] T-001.1 a\n- [ ] T-001.2 b\n",
            encoding="utf-8")
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)

    def make_projects(self, rows):
        con = sqlite3.connect(str(self.home / "projects.db"))
        try:
            con.execute("CREATE TABLE projects (slug TEXT, name TEXT, primary_path TEXT)")
            con.executemany("INSERT INTO projects VALUES (?,?,?)", rows)
            con.commit()
        finally:
            con.close()

    def make_sessions(self, columns_sql: str, rows):
        con = sqlite3.connect(str(self.home / "state.db"))
        try:
            con.execute(f"CREATE TABLE sessions ({columns_sql})")
            if rows:
                marks = ",".join("?" * len(rows[0]))
                con.executemany(f"INSERT INTO sessions VALUES ({marks})", rows)
            con.commit()
        finally:
            con.close()

    def make_meta(self, text: str) -> Path:
        p = self.cockpit / "META.yaml"
        p.write_text(text, encoding="utf-8")
        return p

    def overview(self, **kw):
        return store.overview(self.cockpit, **kw)

    def cleanup(self):
        ha.close_all()
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


PROJ = ("demo", "演示项目", str(Path(tempfile.gettempdir()) / "demo-proj"))


class TestQ03PartialColumns(unittest.TestCase):
    """Q-03：表在、部分列缺 → 字段 None + 告警（既不崩也不撒谎）。"""

    def setUp(self):
        self.h = _TempHost()
        self.h.make_projects([PROJ])
        self.proj_root = Path(PROJ[2])
        self.proj_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.h.cleanup()
        try:
            self.proj_root.rmdir()
        except OSError:
            pass

    def test_missing_started_at_degrades_with_warning(self):
        # 只有 id 与 cwd：started_at / updated_at 都没有
        self.h.make_sessions("id TEXT, cwd TEXT", [("s1", str(self.proj_root))])
        ov = self.h.overview(setup={"status": "done", "mode": None, "at": None})
        sessions = ha.read_sessions()
        self.assertEqual(sessions[0]["started_at"], None)
        self.assertEqual(sessions[0]["updated_at"], None)
        self.assertTrue(any("started_at" in w for w in ov["warnings"]),
                        ov["warnings"])
        self.assertTrue(any("updated_at" in w for w in ov["warnings"]),
                        ov["warnings"])
        # 不阻塞：项目照常出现，会话照常归到项目
        self.assertEqual([p["slug"] for p in ov["projects"]], ["demo"])
        self.assertEqual(ov["projects"][0]["session_count"], 1)

    def test_missing_cwd_warns_and_sessions_go_unassigned(self):
        self.h.make_sessions("id TEXT, started_at REAL", [("s1", 100.0)])
        ov = self.h.overview(setup={"status": "done", "mode": None, "at": None})
        self.assertTrue(any("cwd" in w for w in ov["warnings"]), ov["warnings"])
        self.assertEqual([s["id"] for s in ov["unassigned_sessions"]], ["s1"])

    def test_missing_table_is_silent_not_a_warning(self):
        """库存在但表不存在 → 空 + 静默（fresh 首启的正常态）。"""
        con = sqlite3.connect(str(self.h.home / "state.db"))
        con.execute("CREATE TABLE unrelated (x INTEGER)")
        con.commit()
        con.close()
        ov = self.h.overview(setup={"status": "pending", "mode": None, "at": None})
        self.assertEqual(ov["warnings"], [])
        self.assertEqual(ov["stats"]["sessions"], 0)

    def test_missing_db_is_silent(self):
        ov = self.h.overview(setup={"status": "pending", "mode": None, "at": None})
        self.assertEqual(ov["warnings"], [])
        self.assertEqual(ov["stats"]["sessions"], 0)

    def test_missing_primary_key_still_raises(self):
        """主键列缺失连「是哪条会话」都答不了 → 必须报错，不能降级成空列表。"""
        self.h.make_sessions("cwd TEXT, started_at REAL", [("C:/x", 1.0)])
        with self.assertRaises(ha.HostSchemaError):
            ha.read_sessions()


class TestQ04FreshScope(unittest.TestCase):
    """Q-04：fresh 缺 at → 作用域为空 + 告警；正常 fresh 只看法锚点之后的会话。"""

    def setUp(self):
        self.h = _TempHost()
        self.h.make_projects([PROJ])
        self.h.make_sessions(
            "id TEXT, cwd TEXT, started_at REAL, title TEXT, updated_at REAL",
            [("old", str(Path(PROJ[2])), 1000.0, "旧", 1000.0),
             ("new", str(Path(PROJ[2])), 2000.0, "新", 2000.0)])

    def tearDown(self):
        self.h.cleanup()

    def test_fresh_without_at_hides_everything_and_warns(self):
        ov = self.h.overview(setup={"status": "done", "mode": "fresh", "at": None})
        self.assertEqual(ov["stats"]["sessions"], 0, "fresh 缺 at 时不得看到任何会话")
        self.assertEqual(ov["projects"][0]["session_count"], 0)
        self.assertEqual(ov["unassigned_sessions"], [])
        self.assertTrue(any("引导状态异常" in w for w in ov["warnings"]), ov["warnings"])
        self.assertIsNone(ov["scope"]["since"])

    def test_fresh_with_at_filters_older_sessions(self):
        ov = self.h.overview(setup={"status": "done", "mode": "fresh",
                                    "at": "1970-01-01T00:25:00+00:00"})   # 1500 epoch
        self.assertEqual(ov["stats"]["sessions"], 1)
        self.assertEqual(ov["projects"][0]["session_count"], 1)
        self.assertEqual(ov["warnings"], [])
        self.assertAlmostEqual(ov["scope"]["since"], 1500.0, places=3)

    def test_imported_mode_sees_everything(self):
        ov = self.h.overview(setup={"status": "done", "mode": "imported", "at": "1970-01-01T01:00:00+00:00"})
        self.assertEqual(ov["stats"]["sessions"], 2)
        self.assertIsNone(ov["scope"]["since"])

    def test_fresh_from_meta_file_on_disk(self):
        """走真实文件路径：META.yaml 写 mode: fresh 且 at 空 → 同样空作用域 + 告警。"""
        self.h.make_meta(
            "setup:\n  status: done\n  mode: fresh\n  at: null\n"
            "unclassified_slug: 未分类\ncategories:\n  - 办公事务\n")
        ov = self.h.overview()
        self.assertEqual(ov["stats"]["sessions"], 0)
        self.assertTrue(any("引导状态异常" in w for w in ov["warnings"]), ov["warnings"])


class TestSetSetupOrder(unittest.TestCase):
    """Q-04 后端：set_setup 必须先写 at 再写 mode，且 at 是单向门。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cockpit-setup-"))
        self.p = self.dir / "META.yaml"
        self.p.write_text("setup:\n  status: pending\n  mode: null\n  at: null\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_at_written_before_mode(self):
        """观测顺序：set_setup 调用的第一个写入必须是 at（不是只看文件里的行位置）。

        文件里键的**物理行序**由模板决定（META.yaml 本来就是 status/mode/at 这个顺序），
        真实约束是「写入次序」——因为 at 是单向门，先写 mode 就可能把一个
        「mode 已 fresh、at 还空」的中间态留到磁盘上（Q-04）。
        """
        calls: list[str] = []
        original = meta_mod._set_simple

        def spy(text, section, key, value):
            calls.append(key)
            return original(text, section, key, value)

        meta_mod._set_simple = spy
        try:
            meta_mod.set_setup(self.p, status="done", mode="fresh",
                               at="2026-11-22T00:00:00+08:00")
        finally:
            meta_mod._set_simple = original

        self.assertEqual(calls, ["at", "mode", "status"],
                         f"写入次序不是 at→mode→status：{calls}")
        cfg = meta_mod.load(self.p)["setup"]
        self.assertEqual(cfg["mode"], "fresh")
        self.assertEqual(cfg["at"], "2026-11-22T00:00:00+08:00")
        self.assertEqual(cfg["status"], "done")

    def test_at_is_a_one_way_door(self):
        meta_mod.set_setup(self.p, at="2026-11-22T00:00:00+08:00")
        with self.assertRaises(meta_mod.MetaWriteError):
            meta_mod.set_setup(self.p, at="2027-01-01T00:00:00+08:00")
        self.assertEqual(meta_mod.load(self.p)["setup"]["at"], "2026-11-22T00:00:00+08:00")

    def test_no_intermediate_state_when_at_write_fails(self):
        """at 写不进去（已存在）时整批拒绝，不能先把 mode 改成 fresh。"""
        meta_mod.set_setup(self.p, at="2026-11-22T00:00:00+08:00")
        with self.assertRaises(meta_mod.MetaWriteError):
            meta_mod.set_setup(self.p, mode="fresh", at="2026-12-01T00:00:00+08:00")
        self.assertIsNone(meta_mod.load(self.p)["setup"]["mode"])

    def test_scope_warnings_unit(self):
        self.assertEqual(store.scope_warnings({"mode": "fresh", "at": "x"}), [])
        self.assertEqual(store.scope_warnings({"mode": "imported", "at": None}), [])
        self.assertEqual(len(store.scope_warnings({"mode": "fresh", "at": None})), 1)
        self.assertEqual(store.scope_at({"mode": "fresh", "at": None}), float("inf"))


if __name__ == "__main__":
    unittest.main()
