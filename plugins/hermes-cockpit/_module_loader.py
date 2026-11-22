"""_module_loader — 按**文件路径**显式加载插件内部模块，模块名带唯一前缀。

## 为什么必须有这个文件（第八轮实测故障）

旧写法是 `sys.path.insert(0, core); import inject`。这在独立进程里能用，
在宿主进程里**必然出错**：宿主里 `sys.modules['inject']` 可能已被别的模块占用
（实测宿主 venv 内有 `boto3/s3/inject.py`），而

    sys.path 的优先级压不过 sys.modules 里已缓存的模块

于是 `import inject` 拿到的是**别人的**模块，我们的 `render_for_session` 根本不执行。
宿主每会话报：

    Plugin system prompt section hermes-cockpit.project raised and was skipped:
    module 'inject' has no attribute 'render_for_session'

同样的坑覆盖 7 个裸名：inject / store / meta / taskfile / progress / host_adapter / fileio。
`store` `meta` `progress` `taskfile` 在宿主 venv 里都真实存在同名文件，撞上是迟早的事。

## 规则（不要退回裸 import）

1. 插件内部模块一律经 `load()` 取，**不再 `import <裸名>`**
2. 注册进 `sys.modules` 的键必须是 `hermes_cockpit_<stem>`，全局唯一，不会撞
3. 同一路径只加载一次（幂等）——重复加载会产生两份模块状态，比撞名更难查
4. 先查 `sys.modules` 再用：同一进程里换路径（装到宿主 vs 源码仓）时**以先加载的为准**，
   这与 Python import 的语义一致，避免半新半旧两套代码同时存在
5. 加载失败**必须抛出声**（raise ImportError），不许返回空壳模块 ——
   静默失效正是这次 bug 藏了这么久的原因
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

# 前缀必须唯一到「不会与任何第三方包重名」的程度。
PREFIX = "hermes_cockpit_"

PLUGIN_ROOT = Path(__file__).resolve().parent
CORE_DIR = PLUGIN_ROOT / "core"
DASHBOARD_DIR = PLUGIN_ROOT / "dashboard"

# 模块 stem → 所在目录。集中声明，避免各处再拼路径。
_LOCATIONS = {
    "prompt_inject": CORE_DIR,
    "meta": CORE_DIR,
    "progress": CORE_DIR,
    "taskfile": CORE_DIR,
    "host_adapter": DASHBOARD_DIR,
    "store": DASHBOARD_DIR,
    "fileio": DASHBOARD_DIR,
}


def module_name(stem: str) -> str:
    """裸 stem → 唯一模块名。"""
    return PREFIX + stem


def _path_of(stem: str) -> Path:
    directory = _LOCATIONS.get(stem)
    if directory is None:
        # 没登记的 stem 按 core/ 再 dashboard/ 顺序找，找到就用；找不到抛出
        for d in (CORE_DIR, DASHBOARD_DIR):
            if (d / f"{stem}.py").is_file():
                return d / f"{stem}.py"
        raise ImportError(
            f"hermes-cockpit: 找不到内部模块 {stem!r}（已查 {CORE_DIR}、{DASHBOARD_DIR}）")
    return directory / f"{stem}.py"


def load(stem: str) -> ModuleType:
    """按文件路径加载插件内部模块，返回带唯一前缀的模块对象。"""
    name = module_name(stem)
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = _path_of(stem)
    if not path.is_file():
        raise ImportError(f"hermes-cockpit: 内部模块文件不存在：{path}")

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:          # pragma: no cover - 极端情况
        raise ImportError(f"hermes-cockpit: 无法为 {path} 构造加载 spec")

    module = importlib.util.module_from_spec(spec)
    # 进 sys.modules 之后再 exec：模块内的相互引用才能拿到同一个对象
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 半初始化模块留在 sys.modules 会让后续 import 拿到空壳 —— 当场摘掉再抛
        sys.modules.pop(name, None)
        raise
    return module


# 常用别名，调用方写 `ml.inject` 比 `ml.load("prompt_inject")` 少打错字
def prompt_inject() -> ModuleType:
    return load("prompt_inject")


def meta() -> ModuleType:
    return load("meta")


def progress() -> ModuleType:
    return load("progress")


def taskfile() -> ModuleType:
    return load("taskfile")


def host_adapter() -> ModuleType:
    return load("host_adapter")


def store() -> ModuleType:
    return load("store")


def fileio() -> ModuleType:
    return load("fileio")
