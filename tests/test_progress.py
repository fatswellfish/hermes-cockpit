"""Q-07 验收 —— core/progress.py::recompute 表驱动测试（裁定表 5 行全测）。

除表驱动之外还锁死三条硬规则：
  · 有阻塞就 blocked，即使子任务全完成、即使手写 100% 也不覆盖
  · 有子任务时手写百分比被覆盖；无子任务时手写值原样保留
  · recompute 是纯函数：不改入参、不共享子任务列表

跑法：python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "hermes-cockpit" / "core"))

import progress as pg  # noqa: E402
import taskfile as tf  # noqa: E402


def _task(status="todo", progress=0, subtasks=None, blocker=None):
    t = tf._empty_task()
    t.update({"id": "T-900", "title": "表驱动", "status": status,
              "progress": progress, "subtasks": subtasks or [], "blocker": blocker})
    return t


def _sub(sid, done, title="子任务"):
    return {"id": sid, "title": title, "done": done}


# 规则表（QUESTIONS-裁定.md Q-07）逐行落地：
#   (说明, 输入, 期望 status, 期望 progress)
TABLE = [
    ("1 有阻塞 → blocked，百分比按比例不改",
     _task(status="doing", progress=100, blocker="等对方回话",
           subtasks=[_sub("T-900.1", True), _sub("T-900.2", False),
                     _sub("T-900.3", False), _sub("T-900.4", False)]),
     "blocked", 25),
    ("2 子任务全部完成 → done 100",
     _task(status="doing", progress=40,
           subtasks=[_sub("T-900.1", True), _sub("T-900.2", True)]),
     "done", 100),
    ("3 部分完成 → doing round(p)",
     _task(status="todo", progress=0,
           subtasks=[_sub("T-900.1", True), _sub("T-900.2", False), _sub("T-900.3", False)]),
     "doing", 33),
    ("4 全未完成且无 doing 标记 → todo 0",
     _task(status="doing", progress=55,
           subtasks=[_sub("T-900.1", False), _sub("T-900.2", False)]),
     "todo", 0),
    ("5 无子任务 → 手写值原样保留",
     _task(status="blocked", progress=70, blocker=None, subtasks=[]),
     "blocked", 70),
]


class TestRuleTable(unittest.TestCase):
    """裁定表 5 行，逐行断言。"""

    def test_table_rows(self):
        for name, task, want_status, want_progress in TABLE:
            with self.subTest(name):
                got = pg.recompute(task)
                self.assertEqual(got["status"], want_status, name)
                self.assertEqual(got["progress"], want_progress, name)

    def test_table_has_five_rows(self):
        self.assertEqual(len(TABLE), 5)


class TestHardRules(unittest.TestCase):
    """三条硬规则的独立用例（不只靠表）。"""

    def test_blocker_beats_100_percent(self):
        """阻塞优先于 100%：子任务全完成但写着阻塞 → blocked。"""
        t = _task(status="done", progress=100, blocker="等审批",
                  subtasks=[_sub("T-900.1", True), _sub("T-900.2", True)])
        got = pg.recompute(t)
        self.assertEqual(got["status"], "blocked")
        self.assertEqual(got["progress"], 100)

    def test_blocker_placeholder_words_are_not_blockers(self):
        """「无」「没有」「无。」「暂无」「无阻塞」都不算阻塞（D-07 实测的误报源）。"""
        for word in ("无", " 无 ", "无。", "暂无", "无阻塞", "没有", "没有阻碍",
                     "N/A", "—", "None", "", None):
            with self.subTest(word=word):
                t = _task(status="doing", progress=0, blocker=word,
                          subtasks=[_sub("T-900.1", True), _sub("T-900.2", True)])
                self.assertEqual(pg.recompute(t)["status"], "done",
                                 f"「{word}」被当成了真阻塞")

    def test_real_blocker_text_still_blocks(self):
        """归一化不能把真阻塞也吃掉：带具体原因的写法必须仍然 blocked。"""
        for word in ("卡在等 review", "无网络权限无法拉镜像", "暂无回复，对方已 3 天未回"):
            with self.subTest(word=word):
                t = _task(blocker=word,
                          subtasks=[_sub("T-900.1", True), _sub("T-900.2", True)])
                self.assertEqual(pg.recompute(t)["status"], "blocked", f"「{word}」被漏判")

    def test_handwritten_progress_overridden_when_subtasks_exist(self):
        """有子任务时手写百分比被覆盖（子任务是事实源）。"""
        t = _task(status="todo", progress=90,
                  subtasks=[_sub("T-900.1", True), _sub("T-900.2", False),
                            _sub("T-900.3", False), _sub("T-900.4", False)])
        self.assertEqual(pg.recompute(t)["progress"], 25)

    def test_handwritten_kept_without_subtasks(self):
        t = _task(status="doing", progress=37)
        got = pg.recompute(t)
        self.assertEqual(got["status"], "doing")
        self.assertEqual(got["progress"], 37)

    def test_zero_subtask_count_path_is_untouched(self):
        """无子任务时连 status 都不推导（不因为 progress=100 就改写成 done）。"""
        t = _task(status="todo", progress=100)
        self.assertEqual(pg.recompute(t)["status"], "todo")

    def test_pure_function_does_not_mutate_input(self):
        t = _task(status="todo", progress=11, blocker="卡住",
                  subtasks=[_sub("T-900.1", True), _sub("T-900.2", False)])
        status_before, progress_before = t["status"], t["progress"]
        subs_before = [dict(s) for s in t["subtasks"]]
        got = pg.recompute(t)
        self.assertIsNot(got, t)
        self.assertEqual(t["status"], status_before)
        self.assertEqual(t["progress"], progress_before)
        self.assertEqual(t["subtasks"], subs_before)

    def test_doing_mark_counts_as_doing(self):
        """全未完成但有「进行中」标记 → doing，不是 todo。"""
        t = _task(status="todo", progress=0,
                  subtasks=[_sub("T-900.1", False, "进行中：写单元测试")])
        got = pg.recompute(t)
        self.assertEqual(got["status"], "doing")
        self.assertEqual(got["progress"], 0)

    def test_ratio_rounds_like_the_table(self):
        """1/3 → 33，2/3 → 67，6 个里 1 个完成 → 17（round）。"""
        cases = [([True, False, False], 33), ([True, True, False], 67),
                 ([True, False, False, False, False, False], 17)]
        for done_flags, want in cases:
            with self.subTest(flags=done_flags):
                subs = [_sub(f"T-900.{i + 1}", d) for i, d in enumerate(done_flags)]
                self.assertEqual(pg.recompute(_task(subtasks=subs))["progress"], want)


class TestInteropWithParser(unittest.TestCase):
    """与 taskfile 的互操作：解析出的任务能直接喂给 recompute，写回后 parse 得到同一结果。"""

    def test_parse_then_recompute_then_write_back_is_stable(self):
        text = (
            "## T-900 演示 · doing · 40% · proj:demo\n"
            "- [x] T-900.1 已完成\n"
            "- [ ] T-900.2 未完成\n"
            "\n**快照**：做到一半\n"
            "\n## 格式约定（以下为示例，不参与解析）\n\n```markdown\n## T-001 x\n```\n"
        )
        tasks = tf.parse(text)
        self.assertEqual(len(tasks), 1)
        recomputed = [pg.recompute(t) for t in tasks]
        self.assertEqual(recomputed[0]["status"], "doing")
        self.assertEqual(recomputed[0]["progress"], 50)   # 1/2

        written = tf.write_back(text, recomputed)
        self.assertIn("## T-900 演示 · doing · 50% · proj:demo", written)
        again = tf.parse(written)
        self.assertEqual(again[0]["progress"], 50)
        self.assertEqual(again[0]["status"], "doing")
        # 幂等：再写回一次字节不变
        self.assertEqual(tf.write_back(written, [pg.recompute(t) for t in again]), written)


if __name__ == "__main__":
    unittest.main()
