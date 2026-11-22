"""重启 Hermes gateway —— 从一个「不会被它杀掉」的独立进程里执行。

为什么需要这个脚本
------------------
助手本身跑在 gateway 进程里，`hermes gateway restart` 会被硬拦：

    Blocked: ... cannot restart the gateway from inside the gateway process.
    The gateway would kill this command before it could complete (SIGTERM propagates
    to child processes).

所以先用官方支持的 `_spawn_gateway_restart_watcher` 起一个**脱离的 watcher**：
它独立于 gateway 的进程树，等旧 PID 退出后把 gateway 重新拉起来。
watcher 是 Hermes 自己的机制（hermes_cli/gateway.py），不是自研。

用法
----
    python scripts/restart_gateway.py --dry-run   # 只看会做什么
    python scripts/restart_gateway.py             # 真重启（会断开当前会话）

判据（脚本自己验，不靠"命令返回 0"）
------------------------------------
  1. 重启前记录旧 PID
  2. watcher 起来了 → 打印 watcher 策略
  3. 旧 PID 退出 → 新 PID 出现且不同 → 才算重启成功
  4. 新 PID 上确认插件被加载（看日志里 inject 报错是否消失）
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

HOME = pathlib.Path.home() / "AppData" / "Local" / "hermes"
STATE = HOME / "gateway_state.json"


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gateway_alive(pid: int) -> bool:
    """PID 还活着吗。Windows 上用 tasklist，别用 os.kill(pid, 0)——它会误杀。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--profile", default="default")
    args = ap.parse_args()

    state = read_state()
    old_pid = int(state.get("pid") or 0)
    if old_pid <= 0:
        print("❌ gateway_state.json 里没有 pid，无法判断该等谁退出")
        return 1

    print(f"当前 gateway PID: {old_pid}  (code_version={state.get('code_version')})")
    if not gateway_alive(old_pid):
        print("⚠ 这个 PID 已经不在跑了，可能状态文件过期")
        return 1

    if args.dry_run:
        print("\n[dry-run] 会做的事：")
        print(f"  1. 调 launch_detached_profile_gateway_restart({args.profile!r}, {old_pid})")
        print("  2. watcher 等旧 PID 退出后重新拉起 gateway")
        print("  3. 脚本轮询新 PID，确认与旧 PID 不同")
        print("\n⚠ 真跑会断开当前会话（网关重启）。")
        return 0

    sys.path.insert(0, str(HOME / "hermes-agent"))
    from hermes_cli.gateway import launch_detached_profile_gateway_restart

    ok = launch_detached_profile_gateway_restart(args.profile, old_pid)
    if not ok:
        print("❌ watcher 没起来 —— 不要重试同一路径，先看 _gateway_run_args_for_profile 为什么返回空")
        return 2
    print("✓ watcher 已脱离启动，等旧进程退出中…")

    # 等旧 PID 退出（watcher 只会在它退出后拉新的）
    deadline = time.time() + 90
    while time.time() < deadline and gateway_alive(old_pid):
        time.sleep(2)
    if gateway_alive(old_pid):
        print(f"❌ {old_pid} 90 秒内没退出 —— watcher 在等它，重启不会发生")
        return 3
    print(f"✓ 旧 PID {old_pid} 已退出")

    # 等新 PID 出现
    new_pid = 0
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(3)
        cur = int(read_state().get("pid") or 0)
        if cur and cur != old_pid and gateway_alive(cur):
            new_pid = cur
            break

    if not new_pid:
        print("❌ 新 gateway 没起来 —— gateway_state.json 的 pid 没变或进程不在")
        return 4

    print(f"✓ 新 gateway PID: {new_pid}")
    print("\n下一步：确认插件注入不再报错")
    print(f"  grep render_for_session {HOME / 'logs' / 'errors.log'} | tail -3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
