"""hermes-cockpit — 项目中枢插件。

Hermes 插件加载器要求插件目录有 __init__.py，缺了会报
"No __init__.py in <dir>" 并跳过整个插件（todo-panel 实测踩过这个坑）。

本文件只做包标记，不承载逻辑：所有实现分别在
  dashboard/  — 后端（路由、适配层、数据变换）
  core/       — 与宿主无关的核心算法
"""
