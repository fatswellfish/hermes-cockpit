"""宿主入口 handle() 验收 —— 不依赖 HTTP，直接按宿主真实调用方式打（PLUGIN-API-约定.md）。

宿主两种加载形态都要能跑：
  · 普通模块式：`plugin_api.handle(method, path, body) -> dict`
  · FastAPI 路由式：`plugin_api.router`（见 test_plugin_api.py 的真 app 测试）

本文件锁死前者，并保证两条路的判据一致（同样的 409 / 403 / 428 / 404 语义）。

跑法：python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "plugins" / "hermes-cockpit" / "dashboard",
           REPO / "plugins" / "hermes-cockpit" / "core"):
    sys.path.insert(0, str(_p))

import fileio as fio  # noqa: E402
import host_adapter as ha  # noqa: E402
import meta as meta_mod  # noqa: E402
import plugin_api as api  # noqa: E402

TASKS = """# 演示

## T-001 演示任务 · doing · 0% · proj:demo
- [ ] T-001.1 第一步
- [ ] T-001.2 第二步

## 格式约定（以下为示例，不参与解析）

```markdown
## T-001 标题
```
"""


class _Box:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cockpit-handle-"))
        self.root = self.dir / "cockpit"
        self.root.mkdir()
        (self.root / "TASKS.md").write_text(TASKS, encoding="utf-8")
        self.meta = self.root / "META.yaml"
        self.meta.write_text("setup:\n  status: done\n  mode: imported\n  at: null\n"
                             "session_map: {}\n", encoding="utf-8")
        self.home = self.dir / "hermes"
        self.home.mkdir()
        for name in ("projects.db", "state.db"):
            con = sqlite3.connect(str(self.home / name))
            con.execute("CREATE TABLE _seed (x INTEGER)")
            con.commit()
            con.close()
        self._old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)

    def call(self, method, path, body=None):
        payload = dict(body or {})
        payload.setdefault("root", str(self.root))
        return api.handle(method, path, payload)

    def stamp(self):
        return list(fio.read_with_stamp(self.root / "TASKS.md")[1])

    def cleanup(self):
        ha.close_all()
        if self._old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)


class TestHandleEntry(unittest.TestCase):

    def setUp(self):
        self.box = _Box()

    def tearDown(self):
        self.box.cleanup()

    def test_overview(self):
        r = self.box.call("GET", "/overview")
        self.assertTrue(r["ok"], r)
        self.assertIn("projects", r)
        self.assertIn("warnings", r)

    def test_trailing_slash_tolerated(self):
        self.assertTrue(self.box.call("GET", "/overview/")["ok"])
        self.assertTrue(self.box.call("GET", "overview")["ok"])

    def test_toggle_ok(self):
        r = self.box.call("POST", "/tasks/toggle",
                          {"id": "T-001", "subtask_id": "T-001.1", "expect_mtime": self.box.stamp()})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["task"]["progress"], 50)
        self.assertIn("- [x] T-001.1 第一步",
                      (self.box.root / "TASKS.md").read_text(encoding="utf-8"))

    def test_stale_is_code_409(self):
        before = (self.box.root / "TASKS.md").read_text(encoding="utf-8")
        r = self.box.call("POST", "/tasks/toggle",
                          {"id": "T-001", "subtask_id": "T-001.1",
                           "expect_mtime": ["dev=0:ino=0:ctime=0", "deadbeef"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], 409)
        self.assertEqual((self.box.root / "TASKS.md").read_text(encoding="utf-8"), before)

    def test_missing_precondition_is_428(self):
        r = self.box.call("POST", "/tasks/toggle", {"id": "T-001", "subtask_id": "T-001.1"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], 428)

    def test_add_and_delete(self):
        r = self.box.call("POST", "/tasks/add",
                          {"title": "新任务", "project": "demo", "expect_mtime": self.box.stamp()})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["id"], "T-002")
        r = self.box.call("DELETE", "/tasks/T-002", {"expect_mtime": self.box.stamp()})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("## T-002", (self.box.root / "TASKS.md").read_text(encoding="utf-8"))

    def test_unknown_route_is_404_and_no_exception(self):
        r = self.box.call("GET", "/nope")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], 404)

    def test_assign_writes_meta(self):
        r = self.box.call("POST", "/sessions/assign",
                          {"session_id": "s-1", "project": "demo"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(meta_mod.find_session(self.box.meta, "s-1")["project"], "demo")

    def test_fresh_out_of_scope_is_code_403(self):
        con = sqlite3.connect(str(self.box.home / "state.db"))
        con.execute("DROP TABLE IF EXISTS _seed")
        con.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, started_at REAL, title TEXT)")
        con.execute("INSERT INTO sessions VALUES ('old','C:/x',1000.0,'旧')")
        con.commit()
        con.close()
        self.box.meta.write_text("setup:\n  status: done\n  mode: fresh\n"
                                 "  at: \"2026-01-01T00:00:00+08:00\"\nsession_map: {}\n",
                                 encoding="utf-8")
        r = self.box.call("POST", "/sessions/assign", {"session_id": "old", "project": "demo"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], 403)

    def test_setup_init(self):
        self.box.meta.write_text("setup:\n  status: pending\n  mode: null\n  at: null\n",
                                 encoding="utf-8")
        r = self.box.call("POST", "/setup/init", {"mode": "fresh"})
        self.assertTrue(r["ok"], r)
        cfg = meta_mod.load(self.box.meta)["setup"]
        self.assertEqual(cfg["mode"], "fresh")
        self.assertTrue(cfg["at"])


if __name__ == "__main__":
    unittest.main()
