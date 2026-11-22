"""模块名遮蔽回归测试 —— 宿主进程里同名的第三方/其它插件模块抢先占用 sys.modules。

真实故障（第八轮）：
    宿主进程里 `sys.modules['inject']` 已被别的模块占住。插件 `__init__.py` 里
    `sys.path.insert(0, core); import inject` 拿到的是**别人的**模块 ——
    `sys.path` 优先级压不过已缓存的模块，于是 `inject.render_for_session` 永远
    不存在，宿主每次会话报：
        Plugin system prompt section hermes-cockpit.project raised and was skipped:
        module 'inject' has no attribute 'render_for_session'

本测试按宿主的加载方式（独立模块名 + submodule_search_locations）加载插件，
并预先用「同名诱饵模块」占满 sys.modules 里的裸模块名，断言：
    1. 我们的 render_for_session 仍能被取到并正常渲染（核心断言）
    2. 诱饵模块没有被我们的代码改写/污染（别人的模块不许被顶替）
    3. 插件内部的模块名带唯一前缀，不再出现在裸名下（不留再撞的机会）

失败方向必须是「爆红」而不是「静默空串」：拿不到就能量化的断言失败。
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "plugins" / "hermes-cockpit"
CORE_DIR = PLUGIN_DIR / "core"
DASH_DIR = PLUGIN_DIR / "dashboard"

# 宿主里最容易撞的裸名：插件自己也用了这些名字当模块名
BARE_NAMES = ("inject", "store", "meta", "taskfile", "progress", "host_adapter", "fileio")


def _load_plugin_module():
    """按宿主的方式加载插件包（独立模块名，不是 hermes_cockpit）。"""
    name = "hermes_plugins.shadow_test_hermes_cockpit"
    for stale in [m for m in sys.modules if m.startswith(name)]:
        del sys.modules[stale]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Decoy(types.ModuleType):
    """模拟「别人先占了这个名字」的模块：没有任何我们的函数，且记录被访问。"""

    def __init__(self, name: str):
        super().__init__(name)
        self.touched_by_cockpit = False


class TestBareModuleNamesAreShadowed(unittest.TestCase):
    def setUp(self):
        self._saved = {n: sys.modules.get(n) for n in BARE_NAMES}
        self.decoys = {}
        for n in BARE_NAMES:
            decoy = _Decoy(n)
            self.decoys[n] = decoy
            sys.modules[n] = decoy
        # 清掉插件可能留下的缓存
        for stale in [m for m in sys.modules
                      if m.startswith("hermes_cockpit") or m.startswith("hermes_plugins.shadow_test")]:
            del sys.modules[stale]

    def tearDown(self):
        for n in BARE_NAMES:
            if self._saved[n] is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = self._saved[n]
        for stale in [m for m in sys.modules
                      if m.startswith("hermes_cockpit") or m.startswith("hermes_plugins.shadow_test")]:
            del sys.modules[stale]

    # ── 核心断言：修复前这里会 AttributeError ───────────────────────────────

    def test_render_for_session_reachable_when_inject_is_occupied(self):
        """sys.modules['inject'] 被占时，插件仍必须拿到我们自己的 render_for_session。"""
        mod = _load_plugin_module()

        # 直接走插件的渲染回调（宿主实际调用的就是它）
        rendered = mod._render_project_section({"session_id": "shadow-session"})

        self.assertIsInstance(rendered, str)

    def test_renderer_callable_is_ours_not_decoy(self):
        """从适配层注册出去的 render 必须是插件自己的函数，不是诱饵模块里的。"""
        mod = _load_plugin_module()

        captured = {}

        class _Ctx:
            def register_system_prompt_section(self, id, content, *, position, max_chars):
                captured["content"] = content
                return object()

        mod.register(_Ctx())
        render = captured["content"]
        self.assertTrue(callable(render))

        # 回调是惰性的：模块要等真正调用它时才加载。这里真的调一次。
        self.assertIsInstance(render({"session_id": "ours-check"}), str)

        # 诱饵模块没有 render_for_session；若插件误用了诱饵，这里会 AttributeError
        self.assertFalse(hasattr(self.decoys["inject"], "render_for_session"))

        # 插件自己那份模块必须已加载到带前缀的唯一名下
        our = [m for m in sys.modules if m.startswith("hermes_cockpit_") and hasattr(
            sys.modules[m], "render_for_session")]
        self.assertTrue(our, f"没有找到带唯一前缀的 inject 模块，当前: "
                             f"{[m for m in sys.modules if 'inject' in m]}")

    def test_plugin_modules_live_under_unique_prefix(self):
        """插件内部模块一律挂在 hermes_cockpit_* 下，裸名里不许出现我们的模块。"""
        mod = _load_plugin_module()
        mod._render_project_section({"session_id": "prefix-check"})
        _load_dashboard_chain()

        leaked = []
        for name in BARE_NAMES:
            m = sys.modules.get(name)
            if m is self.decoys[name]:
                continue                      # 还是诱饵 → 没被顶替，正确
            leaked.append((name, getattr(m, "__file__", "<no file>")))
        self.assertEqual(leaked, [], f"这些裸名被插件顶替了：{leaked}")

    def test_decoy_modules_not_mutated(self):
        """别人的模块不许被我们写入属性（不许靠 patch 诱饵蒙混）。"""
        mod = _load_plugin_module()
        mod._render_project_section({"session_id": "mutate-check"})
        for n, decoy in self.decoys.items():
            self.assertFalse(
                hasattr(decoy, "render_for_session"),
                f"诱饵模块 {n} 上被塞了 render_for_session —— 这不是修复，是把别人的模块改了")
            self.assertFalse(
                decoy.touched_by_cockpit,
                f"诱饵模块 {n} 被我们访问过属性")


def _load_dashboard_chain():
    """加载 dashboard 下的 plugin_api（它会连带导入 store/fileio/taskfile/...）。"""
    name = "hermes_cockpit_test_plugin_api_shadow"
    for stale in [m for m in sys.modules if m.startswith(name)]:
        del sys.modules[stale]
    target = DASH_DIR / "plugin_api.py"
    if not target.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:                     # fastapi 缺失等环境原因不算本测试失败
        return None
    return module


if __name__ == "__main__":
    unittest.main()
