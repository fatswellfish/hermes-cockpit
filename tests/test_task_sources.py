"""三份任务文件（agent / work / home）的聚合与解析。

对应用户 2026-11-23 的要求：
  · todo 按来源分三份文件
  · 每条 todo 存 session id，没有的靠 project 兜底
  · 人看的是主条目，机器看的是它下面的子步骤
"""

import os
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "project-hub"))

from core import taskfile as tf          # noqa: E402
from dashboard import store              # noqa: E402


AGENT_MD = """# agent 任务

## T-001 让 agent 干的活 · doing · 50%

> session: local-deadbeef0123456789abcdef0123456789abcdef0123456789abcdef01234567
> source: agent

- [x] T-001.1 第一步
- [ ] T-001.2 第二步

**快照**：做到一半
**阻塞**：无

## 格式约定（以下不解析）
"""

WORK_MD = """# 办公日常

## T-001 跟客户对报价 · todo · 0%

> source: work

- [ ] T-001.1 拉一版成本
- [ ] T-001.2 发给客户

## 格式约定（以下不解析）
"""

HOME_MD = """# 家里的事

## T-001 给孩子约体检 · todo · 0%

> source: home

- [ ] T-001.1 查社区医院时间

## 格式约定（以下不解析）
"""


class TestThreeTaskFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, files: dict[str, str]):
        for name, body in files.items():
            (self.root / name).write_text(body, encoding="utf-8")

    def test_reads_all_three_files(self):
        """三份文件都要读进来，不是只读第一份。"""
        self._write({"TASKS.agent.md": AGENT_MD,
                     "TASKS.work.md": WORK_MD,
                     "TASKS.home.md": HOME_MD})
        _, tasks = store.read_tasks(self.root)
        self.assertEqual(len(tasks), 3)
        sources = {t["source"] for t in tasks}
        self.assertEqual(sources, {"agent", "work", "home"})

    def test_source_falls_back_to_filename(self):
        """文件里没写 > source: 时，按文件名推断来源。"""
        body = WORK_MD.replace("> source: work\n", "")
        self._write({"TASKS.work.md": body})
        _, tasks = store.read_tasks(self.root)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["source"], "work")

    def test_source_line_overrides_filename(self):
        """文件内显式声明的 source 优先于文件名（agent 帮拆的日常工作）。"""
        body = WORK_MD.replace("> source: work", "> source: agent")
        self._write({"TASKS.work.md": body})
        _, tasks = store.read_tasks(self.root)
        self.assertEqual(tasks[0]["source"], "agent")

    def test_source_file_recorded(self):
        """每条要记住自己来自哪个文件，写回时才不会串档。"""
        self._write({"TASKS.home.md": HOME_MD})
        _, tasks = store.read_tasks(self.root)
        self.assertEqual(tasks[0]["source_file"], "TASKS.home.md")

    def test_same_id_across_files_does_not_collide(self):
        """三份文件各自从 T-001 开始编号，合并不许报 id 重复。"""
        self._write({"TASKS.agent.md": AGENT_MD,
                     "TASKS.work.md": WORK_MD,
                     "TASKS.home.md": HOME_MD})
        _, tasks = store.read_tasks(self.root)
        self.assertEqual([t["id"] for t in tasks],
                         ["T-001", "T-001", "T-001"])

    def test_legacy_single_file_still_read(self):
        """三份都不存在时才回落旧的单个 TASKS.md。"""
        (self.root / "TASKS.md").write_text(AGENT_MD, encoding="utf-8")
        _, tasks = store.read_tasks(self.root)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["source"], "agent")
        self.assertEqual(tasks[0]["source_file"], "TASKS.md")

    def test_new_files_win_over_legacy(self):
        """三份存在时，旧 TASKS.md 即使还在也不参与，避免重复计数。"""
        self._write({"TASKS.agent.md": AGENT_MD,
                     "TASKS.md": HOME_MD})
        _, tasks = store.read_tasks(self.root)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "让 agent 干的活")

    def test_missing_all_files_is_empty_not_error(self):
        _, tasks = store.read_tasks(self.root)
        self.assertEqual(tasks, [])


class TestSessionField(unittest.TestCase):
    def test_english_session_line(self):
        """> session: <id> —— TASKS.agent.md 模板用的写法。"""
        tasks = tf.parse(AGENT_MD)
        self.assertEqual(tasks[0]["session"], "local-deadbeef0123456789abcdef0123456789abcdef0123456789abcdef01234567")

    def test_chinese_session_line_still_works(self):
        """**会话**：<id> —— 旧格式继续可用。"""
        body = "## T-001 标题 · todo · 0%\n\n**会话**：local-abcdef12345678\n"
        tasks = tf.parse(body)
        self.assertEqual(tasks[0]["session"], "local-abcdef12345678")

    def test_placeholder_is_not_a_session(self):
        """占位符/说明文字不许被当成会话 id。"""
        body = "## T-001 标题 · todo · 0%\n\n> session: （可不填，靠 proj 兜底）\n"
        tasks = tf.parse(body)
        self.assertIsNone(tasks[0]["session"])

    def test_short_value_rejected(self):
        body = "## T-001 标题 · todo · 0%\n\n> session: abc\n"
        self.assertIsNone(tf.parse(body)[0]["session"])

    def test_task_without_session(self):
        """没有 session 行是合法的——靠 project 兜底。"""
        body = "## T-001 标题 · todo · 0% · proj:mms\n"
        t = tf.parse(body)[0]
        self.assertIsNone(t["session"])
        self.assertEqual(t["project"], "mms")


class TestCommentAndFence(unittest.TestCase):
    def test_html_comment_not_parsed(self):
        """<!-- --> 里的任务不算。模板文件靠这个放示例。"""
        body = """# 标题

<!--
## T-001 被注释掉的 · todo · 0%
- [ ] T-001.1 不该出现
-->

## T-002 真的 · todo · 0%
"""
        tasks = tf.parse(body)
        self.assertEqual([t["id"] for t in tasks], ["T-002"])

    def test_single_line_comment(self):
        body = "<!-- ## T-001 单行注释 · todo · 0% -->\n\n## T-002 真的 · todo · 0%\n"
        self.assertEqual([t["id"] for t in tf.parse(body)], ["T-002"])

    def test_fenced_block_not_parsed(self):
        body = """## T-001 真的 · todo · 0%

```
## T-099 代码块里的例子 · todo · 0%
```
"""
        self.assertEqual([t["id"] for t in tf.parse(body)], ["T-001"])


class TestSubtaskProgress(unittest.TestCase):
    def test_subtask_done_drives_progress(self):
        """人看主条目、机器看子步骤：子步完成推动进度。"""
        body = """## T-001 主条目 · doing · 0%

- [x] T-001.1 完成
- [x] T-001.2 完成
- [ ] T-001.3 未完成
- [ ] T-001.4 未完成
"""
        t = tf.parse(body)[0]
        self.assertEqual(len(t["subtasks"]), 4)
        self.assertEqual(sum(1 for s in t["subtasks"] if s["done"]), 2)


class TestWriteIgnoresSourceFileHints(unittest.TestCase):
    """写操作必须落回原文件，不能一律写 TASKS.md。

    三份文件里可能都有 T-001，勾错就是数据串档。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        for name, body in (("TASKS.agent.md", AGENT_MD),
                           ("TASKS.work.md", WORK_MD),
                           ("TASKS.home.md", HOME_MD)):
            (self.root / name).write_text(body, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_taskfile_for_honours_explicit_filename(self):
        from dashboard import plugin_api as api
        self.assertEqual(api._taskfile_for(self.root, "TASKS.work.md").name,
                         "TASKS.work.md")

    def test_taskfile_for_finds_by_id(self):
        """只给 id 时去三份里找；T-001 三份都有，返回第一份命中的。"""
        from dashboard import plugin_api as api
        got = api._taskfile_for(self.root, "T-001").name
        self.assertIn(got, ("TASKS.agent.md", "TASKS.work.md", "TASKS.home.md"))

    def test_toggle_writes_only_target_file(self):
        """勾 work 的那条，agent 那份必须一个字节都不变。"""
        from dashboard import plugin_api as api
        from dashboard import fileio as fio

        agent_before = (self.root / "TASKS.agent.md").read_text(encoding="utf-8")
        work_p = self.root / "TASKS.work.md"
        _, stamp = fio.read_with_stamp(work_p)

        status, payload = api._handle_route("POST", "/tasks/toggle", {
            "root": str(self.root),
            "source_file": "TASKS.work.md",
            "id": "T-001",
            "subtask_id": "T-001.1",
            "done": True,
            "expect_mtime": list(stamp),
        })
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["task"]["subtasks"][0]["done"])

        self.assertIn("- [x] T-001.1", work_p.read_text(encoding="utf-8"))
        self.assertEqual(
            (self.root / "TASKS.agent.md").read_text(encoding="utf-8"),
            agent_before, "勾 work 的任务却动了 agent 那份")

    def test_toggle_without_stamp_is_refused(self):
        """没带戳就拒绝写（拒绝盲写），且文件不被改动。"""
        from dashboard import plugin_api as api
        before = (self.root / "TASKS.work.md").read_text(encoding="utf-8")
        status, payload = api._handle_route("POST", "/tasks/toggle", {
            "root": str(self.root),
            "source_file": "TASKS.work.md",
            "id": "T-001", "subtask_id": "T-001.1", "done": True,
        })
        self.assertEqual(status, 428, payload)
        self.assertEqual((self.root / "TASKS.work.md").read_text(encoding="utf-8"), before)


class TestOverviewStamps(unittest.TestCase):
    def test_overview_exposes_per_file_stamps(self):
        """三份文件各有自己的戳，前端勾哪条就带哪个。"""
        import shutil
        from dashboard import plugin_api as api

        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            for name in ("TASKS.agent.md", "TASKS.work.md", "TASKS.home.md"):
                shutil.copy(REPO / name, tmp / name)
            shutil.copy(REPO / "META.example.yaml", tmp / "META.yaml")
            payload = api._overview_with_stamp(tmp)
            self.assertIn("stamps", payload)
            self.assertIsInstance(payload["stamps"], dict)
            self.assertTrue(payload["stamps"], "三份文件都在，stamps 不该是空")
            for name in payload["stamps"]:
                self.assertIn(name, ("TASKS.agent.md", "TASKS.work.md", "TASKS.home.md"))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
