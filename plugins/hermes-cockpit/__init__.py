"""hermes-cockpit — 项目中枢插件。

Hermes 插件加载器要求插件目录有 __init__.py，缺了会报
"No __init__.py in <dir>" 并跳过整个插件（todo-panel 实测踩过这个坑）。

本文件只做两件事：包标记 + 宿主入口函数 register(ctx)。

**register(ctx) 里不许写业务逻辑。** 它只负责把宿主的 ctx 转交给适配层：
宿主只在 register(ctx) 成功执行后才把插件记为 enabled；没有这个函数，
hermes_cli/plugins_loader.py:615 会报 "no register() function" 并整条不加载
（实测：hermes plugins list 显示 not enabled → router 不挂载、hook 一条不跑）。

## 模块加载方式（第八轮修复，勿退回裸 import）

**禁止** `sys.path.insert(0, core); import inject`。宿主进程里 `sys.modules['inject']`
可能已被别的模块占住（实测宿主 venv 有 `boto3/s3/inject.py`），而 sys.path 的优先级
压不过已缓存的模块 ⇒ 拿到别人的模块 ⇒ `render_for_session` 不存在 ⇒
宿主每会话报 "module 'inject' has no attribute 'render_for_session'" 并跳过整个 section。

内部模块一律经 `_module_loader.load()` 按文件路径加载，注册名带 `hermes_cockpit_` 前缀。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent
_PACKAGE_NAME = __name__


def _module_loader():
    """取模块加载器。

    本包在宿主里可能是以独立模块名（如 `hermes_plugins.<name>`）加载的，
    包内相对导入不一定可用 —— 旧代码正是因此才去补 sys.path。这里按文件路径显式加载
    `_module_loader.py`；失败就让异常冒出去，静默失效不可接受。
    """
    name = f"{_PACKAGE_NAME}._module_loader"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing

    spec_rel = _PLUGIN_ROOT / "_module_loader.py"
    spec = importlib.util.spec_from_file_location(name, spec_rel)
    if spec is None or spec.loader is None:      # pragma: no cover - 极端情况
        raise ImportError(f"hermes-cockpit: 无法加载模块加载器 {spec_rel}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)              # 不留半初始化空壳
        raise
    return module


def _adapter():
    """导入适配层（host_adapter）。"""
    return _module_loader().host_adapter()


def _render_project_section(session_info):
    """系统提示段落的回调本体（E2）。薄壳：取数据 → 交给 core/prompt_inject 判定。

    这里只做装配（数据从适配层来、判定在 core），不含任何判定规则。
    """
    ml = _module_loader()
    inject = ml.prompt_inject()
    ha = ml.host_adapter()
    store = ml.store()
    meta_mod = ml.meta()

    # 数据目录必须走 store.project_root()，**不许再写死 `_PLUGIN_ROOT.parent.parent`**：
    # 那个写法只在源码仓里指对，装到宿主后指到 AppData\Local\hermes\，META.yaml 读不到
    # → 注入永远静默为空（与 GAP-02 同一个成因）。project_root 会读 plugin.yaml 的
    # data_dir、再向上逐级找 TASKS.md/META.yaml。
    def load_meta():
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

