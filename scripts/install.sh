#!/usr/bin/env bash
# Hermes Cockpit 安装脚本
#
# 为什么需要脚本而不是手 cp：装到宿主后有一处**必须改的值**（data_dir），
# 改漏了插件能开、界面能出，但任务是空的且只在 warnings 里提示一句 —— 很难发现。
#
# 用法：
#   bash scripts/install.sh              # 装到默认宿主
#   bash scripts/install.sh --dry-run    # 只看会做什么

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# git-bash 的 pwd 给的是 /c/dev/... 这种 MSYS 路径，Windows 上的 Python 不认，
# 写进 data_dir 会导致 store 找不到 TASKS.md（tasks 恒为 0）。转成原生 C:/dev/...。
if command -v cygpath >/dev/null 2>&1; then
  REPO_NATIVE="$(cygpath -m "$REPO")"
else
  REPO_NATIVE="$REPO"
fi
HOST_HOME="${HERMES_HOME:-$HOME/AppData/Local/hermes}"
PLUG_SRC="$REPO/plugins/hermes-cockpit"
PLUG_DST="$HOST_HOME/plugins/hermes-cockpit"
DESK_SRC="$REPO/desktop-plugins/hermes-cockpit"
DESK_DST="$HOST_HOME/desktop-plugins/hermes-cockpit"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '%s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then say "  [dry] $*"; else eval "$@"; fi; }

say "仓库：$REPO"
say "宿主：$HOST_HOME"
say ""

# 1) 后端插件（只拷代码，不拷 TASKS.md / META.yaml —— 数据留在仓库里，只有一份）
say "① 同步后端插件"
run "mkdir -p '$PLUG_DST'"
run "cp -r '$PLUG_SRC'/dashboard '$PLUG_DST'/"
run "cp -r '$PLUG_SRC'/core '$PLUG_DST'/"
run "cp '$PLUG_SRC/__init__.py' '$PLUG_DST'/"
# _module_loader.py 是包内模块加载器，__init__.py 与 dashboard/plugin_api.py 都靠它；
# 漏拷会让插件在 import 阶段直接 FileNotFoundError（实测：宿主缺此文件，整个插件加载失败）。
run "cp '$PLUG_SRC/_module_loader.py' '$PLUG_DST'/"
run "cp '$PLUG_SRC/plugin.yaml' '$PLUG_DST'/"
run "cp '$PLUG_SRC/plugin_api.py' '$PLUG_DST'/" 2>/dev/null || true
# cp -r 是叠加不是镜像：源码里已改名的模块（inject.py → prompt_inject.py）会在宿主留下旧副本。
# 旧副本一旦被 import 就是「找不到新函数」的根源，所以按源码清单清掉宿主 core/ 里的多余 .py。
run "for f in '$PLUG_DST'/core/*.py; do [ -e \"\$f\" ] || continue; b=\$(basename \"\$f\"); [ -e '$PLUG_SRC/core/'\"\$b\" ] || { say \"  清除宿主残留 core/\$b\"; rm -f \"\$f\"; }; done"
run "rm -rf '$PLUG_DST'/core/__pycache__ '$PLUG_DST'/__pycache__"

# 2) data_dir 必须指向仓库（TASKS.md / META.yaml 所在）
#    漏了这步：插件能开、界面能出，但 tasks 恒为 0（GAP-02 实测）
say "② 改写 data_dir → $REPO_NATIVE"
if [ "$DRY" = 1 ]; then
  say "  [dry] 改写 data_dir: $REPO_NATIVE"
else
  python - "$PLUG_DST/plugin.yaml" "$REPO_NATIVE" <<'PY'
import sys, pathlib, re
p, repo = pathlib.Path(sys.argv[1]), sys.argv[2].replace("\\", "/")
t = p.read_text(encoding="utf-8")
t2 = re.sub(r"(?m)^data_dir:.*$", f"data_dir: {repo}", t)
if t2 == t and f"data_dir: {repo}" not in t:
    raise SystemExit("❌ 没找到 data_dir 行，装出来的插件会是空的")
p.write_text(t2, encoding="utf-8")
print(f"  data_dir → {repo}")
PY
fi

# 3) 桌面插件
say "③ 同步桌面插件"
run "mkdir -p '$DESK_DST'"
run "cp '$DESK_SRC/plugin.js' '$DESK_DST/'"

# 4) 启用（CLI 认的名字是 plugin.yaml 的 name，不是目录名 —— 这是实测踩过的坑）
say "④ 启用插件"
if [ "$DRY" = 1 ]; then
  say "  [dry] hermes plugins enable \"Hermes Cockpit\""
else
  hermes plugins enable "Hermes Cockpit" || \
    say "  ⚠️ 启用失败：CLI 认的 key 取自 plugin.yaml 的 name 字段，用目录名会报 No plugin named"
fi

# 5) 自检：真的取到数吗
say ""
say "⑤ 自检"
if [ "$DRY" = 1 ]; then
  say "  [dry] 跑 overview 取数检查"
else
  PY="$HOST_HOME/hermes-agent/venv/Scripts/python.exe"
  [ -x "$PY" ] || PY="$(command -v python)"
  "$PY" - "$PLUG_DST/dashboard/plugin_api.py" <<'PY'
import sys, json, importlib.util
spec = importlib.util.spec_from_file_location("api", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ov = m.handle("GET", "/overview", None)
st = ov["stats"]
print(f"  项目 {st['projects']} / 会话 {st['sessions']} / 任务 {st['tasks']} / 看板 {st['boards']}")
if st["tasks"] == 0:
    print("  ❌ tasks=0 —— data_dir 没指对，检查 ② 那步")
    sys.exit(1)
if not ov.get("stamp"):
    print("  ❌ stamp 为空 —— 写操作会一律 428")
    sys.exit(1)
print("  ✅ 取数正常，写入口可用")
PY
fi

say ""
say "完成。桌面 App 无需重启（宿主会自动 reload 插件）。"
