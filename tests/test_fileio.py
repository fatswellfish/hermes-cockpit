"""A4 并发写保护验收测试 —— dashboard/fileio.py。

覆盖（WBS A4 / REVIEW-r1 D-04 相邻能力）：
  · read_with_stamp 返回 (原文, mtime_ns)，换行符原样保留
  · write_if_unchanged 在 mtime 一致时写入，返回新戳
  · mtime 不一致抛 StaleWrite，且**一个字节都没写**（不静默覆盖）
  · 两线程 / 两进程同时写同一文件：恰好一个成功，另一个 StaleWrite，文件不损坏
  · 写入走临时文件 + 原子替换：失败路径不留 .tmp 残文件
  · 真实 TASKS.md 做一轮 read → 改 → 写回（副本，不动仓库文件）

跑法：python -m pytest tests/ -q
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "hermes-cockpit" / "dashboard"))
sys.path.insert(0, str(REPO / "plugins" / "hermes-cockpit" / "core"))

import fileio as fio  # noqa: E402
import taskfile as tf  # noqa: E402

TASKS_MD = REPO / "TASKS.md"


def _write_in_subprocess(path: str, text: str, expect, q) -> None:
    """子进程里做一次「检查后写」，把结果放回队列（跨进程验证，不靠同进程锁）。"""
    try:
        fio.write_if_unchanged(path, text, expect)
        q.put("ok")
    except fio.StaleWrite:
        q.put("stale")
    except Exception as e:                      # noqa: BLE001 - 任何异常都要让主进程看见
        q.put(f"error:{type(e).__name__}:{e}")


class TestReadWriteBasics(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "TASKS.md"
        self.p.write_text("原始内容\n", encoding="utf-8", newline="")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_returns_text_and_opaque_stamp(self):
        text, stamp = fio.read_with_stamp(self.p)
        self.assertEqual(text, "原始内容\n")
        self.assertIsInstance(stamp, tuple)
        self.assertEqual(len(stamp), 2)
        # 同一份未改动的文件，连续读两次令牌必须相同（否则任何写都会误判 stale）
        _, again = fio.read_with_stamp(self.p)
        self.assertEqual(again, stamp)

    def test_write_with_matching_stamp(self):
        _, stamp = fio.read_with_stamp(self.p)
        new_stamp = fio.write_if_unchanged(self.p, "新内容\n", stamp)
        self.assertEqual(self.p.read_text(encoding="utf-8"), "新内容\n")
        self.assertNotEqual(new_stamp, stamp)
        self.assertEqual(fio.read_with_stamp(self.p), ("新内容\n", new_stamp))

    def test_stamp_changes_on_every_write_even_in_same_clock_tick(self):
        """回归：Windows 上连续写的 mtime_ns 会撞在同一时间片，令牌必须仍然每次不同。

        实测 200 次连续 replace 中 70 次 st_mtime_ns 相同；只用 mtime 做戳时并发保护失效。
        """
        seen = set()
        _, stamp = fio.read_with_stamp(self.p)
        seen.add(stamp)
        for i in range(100):
            stamp = fio.write_if_unchanged(self.p, f"第{i}版\n", stamp)
            self.assertNotIn(stamp, seen, f"第 {i} 次写的令牌与之前重复")
            seen.add(stamp)

    def test_stale_stamp_raises_and_writes_nothing(self):
        _, stamp = fio.read_with_stamp(self.p)
        fio.write_if_unchanged(self.p, "别人先写的\n", stamp)
        with self.assertRaises(fio.StaleWrite) as ctx:
            fio.write_if_unchanged(self.p, "后写的人\n", stamp)
        self.assertEqual(self.p.read_text(encoding="utf-8"), "别人先写的\n",
                         "抛了 StaleWrite 却仍然覆盖了 —— 这就是静默丢内容")
        self.assertIn("拒绝覆盖", str(ctx.exception))

    def test_newline_style_preserved(self):
        """人机共写文件里 CRLF 不能被悄悄改成 LF。"""
        self.p.write_bytes("甲行\r\n乙行\r\n".encode("utf-8"))
        text, stamp = fio.read_with_stamp(self.p)
        self.assertEqual(text, "甲行\r\n乙行\r\n")
        fio.write_if_unchanged(self.p, text, stamp)
        self.assertEqual(self.p.read_bytes(), "甲行\r\n乙行\r\n".encode("utf-8"))

    def test_missing_file_raises(self):
        with self.assertRaises(fio.FileIOError):
            fio.read_with_stamp(Path(self.tmp.name) / "nope.md")
        with self.assertRaises(fio.FileIOError):
            fio.write_if_unchanged(Path(self.tmp.name) / "nope.md", "x", 0)

    def test_no_temp_files_left_behind(self):
        _, stamp = fio.read_with_stamp(self.p)
        fio.write_if_unchanged(self.p, "写一次\n", stamp)
        _, stamp2 = fio.read_with_stamp(self.p)
        with self.assertRaises(fio.StaleWrite):
            fio.write_if_unchanged(self.p, "失败路径\n", stamp)
        leftovers = [f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [], f"留下临时文件：{leftovers}")


class TestConcurrentWriters(unittest.TestCase):
    """两个写入者拿同一个戳：恰好一个成功、一个 StaleWrite，文件不损坏。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "TASKS.md"
        self.p.write_text("## T-001 初始 · doing · 0% · proj:x\n", encoding="utf-8", newline="")
        _, self.stamp = fio.read_with_stamp(self.p)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_threads_one_wins(self):
        """同一戳起跑，重复 20 轮：每轮都必须「恰好一个成功」——单轮通过说明不了问题。"""
        for round_no in range(20):
            p = Path(self.tmp.name) / f"round{round_no}.md"
            p.write_text("## T-001 初始 · doing · 0% · proj:x\n", encoding="utf-8", newline="")
            _, stamp = fio.read_with_stamp(p)

            results: list[str] = []
            lock = threading.Lock()
            barrier = threading.Barrier(2)

            def worker(name: str):
                barrier.wait()                  # 两线程同一刻起跑，最大化竞争
                try:
                    fio.write_if_unchanged(p, f"## T-001 {name} · doing · 0% · proj:x\n", stamp)
                    out = "ok"
                except fio.StaleWrite:
                    out = "stale"
                with lock:
                    results.append(out)

            ts = [threading.Thread(target=worker, args=(n,)) for n in ("甲", "乙")]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=30)

            self.assertEqual(sorted(results), ["ok", "stale"],
                             f"第 {round_no} 轮结果不符合预期：{results}")
            content = p.read_text(encoding="utf-8")
            self.assertIn(content.split()[2], ("甲", "乙"))     # 完整写到某一个版本
            self.assertTrue(content.endswith("\n"))
            self.assertEqual(len(tf.parse(content)), 1, f"文件被写坏了：{content!r}")

    def test_two_processes_one_wins(self):
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_write_in_subprocess,
                             args=(str(self.p), f"## T-001 子{i} · doing · 0% · proj:x\n",
                                   self.stamp, q))
                 for i in ("甲", "乙")]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join(timeout=60)
        results = sorted([q.get(timeout=10) for _ in procs])
        self.assertEqual(results, ["ok", "stale"], f"跨进程结果不符合预期：{results}")
        content = self.p.read_text(encoding="utf-8")
        self.assertEqual(len(tf.parse(content)), 1, f"文件被写坏了：{content!r}")

    def test_concurrent_reader_is_not_a_conflict(self):
        """回归：读侧正在打开文件时，写不得被误判成 StaleWrite。

        成因：Windows 的 os.replace 无法覆盖一个正被别人 open 着的文件，
        旧写法把这个 PermissionError 直接报成 StaleWrite。
        实测 120 轮里 58 轮「内容一模一样的写回」被判冲突 —— 那是假报警，
        视图层会拿到 409 让用户白刷新一次。

        判据：写入必须成功，且落地内容正是我们写的；读线程全程并发。
        """
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    self.p.read_text(encoding="utf-8")
                except OSError:
                    pass

        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(10):
                text, stamp = fio.read_with_stamp(self.p)
                fio.write_if_unchanged(self.p, text.replace("doing", f"doing{i}"), stamp)
        finally:
            stop.set()
            t.join(timeout=30)
        self.assertIn("doing9", self.p.read_text(encoding="utf-8"))

    def test_genuine_conflict_still_rejected_under_concurrent_reader(self):
        """负向对照：读者并发时，真冲突仍必须抛 StaleWrite（重试不能把闸门放空）。"""
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    self.p.read_text(encoding="utf-8")
                except OSError:
                    pass

        t = threading.Thread(target=reader)
        t.start()
        try:
            _, stamp = fio.read_with_stamp(self.p)
            other = "## T-001 别人写的 · doing · 0% · proj:x\n"
            self.p.write_text(other, encoding="utf-8", newline="")   # 真有人改过
            with self.assertRaises(fio.StaleWrite):
                fio.write_if_unchanged(self.p, "## T-001 我的 · doing · 0% · proj:x\n", stamp)
        finally:
            stop.set()
            t.join(timeout=30)
        self.assertEqual(self.p.read_text(encoding="utf-8"), other,
                         "重试逻辑把真冲突也放过了 —— 静默覆盖")

    def test_sequential_writes_all_succeed_with_fresh_stamps(self):
        """拿新戳连续写不冲突（并发保护不能把正常写也拦掉）。"""
        for i in range(5):
            text, stamp = fio.read_with_stamp(self.p)
            fio.write_if_unchanged(self.p, text.replace("doing", f"doing{i}"), stamp)
        self.assertIn("doing4", self.p.read_text(encoding="utf-8"))


class TestRealTasksMdRoundtrip(unittest.TestCase):
    """真实 TASKS.md 的 read → 改 → write_back → write_if_unchanged（在副本上做）。"""

    def test_edit_through_write_back(self):
        if not TASKS_MD.exists():
            self.skipTest("TASKS.md 不存在")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "TASKS.md"
            p.write_bytes(TASKS_MD.read_bytes())
            text, stamp = fio.read_with_stamp(p)
            tasks = tf.parse(text)
            tasks[0]["subtasks"][2]["done"] = True
            new_text = tf.write_back(text, tasks)
            new_stamp = fio.write_if_unchanged(p, new_text, stamp)

            back, stamp2 = fio.read_with_stamp(p)
            self.assertEqual(stamp2, new_stamp)
            self.assertEqual(tf.parse(back), tasks)
            # 文件头仍然逐字节不变
            self.assertEqual(back[:back.index("## T-")], text[:text.index("## T-")])
            # 旧戳再写一次必须被拒
            with self.assertRaises(fio.StaleWrite):
                fio.write_if_unchanged(p, text, stamp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
