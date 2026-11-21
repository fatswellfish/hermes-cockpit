"""A2 任务文件解析器验收测试 —— core/taskfile.py。

覆盖（WBS A2）：
  · 往返：parse(serialize(parse(x))) 与 parse(x) 逐字段相等
  · 「格式约定」之后的内容不参与解析（末尾示例代码块不被吞成任务）
  · 字段级：id/title/status/progress/project/subtasks/due/snapshot/blocker/updated/session
  · 真实 TASKS.md 的解析（文件存在时）
跑法：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "hermes-cockpit" / "core"))

import taskfile as tf  # noqa: E402

TASKS_MD = REPO / "TASKS.md"

SPEC_EXAMPLE = """# TASKS

## T-001 标题甲 · doing · 40% · proj:mms
- [x] T-001.1 子任务已完成
- [ ] T-001.2 子任务未完成
> 更新：2026-11-21 22:30
**截止**：2026-11-30
**快照**：现在做到哪了
**阻塞**：卡在哪
"""


class TestFormatFields(unittest.TestCase):
    """按 WBS「格式约定」逐字段验收。"""

    def setUp(self):
        self.tasks = tf.parse(SPEC_EXAMPLE)

    def test_single_task_parsed(self):
        self.assertEqual(len(self.tasks), 1)
        t = self.tasks[0]
        self.assertEqual(t["id"], "T-001")
        self.assertEqual(t["title"], "标题甲")
        self.assertEqual(t["status"], "doing")
        self.assertEqual(t["progress"], 40)
        self.assertEqual(t["project"], "mms")
        self.assertEqual(t["updated"], "2026-11-21 22:30")
        self.assertEqual(t["due"], "2026-11-30")
        self.assertEqual(t["snapshot"], "现在做到哪了")
        self.assertEqual(t["blocker"], "卡在哪")

    def test_subtasks(self):
        subs = self.tasks[0]["subtasks"]
        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[0], {"id": "T-001.1", "title": "子任务已完成", "done": True})
        self.assertEqual(subs[1], {"id": "T-001.2", "title": "子任务未完成", "done": False})

    def test_all_contract_fields_present(self):
        t = self.tasks[0]
        for field in tf.FIELDS:
            self.assertIn(field, t, f"缺字段 {field}")
        self.assertIsNone(t["session"])          # 示例里没有会话行

    def test_session_field(self):
        txt = SPEC_EXAMPLE.replace("**阻塞**：卡在哪",
                                  "**阻塞**：卡在哪\n**会话**：local-abc123")
        t = tf.parse(txt)[0]
        self.assertEqual(t["session"], "local-abc123")

    def test_optional_segments_absent(self):
        """没有 · status · % · proj 时用默认值，不报错。"""
        t = tf.parse("## T-009 只有一个标题\n")[0]
        self.assertEqual(t["id"], "T-009")
        self.assertEqual(t["title"], "只有一个标题")
        self.assertEqual(t["status"], "todo")
        self.assertEqual(t["progress"], 0)
        self.assertIsNone(t["project"])


class TestStopAtFormatSection(unittest.TestCase):
    """硬规则：「格式约定」之后的内容不参与解析。"""

    def test_example_code_block_is_not_swallowed(self):
        """末尾示例里有 `## T-001 标题 · doing · 40% · proj:项目slug`，
        若被当成真任务就会出现重复 id —— 必须整体忽略。"""
        text = SPEC_EXAMPLE + """
## 格式约定（以下为示例，不参与解析）

```markdown
## T-900 示例任务 · doing · 40% · proj:项目slug
- [x] T-900.1 子任务
- [ ] T-900.2 子任务
**截止**：2026-11-30
**快照**：当前进展
**阻塞**：卡点
```

状态取值：`todo` / `doing` / `done` / `blocked`
"""
        tasks = tf.parse(text)
        self.assertEqual([t["id"] for t in tasks], ["T-001"])
        self.assertEqual(len(tasks[0]["subtasks"]), 2)
        # 示例块里的 T-900.1/T-900.2 一条都不能混进来
        sub_ids = [s["id"] for s in tasks[0]["subtasks"]]
        self.assertNotIn("T-900.1", sub_ids)
        self.assertEqual(tasks[0]["blocker"], "卡在哪")   # 真任务自己的字段

    def test_real_format_section_example_not_parsed(self):
        """真实 TASKS.md 的「格式约定」里写的是 T-001（与真任务同 id）——
        若被解析会抛 id 重复，或把示例子任务混进真任务。"""
        if not TASKS_MD.exists():
            self.skipTest("TASKS.md 不存在")
        text = TASKS_MD.read_text(encoding="utf-8")
        tasks = tf.parse(text)          # 不抛异常即证明示例块没被当成任务
        ids = [t["id"] for t in tasks]
        self.assertEqual(ids, ["T-001", "T-002"])
        # 示例块里只有两条子任务；真 T-001 有 12 条
        self.assertEqual(len(tasks[0]["subtasks"]), 12)
        self.assertEqual(tasks[0]["status"], "doing")
        self.assertEqual(tasks[0]["project"], "hermes-cockpit")
        self.assertTrue(tasks[0]["subtasks"][0]["done"])     # T-001.1 已勾
        self.assertFalse(tasks[0]["subtasks"][1]["done"])    # T-001.2 未勾

    def test_fenced_block_before_format_section_also_ignored(self):
        text = """## T-010 真任务 · todo · 0% · proj:x
```
## T-999 代码块里的假任务 · doing · 50%
```
- [ ] T-010.1 真的子任务
"""
        tasks = tf.parse(text)
        self.assertEqual([t["id"] for t in tasks], ["T-010"])
        self.assertEqual(len(tasks[0]["subtasks"]), 1)


class TestMultiLineAndEdgeCases(unittest.TestCase):

    def test_multiline_snapshot_bare_continuation(self):
        """实测 TASKS.md 的 T-002 快照折成两行，第二行没有任何标记。"""
        text = """## T-001 A · doing · 0% · proj:x
**快照**：第一行
第二行是续写
**阻塞**：无
"""
        t = tf.parse(text)[0]
        self.assertEqual(t["snapshot"], "第一行\n第二行是续写")
        self.assertEqual(t["blocker"], "无")

    def test_auto_numbered_subtasks(self):
        text = """## T-005 A · todo · 0% · proj:x
- [ ] 没有写 id 的子任务
- [x] 第二条
"""
        subs = tf.parse(text)[0]["subtasks"]
        self.assertEqual([s["id"] for s in subs], ["T-005.1", "T-005.2"])
        self.assertEqual(subs[1]["done"], True)

    def test_non_task_h2_ends_block(self):
        text = """## T-001 A · todo · 0% · proj:x
- [ ] T-001.1 子任务

## 模块 A · 数据地基

- [ ] 这一行不属于任何任务
"""
        tasks = tf.parse(text)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(tasks[0]["subtasks"]), 1)

    def test_duplicate_id_raises(self):
        text = "## T-001 A · todo · 0% · proj:x\n## T-001 B · todo · 0% · proj:y\n"
        with self.assertRaises(tf.TaskFileError):
            tf.parse(text)

    def test_empty_text(self):
        self.assertEqual(tf.parse(""), [])


class TestRoundTrip(unittest.TestCase):
    """serialize(parse(x)) 再 parse 必须逐字段相等。"""

    def _assert_roundtrip(self, text: str):
        first = tf.parse(text)
        serialized = tf.serialize(first)
        second = tf.parse(serialized)
        self.assertEqual(second, first,
                         f"往返不一致\n序列化后：\n{serialized}")

    def test_roundtrip_spec_example(self):
        self._assert_roundtrip(SPEC_EXAMPLE)

    def test_roundtrip_with_all_fields(self):
        text = """## T-007 全字段 · blocked · 55% · proj:software-pipeline
- [x] T-007.1 已完成
- [ ] T-007.2 未完成
- [x] T-007.3 也完成了
> 更新：2026-11-21 22:30
**截止**：2026-12-01
**快照**：多行快照
第二行
**阻塞**：等审批
**会话**：local-deadbeef
"""
        self._assert_roundtrip(text)

    def test_roundtrip_no_optional_fields(self):
        self._assert_roundtrip("## T-002 只有标题\n- [ ] T-002.1 x\n")

    def test_roundtrip_real_tasks_md(self):
        if not TASKS_MD.exists():
            self.skipTest("TASKS.md 不存在")
        self._assert_roundtrip(TASKS_MD.read_text(encoding="utf-8"))

    def test_serialize_output_keeps_format_section(self):
        """序列化产物必须自带「格式约定」，否则示例块会被下一次 parse 吞掉。"""
        out = tf.serialize(tf.parse(SPEC_EXAMPLE))
        self.assertIn("格式约定", out)
        self.assertEqual(len(tf.parse(out)), 1)

    def test_parent_progress_not_recomputed(self):
        """解析器不改父进度；WBS 要求由 B1 写操作重算（3 子任务勾 2 → 67%）。"""
        text = """## T-001 A · doing · 10% · proj:x
- [x] T-001.1 a
- [x] T-001.2 b
- [ ] T-001.3 c
"""
        t = tf.parse(text)[0]
        self.assertEqual(t["progress"], 10)      # 原样保留


if __name__ == "__main__":
    unittest.main(verbosity=2)
