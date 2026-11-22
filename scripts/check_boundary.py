#!/usr/bin/env python
"""check_boundary — F1 边界扫描（S10 的机器判据）。

规则（DD-001 §11.1「适配层边界(写死)」/ WBS F1）：
    宿主 = Hermes 本体。所有对 projects.db / state.db / kanban.db / gateway RPC 的访问，
    **只允许**出现在 plugins/project-hub/dashboard/host_adapter.py。

被扫描范围：plugins/project-hub/ 与 desktop-plugins/ 下全部 .py / .js
            （tests/ 目录及其 test_*.py / *.test.js 不入扫描：它们本就要构造越界样例来证明扫描有效）
豁免：适配层本体（--adapter 可改，默认 dashboard/host_adapter.py）

检出类型：
    HOST_DB_CONNECT   sqlite3.connect(...) / open_ro(...) 且指向宿主库（字面量或该文件里绑定到宿主库的变量）
    WRITE_SQL         INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / REPLACE / TRUNCATE
    HOST_DB_LITERAL   projects.db / state.db / kanban.db 字面量
    HOST_RPC          host.request( / host.call( / host.invoke( / host.rpc(
    HERMES_HOME_ACCESS  自己拼 LOCALAPPDATA/hermes 或 ~/.hermes —— 绕过适配层 = 越界
    HOST_HOOK         on_session_start / register_hook / ctx.hook —— hook 注册只在适配层（Q-01）
    HOST_REGISTER     适配层之外调用宿主入口 ctx.register_*（错误信息指向适配层）

Q-01 的边界是「数据与网关」，不是「宿主 SDK 的任何调用」，所以：
    · `ctx.rest(` **一律放行** —— 它打到本插件自己的 dashboard/plugin_api.py，是对外接口不是宿主私有调用
    · `on_session_start` / `register_hook` / `ctx.hook` 属于宿主插件 API，换宿主就得改 ⇒ 只在适配层

第六轮裁定（E2）新增一处**明确豁免**：插件目录 `__init__.py` 里的**入口函数定义**
`def register(ctx)`（宿主约定的 entry point，签名由宿主校验），以及它调用
`self._adapter().register_*` 的那一行。豁免是**按模式精确匹配**的：
    · 只认 `__init__.py` 这个路径，且只认「定义 register」或「经 `_adapter()` 转调」
    · 直接写 `ctx.register_system_prompt_section(...)` 仍然违规（宿主调用必须落适配层）
    · `on_session_start` / `register_hook` / `ctx.hook` 的禁令**不因这条豁免而放宽**

输出格式（WBS 要求）：`文件:行号  违规类型  该行内容`
退出码：干净 0；有违规 1；用法错误 2。

用法：
    python scripts/check_boundary.py                 # 扫仓库
    python scripts/check_boundary.py --json          # 机器可读
    python scripts/check_boundary.py --root <dir>    # 扫别的目录（自证用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ── 扫描配置 ────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("plugins/project-hub", "desktop-plugins")
SCAN_SUFFIXES = {".py", ".js"}

DEFAULT_ADAPTER = "plugins/project-hub/dashboard/host_adapter.py"

SKIP_DIR_NAMES = {"tests", "test", "__pycache__", "node_modules", ".git", "venv", ".venv"}
SKIP_FILE_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r".*_test\.py$"),
    re.compile(r".*\.test\.js$"),
    re.compile(r".*\.spec\.js$"),
)

HOST_DB_NAMES = ("projects.db", "state.db", "kanban.db")

# ── 正则 ────────────────────────────────────────────────────────────────────

RE_HOST_DB_LITERAL = re.compile(r"\b(" + "|".join(re.escape(n) for n in HOST_DB_NAMES) + r")\b")

# 数据库连接入口（宿主库与否由同行字面量 / 变量绑定判定）
RE_CONNECT = re.compile(r"\bsqlite3\s*\.\s*connect\s*\(|\bopen_ro\s*\(|\bDatabase\s*\(")

# 写 SQL：必须是语句形态，避免把英文散文里的 update/delete 判成违规
RE_WRITE_SQL = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE\s+[\[\]\"'`\w]+|DELETE\s+FROM"
    r"|DROP\s+(?:TABLE|INDEX|VIEW|TRIGGER)|CREATE\s+(?:TABLE|INDEX|VIEW|TRIGGER)"
    r"|ALTER\s+TABLE|REPLACE\s+INTO|TRUNCATE\s+TABLE)\b",
    re.IGNORECASE,
)

RE_HOST_RPC = re.compile(r"\bhost\s*\.\s*(?:request|call|invoke|rpc)\s*\(")

# Q-01：hook 注册只在适配层。`ctx.rest(` 不在此列（那是打本插件自己的后端，裁定明文豁免）。
# 用 \b 保证 `my_register_hook` / `_on_session_started` 这类近似名不被误判。
RE_HOST_HOOK = re.compile(
    r"\bon_session_start\b"
    r"|\bregister_hook\b"
    r"|\bctx\s*\.\s*hook\b"
)

RE_HERMES_HOME = re.compile(
    r"LOCALAPPDATA[^\n]{0,40}\bhermes\b"
    r"|[\"'`][^\"'`]*[/\\]\.hermes[/\\]"
    r"|join\s*\(\s*[^)\n]*?[\"']hermes[\"']",
    re.IGNORECASE,
)

# 插件入口函数的精确豁免（第六轮裁定 §三 第 1 步 / §四 要求 2）。
# 宿主约定：插件目录 __init__.py 必须导出 register(ctx)，它在里面把 ctx 转交适配层。
# 这里只豁免「定义」与「经 _adapter() 转调」两种写法；直接调 ctx.register_* 仍违规。
RE_REGISTER_DEF = re.compile(r"^\s*(?:async\s+)?def\s+register\s*\(\s*(?:ctx|context)\s*\)")
RE_REGISTER_FORWARD = re.compile(r"_adapter\s*\(\s*\)\s*\.\s*register_\w+\s*\(")

# 适配层之外的宿主入口注册调用（HOST_REGISTER）。`ctx.rest(` 不在此列。
RE_HOST_REGISTER = re.compile(r"\bctx\s*\.\s*register_\w+\s*\(")

RE_ASSIGN = re.compile(r"([A-Za-z_][\w.]*)\s*=\s*(.*)")


class Finding:
    __slots__ = ("path", "line", "kind", "text")

    def __init__(self, path: str, line: int, kind: str, text: str):
        self.path, self.line, self.kind, self.text = path, line, kind, text

    def as_dict(self) -> dict:
        return {"file": self.path, "line": self.line, "kind": self.kind,
                "text": self.text.strip()}

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.kind}  {self.text.strip()}"


# ── 预处理：注释不是代码 ────────────────────────────────────────────────────


def _strip_comment(line: str, suffix: str) -> str:
    """去掉行内注释，保留代码（跳过字符串内部的 # 与 //）。"""
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote and line[i - 1:i] != "\\":
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            continue
        if suffix == ".py" and ch == "#":
            break
        if suffix == ".js" and line[i:i + 2] == "//":
            break
        out.append(ch)
    return "".join(out)


def _block_comment_lines(lines: list[str], suffix: str) -> set[int]:
    """块注释 / docstring 覆盖的行号（1 基）。误判方向是「多跳过」，可接受。

    ⚠️ 这是扫描器最脆的一段：旧实现**只认行首的三引号**，于是
    `def f(): \"\"\"docstring 里提到 on_session_start\"\"\"`（紧贴缩进、与代码同行）
    被当成真实代码，把「文档提及」误报成 hook 违规（08-22 实测）。
    现在逐 token 扫：行内任意位置起的三引号都算注释段，成对闭合即止。
    一行里既有注释又有代码时，该行**按代码处理**（宁可多报一条，不漏一条）。
    """
    inside: set[int] = set()
    if suffix == ".py":
        delim = None
        for n, line in enumerate(lines, 1):
            code_seen = False
            idx = 0
            while True:
                if delim is None:
                    cand = [(p, d) for p, d in ((line.find('"""', idx), '"""'),
                                                (line.find("'''", idx), "'''")) if p >= 0]
                    if not cand:
                        if line[idx:].strip():
                            code_seen = True
                        break
                    start, d = min(cand)
                    if line[idx:start].strip():
                        code_seen = True          # 三引号之前有代码
                    delim = d
                    idx = start + 3
                else:
                    end = line.find(delim, idx)
                    if end < 0:
                        break
                    idx = end + 3
                    delim = None
            if delim is None:
                # 本行三引号成对闭合
                if code_seen:
                    continue                      # 有代码 → 这一行按代码判
                inside.add(n)                     # 纯注释行 → 跳过
            else:
                # 行尾仍在注释段内；若三引号之前有代码，该行按代码判
                if not code_seen:
                    inside.add(n)
        return inside

    in_block = False
    for n, line in enumerate(lines, 1):
        if in_block:
            inside.add(n)
            if "*/" in line:
                in_block = False
            continue
        idx = line.find("/*")
        if idx >= 0 and "*/" not in line[idx:]:
            in_block = True
            inside.add(n)
    return inside


# ── 单文件扫描（单测直接调用，不需要落盘）──────────────────────────────────


def _is_plugin_entry_file(rel_path: str) -> bool:
    """插件入口文件 = `<...>/plugins/<name>/__init__.py`（宿主约定 register(ctx) 的位置）。"""
    p = rel_path.replace("\\", "/")
    return p.endswith("/__init__.py") and "/plugins/" in "/" + p


def scan_text(text: str, rel_path: str, suffix: str) -> list[Finding]:
    lines = text.splitlines()
    skip = _block_comment_lines(lines, suffix)
    entry_file = _is_plugin_entry_file(rel_path)

    # 第一遍：文件内「绑定过宿主库的变量名」，用于 db = state_db() -> sqlite3.connect(db) 这类写法
    host_vars: set[str] = set()
    for raw in lines:
        code = _strip_comment(raw, suffix)
        m = RE_ASSIGN.search(code)
        if m and RE_HOST_DB_LITERAL.search(m.group(2)):
            host_vars.add(m.group(1).split(".")[-1])

    findings: list[Finding] = []
    for n, raw in enumerate(lines, 1):
        if n in skip:
            continue
        code = _strip_comment(raw, suffix)
        if not code.strip():
            continue

        if RE_CONNECT.search(code):
            if RE_HOST_DB_LITERAL.search(code) or (
                    set(re.findall(r"[A-Za-z_]\w*", code)) & host_vars):
                findings.append(Finding(rel_path, n, "HOST_DB_CONNECT", raw))
                continue

        if RE_WRITE_SQL.search(code):
            findings.append(Finding(rel_path, n, "WRITE_SQL", raw))
            continue

        if RE_HOST_DB_LITERAL.search(code):
            findings.append(Finding(rel_path, n, "HOST_DB_LITERAL", raw))
            continue

        if RE_HOST_RPC.search(code):
            findings.append(Finding(rel_path, n, "HOST_RPC", raw))
            continue

        if RE_HERMES_HOME.search(code):
            findings.append(Finding(rel_path, n, "HERMES_HOME_ACCESS", raw))
            continue

        # Q-01 放在最后：它只认三个精确令牌，前面几条是更严重的越界，命中哪个报哪个
        if RE_HOST_HOOK.search(code):
            findings.append(Finding(rel_path, n, "HOST_HOOK", raw))
            continue

        # 宿主入口注册：豁免只在插件 __init__.py 的「register 定义行」与「经 _adapter() 转调行」
        if RE_HOST_REGISTER.search(code) and not (
                entry_file and (RE_REGISTER_DEF.search(code) or RE_REGISTER_FORWARD.search(code))):
            findings.append(Finding(rel_path, n, "HOST_REGISTER", raw))
            continue

    return findings


# ── 目录遍历 ────────────────────────────────────────────────────────────────


def _iter_files(root: Path, scan_dirs: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for d in scan_dirs:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix not in SCAN_SUFFIXES:
                continue
            rel_parts = p.relative_to(root).parts
            if any(part in SKIP_DIR_NAMES for part in rel_parts[:-1]):
                continue
            if any(pat.match(p.name) for pat in SKIP_FILE_PATTERNS):
                continue
            files.append(p)
    return files


def scan(root: Path = REPO_ROOT, adapter: str = DEFAULT_ADAPTER,
         scan_dirs: tuple[str, ...] = SCAN_DIRS) -> list[Finding]:
    root = Path(root).resolve()
    adapter_path = (root / adapter).resolve()
    findings: list[Finding] = []
    for f in _iter_files(root, scan_dirs):
        if f.resolve() == adapter_path:
            continue  # 适配层本体豁免
        rel = f.relative_to(root).as_posix()
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # 读不了不能算干净：当作违规报出
            findings.append(Finding(rel, 0, "UNREADABLE", f"<{e}>"))
            continue
        findings.extend(scan_text(text, rel, f.suffix))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hermes Cockpit 适配层边界扫描（S10）")
    ap.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录")
    ap.add_argument("--adapter", default=DEFAULT_ADAPTER, help="适配层相对路径（豁免）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"根目录不存在：{root}", file=sys.stderr)
        return 2

    findings = scan(root, args.adapter)
    scanned = len(_iter_files(root.resolve(), SCAN_DIRS))

    if args.json:
        print(json.dumps([f.as_dict() for f in findings], ensure_ascii=False, indent=2))
    elif findings:
        for f in findings:
            print(str(f))
        print(f"\n[FAIL] {len(findings)} 处越界（扫描 {scanned} 个文件，"
              f"适配层 {args.adapter} 已豁免）")
    else:
        print(f"[OK] 适配层外无宿主调用（扫描 {scanned} 个文件，"
              f"适配层 {args.adapter} 已豁免）")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
