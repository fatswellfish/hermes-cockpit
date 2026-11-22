"""A1 数据访问层验收测试 —— host_adapter.py（唯一宿主接触面）。

覆盖（WBS A1 / DD-001 S13）：
  · 三个库各返回非空（projects / sessions / kanban）
  · 列名探测：人为构造「主键叫 session_id」的假表，assert require_column 报明确错误而非静默返回空
  · open_ro 打开的连接无法执行写（CREATE TABLE 抛错）
  · compat_status() 在版本号超区间时返回 readonly=True 且 reason 非空

真库只读、只在库真实存在时才跑（缺库即 skip 并说明，不静默通过）。
跑法：python -m unittest discover -s tests -v
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

import host_adapter as ha  # noqa: E402

COMPAT = REPO / "plugins" / "project-hub" / "dashboard" / "compat.yaml"


class TestRealHostDatabases(unittest.TestCase):
    """真库只读冒烟：三个库都要取到东西。"""

    def test_projects_non_empty(self):
        if not ha.projects_db().exists():
            self.skipTest(f"宿主 projects.db 不存在：{ha.projects_db()}")
        rows = ha.read_projects()
        self.assertGreater(len(rows), 0, "projects 返回空 —— 列名探测可能退化了")
        for r in rows:
            self.assertTrue(r["slug"])
            self.assertIsNotNone(r["name"])
        self.assertIsInstance(rows[0].get("board_slug"), (str, type(None)))

    def test_sessions_non_empty(self):
        if not ha.state_db().exists():
            self.skipTest(f"宿主 state.db 不存在：{ha.state_db()}")
        rows = ha.read_sessions(limit=5)
        self.assertGreater(len(rows), 0, "sessions 返回空 —— 主键列名探测可能退化了")
        self.assertEqual(len(rows), 5)
        for r in rows:
            self.assertTrue(r["id"])
            # started_at 必须是真值（实测 unix 秒浮点），否则 fresh 作用域失效
            self.assertIsNotNone(ha.to_epoch(r["started_at"]))

    def test_boards_non_empty_and_tasks_readable(self):
        if not ha.kanban_dir().is_dir():
            self.skipTest(f"宿主 kanban 目录不存在：{ha.kanban_dir()}")
        boards = ha.read_boards()
        self.assertGreater(len(boards), 0, "看板目录返回空")
        self.assertTrue(all(b["db"].endswith("kanban.db") for b in boards))
        # 逐板读卡，与直查 count(*) 比对（S13 第三条判据的数据侧前提）
        for b in boards:
            got = len(ha.read_board_tasks(b["slug"]))
            con = ha.open_ro(Path(b["db"]))
            try:
                want = con.execute("SELECT count(*) FROM tasks").fetchone()[0]
            finally:
                con.close()
                ha._OPEN_ROS.discard(con)
            self.assertEqual(got, want, f"{b['slug']} 卡片数与 count(*) 不符")


def _mk_fake_state(path: Path) -> None:
    """造一张「主键叫 session_id」的假 sessions 表：模拟宿主另一版本的命名。"""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, cwd TEXT)")
        con.execute("INSERT INTO sessions VALUES ('s1', 'C:/x')")
        con.commit()
    finally:
        con.close()


class TestColumnProbing(unittest.TestCase):
    """列名必须 PRAGMA 探测；探测不到要报错，不许静默返回空。

    假库文件名固定为 state.db：适配层按 hermes_home()/state.db 定位，
    host_adapter 只暴露 state_db() 一个定位函数，没有可注入的路径参数。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        _mk_fake_state(self.db)

    def tearDown(self):
        # open_ro 的连接不随 with 关闭（见 host_adapter.open_ro 文档），
        # 先统一释放，否则 Windows 下删临时目录会 WinError 32
        ha.close_all()
        self.tmp.cleanup()

    def test_require_column_finds_session_id(self):
        con = ha.open_ro(self.db)
        try:
            cols = ha.columns(con, "sessions")
        finally:
            con.close()
        self.assertEqual(cols, ["session_id", "cwd"])
        got = ha.require_column(cols, "id", "session_id", table="sessions", purpose="会话主键")
        self.assertEqual(got, "session_id")

    def test_require_column_raises_instead_of_silent_empty(self):
        """候选列全不存在时必须抛 HostSchemaError，而不是返回空列表。"""
        con = ha.open_ro(self.db)
        try:
            cols = ha.columns(con, "sessions")
        finally:
            con.close()
        with self.assertRaises(ha.HostSchemaError) as ctx:
            ha.require_column(cols, "session_pk", table="sessions", purpose="会话主键")
        msg = str(ctx.exception)
        self.assertIn("sessions", msg)          # 报出哪张表
        self.assertIn("会话主键", msg)          # 报出用途
        self.assertIn("session_pk", msg)        # 报出找的是什么
        self.assertIn("session_id", msg)        # 报出实际列，便于定位

    def test_columns_missing_table_returns_empty_not_raise(self):
        """表不存在时 columns() 返回空（由调用方决定是否视为致命）。"""
        con = ha.open_ro(self.db)
        try:
            self.assertEqual(ha.columns(con, "nope"), [])
        finally:
            con.close()

    def test_pick_column_is_case_insensitive(self):
        self.assertEqual(ha.pick_column(["Started_At"], "started_at"), "Started_At")
        self.assertIsNone(ha.pick_column(["foo"], "bar"))

    def test_hardcoded_created_at_is_not_used(self):
        """实测 66 列里没有 created_at：候选取 started_at 优先。"""
        cols = ["id", "cwd", "started_at", "title"]
        self.assertEqual(ha.pick_column(cols, "started_at", "created_at"), "started_at")

    def test_read_sessions_on_fake_table_uses_real_pk(self):
        """把 HERMES_HOME 指到假库，read_sessions 必须按 session_id 读出。

        注意：read_sessions 内部 `with open_ro(...)` 结束后连接仍被 sqlite3 的
        上下文管理器留在内部引用里，Windows 下句柄要等 gc 才释放。所以断言读完后
        立刻 gc.collect()，否则 tearDown 删目录会 WinError 32。
        """
        import gc
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self.tmp.name
        try:
            rows = ha.read_sessions()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old
        gc.collect()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "s1")


class TestReadOnlyConnection(unittest.TestCase):
    """S13 第一条判据：只读句柄。"""

    def test_open_ro_blocks_create_table(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE t (a)")
            con.commit()
            con.close()

            ro = ha.open_ro(db)
            try:
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    ro.execute("CREATE TABLE u (a)")
                self.assertIn("readonly", str(ctx.exception).lower())
            finally:
                ro.close()

    def test_open_ro_blocks_insert_update_delete_on_real_db(self):
        if not ha.state_db().exists():
            self.skipTest("宿主 state.db 不存在")
        con = ha.open_ro(ha.state_db())
        try:
            for sql in ("INSERT INTO sessions (id) VALUES ('x')",
                        "UPDATE sessions SET cwd='x'",
                        "DELETE FROM sessions"):
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute(sql)
        finally:
            con.close()

    def test_open_ro_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            ha.open_ro(Path(tempfile.gettempdir()) / "definitely_not_here.db")

    def test_with_block_does_not_close_connection_so_explicit_close_is_required(self):
        """实测坑：`with open_ro(...)` 退出后连接仍可用 —— with 不是 close。

        py3.14 的 `sqlite3.Connection.close` 是只读属性，无法在实例上包一层，
        所以适配层内部一律 `try/finally: con.close()`（read_* 已这么写），
        close_all() 只作兜底（插件卸载 / 测试收尾）。
        """
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            sqlite3.connect(db).close()
            before = set(ha._OPEN_ROS)
            with ha.open_ro(db) as con:
                pass
            # with 已退出，连接仍然活着（证明 with ≠ close）
            self.assertEqual(con.execute("SELECT 1").fetchone()[0], 1)
            self.assertIn(con, ha._OPEN_ROS)
            self.assertEqual(len(ha._OPEN_ROS - before), 1)
            con.close()
            ha._OPEN_ROS.discard(con)
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")

    def test_read_helpers_leave_no_open_connection(self):
        """read_projects / read_sessions / read_board_tasks 读完必须不残留句柄。

        这是「概览接口每 4 秒轮询一次 → 句柄累积到宿主库文件上」的回归测试。
        """
        if not (ha.projects_db().exists() and ha.state_db().exists()):
            self.skipTest("宿主库不存在")
        before = len(ha._OPEN_ROS)
        ha.read_projects()
        ha.read_sessions(limit=3)
        for b in ha.read_boards()[:3]:
            ha.read_board_tasks(b["slug"])
        self.assertEqual(len(ha._OPEN_ROS), before,
                         f"读完仍残留 {len(ha._OPEN_ROS) - before} 个连接")

    def test_close_all_releases_file_handle_on_windows(self):
        """close_all 之后临时目录能被删掉（句柄真释放，不只是标记）。"""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            sqlite3.connect(db).close()
            ha.open_ro(db).execute("SELECT 1").fetchone()
            self.assertGreaterEqual(ha.close_all(), 1)
            os.unlink(db)          # 句柄未释放会 WinError 32
            self.assertFalse(db.exists())

    def test_close_all_closes_everything_it_opened(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "state.db"
            sqlite3.connect(db).close()
            a, b = ha.open_ro(db), ha.open_ro(db)
            self.assertGreaterEqual(ha.close_all(), 2)
            for c in (a, b):
                with self.assertRaises(sqlite3.ProgrammingError):
                    c.execute("SELECT 1")

    def test_adapter_source_has_no_write_sql(self):
        """适配层源码里不得出现写语句（S13 第二条判据的静态侧）。"""
        src = (REPO / "plugins" / "project-hub" / "dashboard" / "host_adapter.py").read_text(
            encoding="utf-8")
        sys.path.insert(0, str(REPO / "scripts"))
        import check_boundary as cb
        hits = [f for f in cb.scan_text(src, "host_adapter.py", ".py")
                if f.kind == "WRITE_SQL"]
        self.assertEqual(hits, [], f"适配层出现写语句：{[str(h) for h in hits]}")


class TestCompatStatus(unittest.TestCase):
    """F2/S8：版本超区间 ⇒ 既提示（reason 非空）又降级（readonly=True）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.old_home = os.environ.get("HERMES_HOME")
        import json
        # host_version() 优先读 gateway_state.json 的 code_version
        (self.home / "gateway_state.json").write_text(
            json.dumps({"code_version": "0.23.2"}), encoding="utf-8")
        os.environ["HERMES_HOME"] = str(self.home)
        ha._COMPAT_CACHE = None

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old_home
        ha._COMPAT_CACHE = None
        self.tmp.cleanup()

    def _compat_file(self, lo: str, hi: str) -> Path:
        f = self.home / "compat.yaml"
        f.write_text(f'host: hermes\nmin_version: "{lo}"\nmax_version: "{hi}"\n'
                     f'on_mismatch: readonly\n', encoding="utf-8")
        ha._COMPAT_CACHE = None
        return f

    def test_real_compat_file_is_ok(self):
        ha._COMPAT_CACHE = None
        st = ha.compat_status(COMPAT)
        self.assertTrue(st["ok"])
        self.assertFalse(st["readonly"])
        self.assertEqual(st["reason"], "")

    def test_above_range_degrades_to_readonly(self):
        st = ha.compat_status(self._compat_file("0.1.0", "0.22.0"))
        self.assertFalse(st["ok"])
        self.assertTrue(st["readonly"])
        self.assertTrue(st["reason"].strip(), "降级时必须给出中文原因，不能空")
        self.assertIn("0.23.2", st["reason"])
        self.assertIn("只读", st["reason"])

    def test_below_range_degrades_to_readonly(self):
        st = ha.compat_status(self._compat_file("0.30.0", "0.99.0"))
        self.assertFalse(st["ok"])
        self.assertTrue(st["readonly"])
        self.assertTrue(st["reason"].strip())

    def test_unknown_version_warns_but_not_readonly(self):
        (self.home / "gateway_state.json").write_text("{}", encoding="utf-8")
        ha._COMPAT_CACHE = None
        st = ha.compat_status(self._compat_file("0.1.0", "0.22.0"))
        self.assertTrue(st["warn"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
