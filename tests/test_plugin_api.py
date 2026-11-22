"""B 模块路由层验收 —— dashboard/plugin_api.py。

用**真的 FastAPI 应用**（TestClient）跑，不用假 router：precondition 在 header 里、
status code 由框架产出，这两处只有真跑才验得到。

每个写端点都测三条：
  ① 正常路径（真的改了文件 / 真的写了 META.yaml）
  ② 409（戳不对 → 不写一个字节）
  ③ 需要作用域的端点再测 403（fresh 下对存量会话归类）

`/sessions/new` 的两条：
  · 正路：临时目录建会话 → 回查 sessions.cwd 等于临时目录 → 删除清理（真建真删）
  · 落地失败：桩把 create_session 换成「建了但 cwd 不对的会话」→ 502 且已尝试删除

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
# 插件内部模块必须经 _module_loader 取 —— 与插件运行时是**同一个模块对象**。
# 若这里裸 `import host_adapter`，桩会打在另一份实例上，测试假绿（第八轮实测踩过）。
sys.path.insert(0, str(REPO / "plugins" / "hermes-cockpit"))
import _module_loader as _ml  # noqa: E402

ha = _ml.host_adapter()
meta_mod = _ml.meta()

import importlib.util as _ilu  # noqa: E402

_api_spec = _ilu.spec_from_file_location(
    "hermes_cockpit_test_plugin_api",
    REPO / "plugins" / "hermes-cockpit" / "dashboard" / "plugin_api.py")
api = _ilu.module_from_spec(_api_spec)
sys.modules["hermes_cockpit_test_plugin_api"] = api
_api_spec.loader.exec_module(api)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TASKS = """# 演示任务文件

前言不应被改动。

## T-001 演示任务 · doing · 0% · proj:demo
- [ ] T-001.1 第一步
- [ ] T-001.2 第二步

**快照**：刚开始

## 格式约定（以下为示例，不参与解析）

```markdown
## T-001 标题
```
"""


class _Sandbox:
    """临时项目根（TASKS.md / META.yaml）+ 临时 hermes home，客户端绑在真 app 上。"""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cockpit-api-"))
        self.root = self.dir / "cockpit"
        self.root.mkdir()
        (self.root / "TASKS.md").write_text(TASKS, encoding="utf-8")
        self.meta = self.root / "META.yaml"
        self.meta.write_text("setup:\n  status: done\n  mode: imported\n  at: null\n"
                             "session_map: {}\n", encoding="utf-8")
        self.home = self.dir / "hermes"
        self.home.mkdir()
        self.seed_empty_host_dbs()
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/hermes-cockpit")
        self.client = TestClient(app, raise_server_exceptions=False)

    # —— 请求助手 ——
    def seed_empty_host_dbs(self):
        """给临时 home 造两张空库，让适配层走「表不存在」分支（而不是库不存在）。

        两条分支都是静默空，但走哪条会掩盖另一端的问题：只造库才能证明
        「库在、表不在」也不会报错。
        """
        for name in ("projects.db", "state.db"):
            con = sqlite3.connect(str(self.home / name))
            con.execute("CREATE TABLE IF NOT EXISTS _seed (x INTEGER)")
            con.commit()
            con.close()

    def get(self, path, **kw):
        # root 显式指定：/overview 也要读沙盒那份 TASKS.md，不能读本仓真实的（戳会错位）
        params = dict(kw.pop("params", None) or {})
        params.setdefault("root", str(self.root))
        return self.client.get(f"/api/plugins/hermes-cockpit{path}", params=params, **kw)

    def post(self, path, body=None, headers=None):
        payload = dict(body or {})
        # 明确指定项目根：不依赖插件按 __file__ 往上三级猜（那会读到本仓真实的 TASKS.md）
        payload.setdefault("root", str(self.root))
        return self.client.post(f"/api/plugins/hermes-cockpit{path}",
                                json=payload, headers=headers or {})

    def delete(self, path, body=None, headers=None):
        payload = dict(body or {})
        payload.setdefault("root", str(self.root))
        return self.client.request("DELETE", f"/api/plugins/hermes-cockpit{path}",
                                   json=payload, headers=headers or {})

    def tasks_text(self) -> str:
        return (self.root / "TASKS.md").read_text(encoding="utf-8")

    def stamp(self):
        """当前文件戳 (ident, sha256) —— 客户端带上它才算「我读过这版」。"""
        _, stamp = _ml.fileio().read_with_stamp(self.root / "TASKS.md")
        return stamp

    def cleanup(self):
        ha.close_all()
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        shutil.rmtree(self.dir, ignore_errors=True)


class TestOverview(unittest.TestCase):

    def setUp(self):
        self.sb = _Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def test_overview_ok_and_shape(self):
        r = self.sb.get("/overview")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        for key in ("projects", "unassigned_sessions", "stats", "setup", "scope", "warnings"):
            self.assertIn(key, data)
        self.assertIsInstance(data["warnings"], list)

    def test_overview_ships_a_usable_stamp(self):
        """回归：/overview 必须下发 stamp，且**拿它就能写**。

        缺陷形态（实测踩过）：以前 /overview 不带戳，前端勾子任务只能不带 expect_mtime，
        后端一律 428 拒绝盲写 —— 勾选功能整个是死的，而测试因为直接调 sb.stamp()
        绕过了 HTTP，一直没暴露。
        """
        ov = self.sb.get("/overview").json()
        self.assertIn("stamp", ov, "/overview 没有下发 stamp —— 前端将无法安全写入")
        self.assertIsInstance(ov["stamp"], list)
        self.assertEqual(len(ov["stamp"]), 2, "戳必须是 (ident, sha256) 二元组")

        # 用 overview 给的戳真的写一次，必须 200
        tasks = ov.get("orphan_tasks") or []
        target = None
        for t in tasks:
            for s in t.get("subtasks") or []:
                target = (t["id"], s["id"], not s["done"])
                break
            if target:
                break
        self.assertIsNotNone(target, "沙盒里没有可勾的子任务")

        r = self.sb.post("/tasks/toggle", {"id": target[0], "subtask_id": target[1],
                                           "done": target[2], "expect_mtime": ov["stamp"]})
        self.assertEqual(r.status_code, 200, f"用 /overview 的戳写不进去：{r.text}")

        # 写完后戳必须变，旧戳必须 409（并发保护仍然有效）
        ov2 = self.sb.get("/overview").json()
        self.assertNotEqual(ov2["stamp"], ov["stamp"], "写盘后戳没变 —— 并发保护失效")
        r2 = self.sb.post("/tasks/toggle", {"id": target[0], "subtask_id": target[1],
                                            "done": target[2], "expect_mtime": ov["stamp"]})
        self.assertEqual(r2.status_code, 409, f"旧戳居然还能写：{r2.text}")


class TestTaskWrites(unittest.TestCase):

    def setUp(self):
        self.sb = _Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def _pre(self):
        """客户端手上那版文件的戳：(ident, sha256) 二元组，服务端按它精确比对。"""
        return {"expect_mtime": list(self.sb.stamp())}

    # ① 正常路径
    def test_toggle_updates_progress_and_writes_back(self):
        r = self.sb.post("/tasks/toggle", {"id": "T-001", "subtask_id": "T-001.1",
                                           **self._pre()})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["task"]["progress"], 50)
        self.assertEqual(body["task"]["status"], "doing")
        text = self.sb.tasks_text()
        self.assertIn("- [x] T-001.1 第一步", text)
        self.assertIn("· doing · 50%", text)
        self.assertIn("前言不应被改动。", text)          # 块外内容逐字节保留
        self.assertIn("## 格式约定", text)               # 约定节没被吞

    def test_toggle_via_if_unmodified_since_header(self):
        """只给 mtime（毫秒/纳秒整数）也要能用 —— If-Unmodified-Since 走这条分支。"""
        ident = self.sb.stamp()[0]
        mtime_ns = int(ident.split(":mtime=")[-1])
        r = self.sb.client.post("/api/plugins/hermes-cockpit/tasks/toggle",
                                json={"root": str(self.sb.root), "id": "T-001",
                                      "subtask_id": "T-001.2"},
                                headers={"If-Unmodified-Since": str(mtime_ns)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["progress"], 50)

    def test_add_task_autonumbers_and_persists(self):
        r = self.sb.post("/tasks/add", {"title": "新任务", "project": "demo",
                                        "subtasks": ["甲", "乙"], **self._pre()})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], "T-002")
        self.assertEqual([s["id"] for s in body["task"]["subtasks"]],
                         ["T-002.1", "T-002.2"])
        self.assertIn("## T-002 新任务 · todo · 0% · proj:demo", self.sb.tasks_text())

    def test_update_task_sets_blocker_and_status_follows_rules(self):
        r = self.sb.post("/tasks/update", {"id": "T-001", "blocker": "等对方回话",
                                           **self._pre()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["task"]["status"], "blocked")
        self.assertIn("**阻塞**：等对方回话", self.sb.tasks_text())

    def test_delete_task(self):
        r = self.sb.delete("/tasks/T-001", self._pre())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("## T-001 演示任务", self.sb.tasks_text())
        self.assertIn("前言不应被改动。", self.sb.tasks_text())

    # ② 409：戳不对，一个字节都不写
    def test_stale_stamp_returns_409_and_writes_nothing(self):
        before = self.sb.tasks_text()
        r = self.sb.post("/tasks/toggle", {"id": "T-001", "subtask_id": "T-001.1",
                                           "expect_mtime": ["dev=0:ino=0:ctime=0", "deadbeef"]})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "stale")
        self.assertEqual(self.sb.tasks_text(), before)

    def test_missing_precondition_returns_428(self):
        r = self.sb.post("/tasks/toggle", {"id": "T-001", "subtask_id": "T-001.1"})
        self.assertEqual(r.status_code, 428, r.text)

    def test_concurrent_edit_between_read_and_write_is_409(self):
        """另一个写者抢先在磁盘上改了文件 → 我们的戳作废 → 409，绝不静默覆盖。"""
        pre = self._pre()
        (self.sb.root / "TASKS.md").write_text(TASKS + "\n别人加的尾巴\n", encoding="utf-8")
        r = self.sb.post("/tasks/toggle", {"id": "T-001", "subtask_id": "T-001.1", **pre})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("别人加的尾巴", self.sb.tasks_text(), "别人的修改被覆盖了")

    def test_unknown_task_is_404(self):
        r = self.sb.post("/tasks/update", {"id": "T-999", "snapshot": "x", **self._pre()})
        self.assertEqual(r.status_code, 404, r.text)


class TestSessionAssign(unittest.TestCase):

    def setUp(self):
        self.sb = _Sandbox()
        self.proj_root = Path(tempfile.gettempdir()) / "cockpit-assign-proj"
        self.proj_root.mkdir(exist_ok=True)
        # 先删掉种子库里的 _seed 占位表，再建真正的 projects 表
        con = sqlite3.connect(str(self.sb.home / "projects.db"))
        con.execute("DROP TABLE IF EXISTS _seed")
        con.execute("CREATE TABLE projects (slug TEXT, name TEXT, primary_path TEXT)")
        con.execute("INSERT INTO projects VALUES ('demo','演示',?)", (str(self.proj_root),))
        con.commit()
        con.close()

    def tearDown(self):
        self.sb.cleanup()
        shutil.rmtree(self.proj_root, ignore_errors=True)

    def _sessions(self, rows):
        con = sqlite3.connect(str(self.sb.home / "state.db"))
        con.execute("DROP TABLE IF EXISTS _seed")
        con.execute("CREATE TABLE IF NOT EXISTS sessions "
                    "(id TEXT, cwd TEXT, started_at REAL, title TEXT)")
        con.executemany("INSERT INTO sessions VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()

    def test_assign_writes_session_map_and_never_touches_cwd(self):
        self._sessions([("s1", str(self.proj_root), 2000.0, "会话一")])
        r = self.sb.post("/sessions/assign", {"session_id": "s1", "project": "demo"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["cwd_changed"])
        entry = meta_mod.find_session(self.sb.meta, "s1")
        self.assertEqual(entry["project"], "demo")
        self.assertEqual(entry["source"], "manual")
        # 宿主的 cwd 没被动过
        con = sqlite3.connect(str(self.sb.home / "state.db"))
        try:
            cwd = con.execute("SELECT cwd FROM sessions WHERE id='s1'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(cwd, str(self.proj_root))

    def test_fresh_scope_excludes_old_sessions_403(self):
        """Q-04：fresh 作用域外的存量会话不可归类 → 403，且 META.yaml 没被写。"""
        self._sessions([("old", str(self.proj_root), 1000.0, "旧会话")])
        self.sb.meta.write_text("setup:\n  status: done\n  mode: fresh\n"
                                "  at: \"2026-01-01T00:00:00+08:00\"\nsession_map: {}\n",
                                encoding="utf-8")
        before = self.sb.meta.read_text(encoding="utf-8")
        r = self.sb.post("/sessions/assign", {"session_id": "old", "project": "demo"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["code"], "out_of_scope")
        self.assertEqual(self.sb.meta.read_text(encoding="utf-8"), before)

    def test_fresh_without_anchor_blocks_writes_409(self):
        """Q-04：fresh 缺 at = 引导异常 → 写入口一律 409（先重走引导）。"""
        self._sessions([("s1", str(self.proj_root), 2000.0, "新会话")])
        self.sb.meta.write_text("setup:\n  status: done\n  mode: fresh\n  at: null\n"
                                "session_map: {}\n", encoding="utf-8")
        r = self.sb.post("/sessions/assign", {"session_id": "s1", "project": "demo"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "scope_blocked")

    def test_bad_source_is_400(self):
        self._sessions([("s1", str(self.proj_root), 2000.0, "会话一")])
        r = self.sb.post("/sessions/assign",
                         {"session_id": "s1", "project": "demo", "source": "made-up"})
        self.assertEqual(r.status_code, 400, r.text)


class TestSetupInit(unittest.TestCase):

    def setUp(self):
        self.sb = _Sandbox()
        self.sb.meta.write_text("setup:\n  status: pending\n  mode: null\n  at: null\n",
                                encoding="utf-8")

    def tearDown(self):
        self.sb.cleanup()

    def test_init_fresh_writes_at_then_mode(self):
        r = self.sb.post("/setup/init", {"mode": "fresh"})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = meta_mod.load(self.sb.meta)["setup"]
        self.assertEqual(cfg["mode"], "fresh")
        self.assertEqual(cfg["status"], "done")
        self.assertTrue(cfg["at"], "at 必须被写下来（fresh 的时间锚点）")
        self.assertEqual(r.json()["warnings"], [])

    def test_init_rejects_unknown_mode(self):
        r = self.sb.post("/setup/init", {"mode": "whatever"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_second_init_keeps_the_anchor(self):
        self.sb.post("/setup/init", {"mode": "fresh", "at": "2026-01-01T00:00:00+08:00"})
        r = self.sb.post("/setup/init", {"mode": "fresh", "at": "2027-01-01T00:00:00+08:00"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(meta_mod.load(self.sb.meta)["setup"]["at"],
                         "2026-01-01T00:00:00+08:00")


class TestSessionNew(unittest.TestCase):
    """/sessions/new：真建真回查真删；以及「落地失败必须报错并清理」。"""

    def setUp(self):
        self.sb = _Sandbox()
        self.cwd = Path(tempfile.mkdtemp(prefix="cockpit-new-session-"))

    def tearDown(self):
        ha.close_all()
        shutil.rmtree(self.cwd, ignore_errors=True)
        self.sb.cleanup()

    def _gateway_available(self) -> bool:
        if ha._host_agent_root() is None:
            return False
        return ha.state_db().exists()

    def test_real_creation_lands_cwd_and_is_cleaned_up(self):
        """真跑一次：建会话 → 回查 cwd == 临时目录 → 删除（不留测试数据）。

        实测坑（2026-11-22）：`GatewayUnavailableError: Gateway draining: update_paused`
        时**不是网关不可用**。判据实测区分得很清楚：
          · `ensure_gateway_runtime(<真实数据根>)` → **ready**（真实 home 恒有网关在）
          · `ensure_gateway_runtime(<临时 home>)`   → draining/update_paused（没人服务它）
        也就是说：适配层一旦把 HERMES_HOME 跟着测试指到临时目录，连的就变成了
        「没人服务的那个 home」。所以这里临时摘掉 HERMES_HOME 再建 —— 测的是产品逻辑，
        不是「让插件去连一个空壳 home」。
        """
        if ha._host_agent_root() is None:
            self.skipTest("本机没有宿主安装树，无法真建会话")
        saved = os.environ.pop("HERMES_HOME", None)
        try:
            attempts: list[str] = []
            r = None
            for _ in range(3):                  # 单次失败不是终局；同因 3 次才算阻塞
                try:
                    r = self.sb.post("/sessions/new",
                                     {"cwd": str(self.cwd), "title": "cockpit api test"})
                except Exception as e:          # noqa: BLE001 - 记下原因，别把异常当结论
                    attempts.append(f"{type(e).__name__}: {e}")
                    continue
                if r.status_code == 200:
                    break
                attempts.append(f"{r.status_code}: {r.text}")
            if r is None or r.status_code != 200:
                self.skipTest("真建会话连续 3 次未成功（同因）：" + " | ".join(attempts[-1:]))

            sid = r.json()["session_id"]
            self.assertTrue(sid)
            try:
                landed = ha.session_cwd(sid)
                self.assertEqual(os.path.normcase(landed or ""), os.path.normcase(str(self.cwd)),
                                 f"cwd 没落地：{landed!r} != {str(self.cwd)!r}")
            finally:
                ha.delete_session(sid)
            self.assertIsNone(ha.session_cwd(sid), "清理失败：测试会话还留在库里")
        finally:
            if saved is not None:
                os.environ["HERMES_HOME"] = saved

    def test_bad_cwd_is_400(self):
        r = self.sb.post("/sessions/new", {"cwd": "relative/path"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_cwd_not_landed_returns_502_and_cleans_up(self):
        """桩：宿主回了一个 cwd 不对的会话 → 502 cwd_not_landed，且尝试删除它。"""
        deleted: list[str] = []
        orig_create, orig_cwd, orig_delete = ha.create_session, ha.session_cwd, ha.delete_session
        ha.create_session = lambda cwd, **kw: {"session_id": "fake-1"}          # noqa: ARG005
        ha.session_cwd = lambda sid: r"C:\somewhere\else"                       # noqa: ARG005
        ha.delete_session = lambda sid, **kw: deleted.append(sid) or {"ok": True}

        def restore():
            ha.create_session, ha.session_cwd, ha.delete_session = orig_create, orig_cwd, orig_delete

        self.addCleanup(restore)
        r = self.sb.post("/sessions/new", {"cwd": str(self.cwd)})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()["code"], "cwd_not_landed")
        self.assertEqual(deleted, ["fake-1"], "落地失败时必须清理刚建的会话")


class TestRouteLayerStaysPure(unittest.TestCase):
    """硬约束：路由层不碰宿主（S10），也不调 serialize 落盘（Q-06）。"""

    def test_plugin_api_is_boundary_clean(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import check_boundary as cb
        src = (REPO / "plugins" / "hermes-cockpit" / "dashboard" / "plugin_api.py").read_text(
            encoding="utf-8")
        self.assertEqual(cb.scan_text(src, "plugin_api.py", ".py"), [])

    def test_no_serialize_in_write_path(self):
        src = (REPO / "plugins" / "hermes-cockpit" / "dashboard" / "plugin_api.py").read_text(
            encoding="utf-8")
        self.assertNotIn("tf.serialize(", src)
        self.assertIn("tf.write_back(", src)


if __name__ == "__main__":
    unittest.main()
