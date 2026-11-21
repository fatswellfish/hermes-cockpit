"""A2 第二轮回归测试 —— D-01 / D-02 / D-03 / D-04（REVIEW-r1.md）。

四条缺陷各自锁死的行为：
  · D-01 serialize() 拆两层：serialize_tasks（只出任务块）+ write_back（只换任务区）。
         用**仓库里真实的 TASKS.md** 走一遍写回，断言文件头与前言逐字节不变、
         写回后 parse 结果逐字段相等。
  · D-02 `_parse_heading` 里不再有自问自答的疑问式注释（有人会照着它猜行为）。
  · D-03 子任务自动编号不再与显式 id 撞号（取该父任务已有最大序号 +1）。
  · D-04 连续 write_back 3 次，字节数与「格式约定」节数都不变（幂等）。

跑法：python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "hermes-cockpit" / "core"))

import taskfile as tf  # noqa: E402

TASKS_MD = REPO / "TASKS.md"
FORMAT_HEADING = "## 格式约定"


def _header_of(text: str) -> str:
    """文件头与前言 = 第一个任务标题之前的全部内容（逐字节）。"""
    idx = text.find("## T-")
    return text[:idx] if idx >= 0 else text


class TestD01WriteBackKeepsHeader(unittest.TestCase):
    """D-01：写回不得丢弃 TASKS.md 的文件头与说明文字。"""

    def setUp(self):
        if not TASKS_MD.exists():
            self.skipTest("TASKS.md 不存在")
        self.orig = TASKS_MD.read_text(encoding="utf-8")

    def test_serialize_tasks_has_no_header_and_no_format_section(self):
        """serialize_tasks 只出任务块：不含文件头标题，也不含「格式约定」节。"""
        body = tf.serialize_tasks(tf.parse(self.orig))
        self.assertTrue(body.startswith("## T-001"))
        self.assertNotIn("# TASKS —", body)
        self.assertNotIn(FORMAT_HEADING, body)
        self.assertNotIn("人机共写", body)

    def test_write_back_keeps_header_bytes_identical(self):
        """真实 TASKS.md 走一遍写回：文件头与前言逐字节不变。"""
        out = tf.write_back(self.orig, tf.parse(self.orig))
        self.assertEqual(_header_of(out), _header_of(self.orig),
                         "文件头/前言被改动了 —— 这是人机共写文件，属于静默抹掉人的内容")
        # 人写的三行说明原样还在
        for line in ("# TASKS — Hermes Cockpit 任务事实源",
                     "> 人机共写。格式见文末「格式约定」",
                     "> 本项目自身也用这个文件演示自己的格式（自举）。"):
            self.assertIn(line, out)

    def test_write_back_parse_roundtrip_field_equal(self):
        """写回后 parse 结果与写回前逐字段相等。"""
        before = tf.parse(self.orig)
        after = tf.parse(tf.write_back(self.orig, before))
        self.assertEqual(after, before)

    def test_write_back_keeps_format_section_verbatim(self):
        """「格式约定」这一节及之后原样保留。"""
        out = tf.write_back(self.orig, tf.parse(self.orig))
        tail_orig = self.orig[self.orig.index(FORMAT_HEADING):]
        tail_out = out[out.index(FORMAT_HEADING):]
        self.assertEqual(tail_out, tail_orig)

    def test_write_back_applies_task_edit(self):
        """写回真的改了任务内容（不是把原文原样吐回来）。"""
        tasks = tf.parse(self.orig)
        tasks[0]["subtasks"][1]["done"] = True
        tasks[0]["blocker"] = "测试写入的阻塞"
        out = tf.write_back(self.orig, tasks)
        got = {t["id"]: t for t in tf.parse(out)}
        self.assertTrue(got["T-001"]["subtasks"][1]["done"])
        self.assertEqual(got["T-001"]["blocker"], "测试写入的阻塞")

    def test_write_back_adds_block_to_taskless_file(self):
        """还没有任务的文件：任务块插到「格式约定」之前，文件头照旧。"""
        text = "# TASKS\n\n> 说明文字\n\n" + FORMAT_HEADING + "\n\n示例\n"
        out = tf.write_back(text, tf.parse("## T-009 新任务 · todo · 0% · proj:x\n"))
        self.assertTrue(out.startswith("# TASKS\n\n> 说明文字\n"))
        self.assertIn("## T-009 新任务", out)
        self.assertLess(out.index("## T-009"), out.index(FORMAT_HEADING))
        self.assertEqual(len(tf.parse(out)), 1)


class TestD02NoQuestionComment(unittest.TestCase):
    """D-02：注释与行为一致，不留自问自答的疑问句。"""

    def test_source_has_no_question_comment_in_parse_heading(self):
        src = (REPO / "plugins" / "hermes-cockpit" / "core" / "taskfile.py").read_text(
            encoding="utf-8")
        start = src.index("def _parse_heading")
        body = src[start:src.index("def _parse_subtask_id")]
        self.assertNotIn("？—— 不", body)
        self.assertNotIn("？", body, f"_parse_heading 里仍有疑问式注释：\n{body}")

    def test_extra_segment_joins_title_as_documented(self):
        """行为与注释一致：多出来的段并入标题尾部，且能往返。"""
        t = tf.parse("## T-003 标题 · doing · 30% · 额外一段 · proj:x\n")[0]
        self.assertEqual(t["title"], "标题 · 额外一段")
        self.assertEqual(t["status"], "doing")
        self.assertEqual(t["project"], "x")
        self.assertEqual(tf.parse(tf.serialize_tasks([t])), [t])


class TestD03SubtaskAutoNumbering(unittest.TestCase):
    """D-03：无显式 id 的子任务不得与已有显式 id 撞号。"""

    def test_explicit_id_then_auto_skips_past_it(self):
        """显式 T-001.1 已占 → 无 id 行取 2；显式 T-001.3 已占 → 下一个无 id 行取 4。"""
        text = """## T-001 A · todo · 0% · proj:x
- [ ] T-001.1 显式一号
- [ ] 没有 id 的
- [ ] T-001.3 显式三号
- [ ] 又一个没有 id 的
"""
        subs = tf.parse(text)[0]["subtasks"]
        self.assertEqual([s["id"] for s in subs], ["T-001.1", "T-001.2", "T-001.3", "T-001.4"])

    def test_auto_numbering_never_reuses_explicit_id(self):
        """显式 id 在自动行之前出现时，自动编号必须避开它（D-03 原始撞号场景）。"""
        text = """## T-001 A · todo · 0% · proj:x
- [ ] T-001.3 先写的显式三号
- [ ] 没有 id 的甲
- [ ] 没有 id 的乙
"""
        ids = [s["id"] for s in tf.parse(text)[0]["subtasks"]]
        self.assertEqual(ids, ["T-001.3", "T-001.4", "T-001.5"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_duplicate_ids_ever(self):
        text = """## T-002 A · todo · 0% · proj:x
- [ ] 无 id 甲
- [ ] T-002.9 显式九号
- [ ] 无 id 乙
"""
        ids = [s["id"] for s in tf.parse(text)[0]["subtasks"]]
        self.assertEqual(ids, ["T-002.1", "T-002.9", "T-002.10"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_numbering_is_stable_across_roundtrip(self):
        """自动编号的产物再解析一次，id 不变（否则每轮写回都会改文件）。"""
        text = "## T-004 A · todo · 0% · proj:x\n- [ ] T-004.2 显式\n- [ ] 无 id\n"
        first = tf.parse(text)
        second = tf.parse(tf.serialize_tasks(first))
        self.assertEqual(second, first)
        self.assertEqual([s["id"] for s in second[0]["subtasks"]], ["T-004.2", "T-004.3"])

    def test_real_tasks_md_ids_unchanged(self):
        """真实 TASKS.md 里子任务 id 本来就齐全，编号不变。"""
        if not TASKS_MD.exists():
            self.skipTest("TASKS.md 不存在")
        tasks = tf.parse(TASKS_MD.read_text(encoding="utf-8"))
        ids = [s["id"] for s in tasks[0]["subtasks"]]
        self.assertEqual(ids, [f"T-001.{n}" for n in range(1, 13)])


class TestD04WriteBackIdempotent(unittest.TestCase):
    """D-04：连续写回 3 次，文件不膨胀、「格式约定」不重复。"""

    def test_real_tasks_md_three_writes_byte_stable(self):
        if not TASKS_MD.exists():
            self.skipTest("TASKS.md 不存在")
        text = TASKS_MD.read_text(encoding="utf-8")
        tasks = tf.parse(text)
        once = tf.write_back(text, tasks)
        twice = tf.write_back(once, tf.parse(once))
        thrice = tf.write_back(twice, tf.parse(twice))
        self.assertEqual(len(twice.encode("utf-8")), len(once.encode("utf-8")))
        self.assertEqual(len(thrice.encode("utf-8")), len(once.encode("utf-8")))
        self.assertEqual(thrice, once)
        self.assertEqual(once.count(FORMAT_HEADING), 1)
        self.assertEqual(thrice.count(FORMAT_HEADING), 1)

    def test_synthetic_doc_three_writes_byte_stable(self):
        text = "# TASKS\n\n> 说明\n\n## T-001 A · doing · 10% · proj:x\n- [ ] T-001.1 a\n\n## 格式约定\n\n示例\n"
        tasks = tf.parse(text)
        a = tf.write_back(text, tasks)
        b = tf.write_back(a, tf.parse(a))
        c = tf.write_back(b, tf.parse(b))
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        self.assertEqual(c.count(FORMAT_HEADING), 1)

    def test_serialize_full_document_has_single_format_section(self):
        """serialize()（从零生成整份文档）也只带一节「格式约定」。"""
        out = tf.serialize(tf.parse("## T-001 A · todo · 0% · proj:x\n"))
        self.assertEqual(out.count(FORMAT_HEADING), 1)
        self.assertEqual(tf.parse(tf.serialize(tf.parse(out))), tf.parse(out))


class TestD05SeparatorsAndProsePreserved(unittest.TestCase):
    """D-05：任务之间的分隔线 `---` 与块内自由正文，写回时原样保留。"""

    TEXT = """# TASKS

> 人手写的前言

---

## T-001 甲 · doing · 10% · proj:x

- [ ] T-001.1 a

这里是人手写的一句自由正文（不属于任何字段）
> 更新：2026-11-21 22:00

**快照**：第一行
第二行续写

**阻塞**：无

---

## T-002 乙 · todo · 0% · proj:y
- [ ] T-002.1 b

---

## 格式约定

示例
"""

    def test_separators_preserved(self):
        out = tf.write_back(self.TEXT, tf.parse(self.TEXT))
        self.assertEqual(sum(1 for l in out.splitlines() if l.strip() == "---"), 3)

    def test_free_prose_preserved(self):
        out = tf.write_back(self.TEXT, tf.parse(self.TEXT))
        self.assertIn("这里是人手写的一句自由正文（不属于任何字段）", out)

    def test_written_back_unchanged_when_nothing_edited(self):
        """没改任何任务时，除了块内空行归一，语义内容一字不差。"""
        out = tf.write_back(self.TEXT, tf.parse(self.TEXT))
        self.assertEqual(tf.parse(out), tf.parse(self.TEXT))
        self.assertTrue(out.startswith("# TASKS\n\n> 人手写的前言\n\n---\n\n## T-001 甲"))

    def test_new_task_appended_inside_task_area(self):
        tasks = tf.parse(self.TEXT)
        tasks.append(tf.parse("## T-009 新任务 · todo · 0% · proj:z\n- [ ] T-009.1 c\n")[0])
        out = tf.write_back(self.TEXT, tasks)
        self.assertLess(out.index("## T-009"), out.index("## 格式约定"),
                        "新任务必须落在任务区，不能跑到格式约定之后")
        self.assertEqual([t["id"] for t in tf.parse(out)], ["T-001", "T-002", "T-009"])

    def test_removed_task_disappears(self):
        tasks = [t for t in tf.parse(self.TEXT) if t["id"] != "T-002"]
        out = tf.write_back(self.TEXT, tasks)
        self.assertNotIn("## T-002", out)
        self.assertIn("## T-001", out)


class TestD06MultilineFieldsKeepWrap(unittest.TestCase):
    """D-06：折行的「快照/阻塞」写回时保持原折行，不合并成一行。"""

    def test_real_tasks_md_snapshot_wrap_kept(self):
        if not TASKS_MD.exists():
            self.skipTest("TASKS.md 不存在")
        orig = TASKS_MD.read_text(encoding="utf-8")
        out = tf.write_back(orig, tf.parse(orig))
        for needle in ("`10.0.1.175:8005`（公司网段，000 不通）。真端点",
                       "当前 bot 靠 fallback 跑在云端 deepseek 上。"):
            self.assertIn(needle, orig)
            self.assertIn(needle, out, "折行被合并了 —— 人手写的排版被改掉")

    def test_field_value_edited_conservatively(self):
        """改了快照的值：只重写这一段，排版结构仍是「字段行 + 续行」。"""
        orig = "## T-001 A · doing · 0% · proj:x\n**快照**：旧第一行\n旧第二行\n**阻塞**：无\n"
        tasks = tf.parse(orig)
        tasks[0]["snapshot"] = "新第一行\n新第二行"
        out = tf.write_back(orig, tasks)
        self.assertIn("**快照**：新第一行\n新第二行\n", out)
        self.assertEqual(tf.parse(out)[0]["snapshot"], "新第一行\n新第二行")


class TestQ08DuplicateSubtaskId(unittest.TestCase):
    """Q-08：同一父任务内子任务 id 重复必须抛 TaskFileError。"""

    def test_duplicate_subtask_id_raises(self):
        text = "## T-001 A · todo · 0% · proj:x\n- [ ] T-001.1 a\n- [x] T-001.1 b\n"
        with self.assertRaises(tf.TaskFileError):
            tf.parse(text)

    def test_same_id_under_different_parents_is_fine(self):
        text = ("## T-001 A · todo · 0% · proj:x\n- [ ] T-001.1 a\n"
                "## T-002 B · todo · 0% · proj:x\n- [ ] T-002.1 a\n")
        self.assertEqual(len(tf.parse(text)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
