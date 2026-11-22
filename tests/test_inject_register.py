"""E2 注册面单测 —— 第六轮裁定 §四 要求 1/2：section id 固定、register 只转调。

用桩 ctx 跑真实的 `plugins/project-hub/__init__.py::register`，不碰真宿主：
    · id 必须是 `hermes-cockpit.project`（重复注册会 ValueError，所以固定 id 是硬约束）
    · register 里不得出现业务逻辑 ⇒ 只能发生一次「注册」调用，且参数由适配层给
    · 宿主抛错（例如重复注册）必须冒泡 —— 吞掉会变成「装了但没注入」的静默失败
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "plugins" / "project-hub"


def _load_plugin_module():
    """按宿主的加载方式（独立模块名 + submodule_search_locations）导入插件包。"""
    name = "hermes_plugins.test_hermes_cockpit"
    for stale in [m for m in sys.modules if m.startswith(name)]:
        del sys.modules[stale]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _StubCtx:
    def __init__(self):
        self.calls: list[dict] = []

    def register_system_prompt_section(self, id, content, *, position, max_chars):
        self.calls.append({"id": id, "content": content,
                           "position": position, "max_chars": max_chars})
        return object()


class _StubCtxRejecting(_StubCtx):
    """模拟宿主对重复 id 的处理：抛 ValueError。"""

    def register_system_prompt_section(self, id, content, *, position, max_chars):
        super().register_system_prompt_section(id, content, position=position, max_chars=max_chars)
        raise ValueError(f"system prompt section {id!r} is already registered")


class TestRegisterEntry(unittest.TestCase):
    def setUp(self):
        self.mod = _load_plugin_module()

    def test_register_calls_host_exactly_once(self):
        ctx = _StubCtx()
        self.mod.register(ctx)
        self.assertEqual(len(ctx.calls), 1)

    def test_section_id_is_fixed(self):
        ctx = _StubCtx()
        self.mod.register(ctx)
        self.assertEqual(ctx.calls[0]["id"], "hermes-cockpit.project")

    def test_two_registrations_use_the_same_id(self):
        """id 固定 = 两次加载不会生成两个不同 id（宿主里重复 id 直接 ValueError）。"""
        first, second = _StubCtx(), _StubCtx()
        self.mod.register(first)
        self.mod.register(second)
        self.assertEqual(first.calls[0]["id"], second.calls[0]["id"])

    def test_content_is_callable_not_a_prebaked_string(self):
        """必须是 callable：会话信息只有在渲染那一刻才拿到。"""
        ctx = _StubCtx()
        self.mod.register(ctx)
        self.assertTrue(callable(ctx.calls[0]["content"]))

    def test_position_and_budget(self):
        ctx = _StubCtx()
        self.mod.register(ctx)
        self.assertEqual(ctx.calls[0]["position"], "after_memory")
        self.assertEqual(ctx.calls[0]["max_chars"], 4000)

    def test_host_error_propagates(self):
        """注册失败不许被吞：静默失败等于「装了但没注入」。"""
        with self.assertRaises(ValueError):
            self.mod.register(_StubCtxRejecting())

    def test_renderer_never_raises_on_garbage_info(self):
        """回调本身必须 fail-open：给垃圾 session-info 也只返回字符串。"""
        ctx = _StubCtx()
        self.mod.register(ctx)
        render = ctx.calls[0]["content"]
        for info in (None, {}, {"session_id": ""}, {"session_id": "不存在的会话"}):
            with self.subTest(info=info):
                self.assertIsInstance(render(info), str)


if __name__ == "__main__":
    unittest.main()
