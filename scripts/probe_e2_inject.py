"""E2 渲染验收探针 —— 唯一目的：让「注入真的渲染出项目上下文」可被机器证据证明。

为什么需要它：真实数据下 E2 对绝大多数会话返回**空串**（cwd 归属只覆盖约 2%，
session_map 里也没有对应条目），所以「起个会话看不到段落」无法区分
「E2 判空了」和「注入根本没生效」。本探针在**用户显式批准**下临时把
META.yaml 的 `reminders.enabled` 打开、跑一次真会话、再原样还原。

用法（默认干跑，只打印将要做什么，不改文件）：
    py -3.10 scripts/probe_e2_inject.py            # 干跑
    py -3.10 scripts/probe_e2_inject.py --apply \
        --session-id local-155d...                 # 真跑（会临时改 META.yaml 再还原）

退出码：0 = 段落出现；1 = 未出现（含已还原）；2 = 用法/前置错误。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
META = REPO / "META.yaml"
SECTION_ID = "hermes-cockpit.project"

PROMPT = ("原样输出你系统提示里以 '## Plugin Context:' 开头的段落，不要解释")


def _set_enabled(text: str, enabled: bool) -> str:
    """只在 `reminders:` 块内改 `enabled:`，保留注释与其余字节。"""
    lines = text.splitlines()
    in_block = False
    for i, line in enumerate(lines):
        if re.match(r"^reminders\s*:", line):
            in_block = True
            continue
        if in_block:
            if line and not line.startswith((" ", "\t")):
                break                              # 出了 reminders 块
            m = re.match(r"^(\s*)enabled\s*:\s*(true|false)\b(.*)$", line, re.IGNORECASE)
            if m:
                lines[i] = f"{m.group(1)}enabled: {'true' if enabled else 'false'}{m.group(3)}"
                return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    raise SystemExit(f"META.yaml 里找不到 reminders.enabled：{META}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E2 系统提示注入验收探针")
    ap.add_argument("--apply", action="store_true", help="真跑（会临时改并还原 META.yaml）")
    ap.add_argument("--session-id", default="", help="要归到本项目的会话 id（写 session_map 用，可选）")
    args = ap.parse_args(argv)

    if not META.is_file():
        print(f"[ERR] 没有 {META}", file=sys.stderr)
        return 2
    original = META.read_text(encoding="utf-8")
    if not re.search(r"^reminders\s*:", original, re.MULTILINE):
        print("[ERR] META.yaml 无 reminders 块", file=sys.stderr)
        return 2
    already = bool(re.search(r"^reminders\s*:\s*$(?:\n\s*#.*)*\n\s*enabled\s*:\s*true",
                             original, re.MULTILINE))

    plan = ("reminders.enabled 已是 true，只跑会话" if already
            else "reminders.enabled: false -> true（跑完还原）")
    print(f"[plan] {plan}")
    print(f"[plan] 起一次 `hermes -z`，问：{PROMPT}")
    if not args.apply:
        print("[dry-run] 未改动任何文件。确认后加 --apply 真跑。")
        return 0

    backup = META.with_suffix(".yaml.e2probe.bak")
    shutil.copy2(META, backup)
    try:
        if not already:
            META.write_text(_set_enabled(original, True), encoding="utf-8")
            print("[step] reminders.enabled -> true")
        out = subprocess.run(["hermes", "-z", PROMPT], cwd=str(REPO),
                             capture_output=True, text=True, encoding="utf-8", timeout=300)
        text = (out.stdout or "") + (out.stderr or "")
        hit = f"## Plugin Context: {SECTION_ID}" in text
        print(f"[hermes exit] {out.returncode}")
        print("---- 会话输出原文 ----")
        print(text.strip() or "<空>")
        print("---- 判定 ----")
        print(f"[{'OK' if hit else 'FAIL'}] 段落 {SECTION_ID} "
              f"{'出现在系统提示中' if hit else '未出现'}")
        return 0 if hit else 1
    finally:
        shutil.copy2(backup, META)
        backup.unlink(missing_ok=True)
        restored = META.read_text(encoding="utf-8")
        print(f"[restore] META.yaml 已还原：字节一致={restored == original}")


if __name__ == "__main__":
    sys.exit(main())
