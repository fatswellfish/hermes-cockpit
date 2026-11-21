#!/usr/bin/env python
"""probe_session_create — 在宿主网关里真开一个会话，回查 sessions.cwd（D6 落地取证）。

为什么不能用「HTTP 轮询一个自己猜的端口」：
    实测本机 0.23.2 的 gateway_state.json **没有任何端口字段**（keys 里没有 port），
    netstat 也没有 8000 在听；网关的对外面是本地 **WebSocket**，客户端要先用控制 socket
    换一张 session-ticket 才能连上。所以「先读 port、再试 /rpc、/jsonrpc」的写法只会落进
    「读不到网关端口」这个分支 —— 那是错的实现（自造的契约），不是宿主不可用。

本脚本只用宿主自己发布的官方客户端（hermes_cli.gateway_client.connect_gateway），
不手搓 HTTP、不猜端口。cwd_explicit=True 是 FS-001 §11.1 的决定字段。

用法：
    python scripts/probe_session_create.py --cwd <存在的绝对目录> [--title ...]
退出码：0 = 建成功；1 = 调用失败；2 = cwd 不合法；3 = 找不到宿主安装树。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _host_agent_root() -> Path | None:
    """Hermes 安装树（含 gateway/ 与 hermes_cli/ 的那一层）。取证脚本不该给本仓加依赖。"""
    env = os.environ.get("HERMES_AGENT_ROOT")
    if env and (Path(env) / "hermes_cli").is_dir():
        return Path(env)
    home = os.environ.get("HERMES_HOME")
    roots = ([Path(home)] if home else []) + [
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes",
    ]
    for r in roots:
        cand = r / "hermes-agent"
        if (cand / "hermes_cli").is_dir():
            return cand
    return None


async def _create(cwd: str, title: str | None, timeout: float) -> dict:
    from hermes_cli.gateway_client import connect_gateway

    params = {"cwd": cwd, "cwd_explicit": True, "source": "gui"}
    if title:
        params["title"] = title
    async with connect_gateway() as client:
        return await client.rpc("session.create", _timeout=timeout, **params)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真开一个会话并回查 cwd（D6 取证）")
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--title", default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cwd = Path(args.cwd)
    if not cwd.is_absolute() or not cwd.is_dir():
        print(f"cwd 必须是存在目录的绝对路径：{args.cwd!r}", file=sys.stderr)
        return 2

    root = _host_agent_root()
    if root is None:
        print(r"找不到宿主安装树（HERMES_AGENT_ROOT / %LOCALAPPDATA%\hermes\hermes-agent）", file=sys.stderr)
        return 3
    sys.path.insert(0, str(root))
    try:
        result = asyncio.run(_create(str(cwd), args.title, args.timeout))
    except Exception as e:                      # noqa: BLE001 - 任何失败都要原样报出来
        print(f"session.create 失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    session_id = (result or {}).get("session_id") or (result or {}).get("id")
    if args.json:
        print(json.dumps({"requested_cwd": str(cwd), "session_id": session_id, "result": result},
                         ensure_ascii=False, default=str))
    else:
        print(f"session_id = {session_id}")
        print(f"result keys = {sorted((result or {}).keys())}")
    return 0 if session_id else 1


if __name__ == "__main__":
    sys.exit(main())
