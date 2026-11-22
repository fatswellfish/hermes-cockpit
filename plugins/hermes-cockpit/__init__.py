"""hermes-cockpit — 项目中枢插件。

Hermes 插件加载器要求插件目录有 __init__.py，缺了会报
"No __init__.py in <dir>" 并跳过整个插件（todo-panel 实测踩过这个坑）。

本文件只做两件事：包标记 + 宿主入口函数 register(ctx)。

**register(ctx) 里不许写业务逻辑。** 它只负责把宿主的 ctx 转交给适配层：
宿主只在 register(ctx) 成功执行后才把插件记为 enabled；没有这个函数，
hermes_cli/plugins_loader.py:615 会报 "no register() function" 并整条不加载
（实测：hermes plugins list 显示 not enabled → router 不挂载、hook 一条不跑）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent


def _adapter():
    """导入适配层。dashboard/ 不在包路径上，且宿主用独立模块名加载本插件，所以补 sys.path。"""
    dash = _PLUGIN_ROOT / "dashboard"
    if str(dash) not in sys.path:
        sys.path.insert(0, str(dash))
    import host_adapter  # noqa: E402 - 运行期导入，插件未启用时不留副作用
    return host_adapter


def _render_project_section(session_info):
    """系统提示段落的回调本体（E2）。薄壳：取数据 → 交给 core/inject 判定。

    这里只做装配（数据从适配层来、判定在 core），不含任何判定规则。
    """
    core = _PLUGIN_ROOT / "core"
    if str(core) not in sys.path:
        sys.path.insert(0, str(core))
    import inject  # noqa: E402

    ha = _adapter()
    import store  # noqa: E402 - dashboard 目录已在 sys.path

    # 数据目录必须走 store.project_root()，**不许再写死 `_PLUGIN_ROOT.parent.parent`**：
    # 那个写法只在源码仓里指对，装到宿主后指到 AppData\Local\hermes\，META.yaml 读不到
    # → 注入永远静默为空（与 GAP-02 同一个成因）。project_root 会读 plugin.yaml 的
    # data_dir、再向上逐级找 TASKS.md/META.yaml。
    def load_meta():
        import meta as meta_mod  # noqa: E402
        path = store.meta_path()
        return meta_mod.load(path) if path.is_file() else {}

    return inject.render_for_session(
        session_info,
        session_cwd=ha.session_cwd,
        read_projects=ha.read_projects,
        load_meta=load_meta,
    )


def register(ctx) -> None:
    """宿主入口：只转调适配层，不实现业务逻辑。"""
    _adapter().register_system_prompt_section(ctx, _render_project_section)

