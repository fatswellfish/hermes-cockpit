"""meta — META.yaml 的读写层（D1 + E1 + B2 的公共底座）。

为什么不用 PyYAML：插件应尽量零依赖。但 META.yaml 结构是**嵌套**的
（setup / categories / session_map / reminders），手写解析必须支持缩进层级，
所以这里实现一个够用的最小 YAML 子集解析器：

    支持：  key: value        （标量，自动识别 null/true/false/数字/字符串）
            key:                  （嵌套映射）
              child: value
            key: [a, b, c]        （流式列表）
            key:                  （块列表）
              - item
    不支持：锚点/别名、多行标量(| >)、带引号的复杂转义 —— META.yaml 用不到

写回策略：**保留注释与原有顺序**，只替换被改动的键所在行。
这是「人可直读、人机共写」的要求（R-11）：不能因为程序写一次就把人的注释抹掉。
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# ── 最小 YAML 子集解析 ─────────────────────────────────────────────────────

_NULL = {"null", "~", ""}
_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}


def _scalar(raw: str):
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in _NULL:
        return None
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _flow_list(raw: str) -> list:
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [_scalar(x) for x in inner.split(",")]


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但不动引号内的 #。"""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _flow_map(raw: str) -> dict:
    """解析流式映射 {project: mms, source: manual, at: "..."}。"""
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return {}
    out: dict = {}
    for part in _split_flow(inner):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        out[k.strip().strip("\"'")] = _scalar(v)
    return out


def _split_flow(inner: str) -> list[str]:
    """按逗号切分，但不切引号内与嵌套括号内的逗号。"""
    parts, buf, quote, depth = [], [], None, 0
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "{[":
            depth += 1
            buf.append(ch)
        elif ch in "}]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _value(raw: str):
    """统一入口：{...} → dict，[...] → list，其余 → 标量。"""
    s = raw.strip()
    if s.startswith("{") and s.endswith("}"):
        return _flow_map(s)
    if s.startswith("[") and s.endswith("]"):
        return _flow_list(s)
    return _scalar(s)


def loads(text: str) -> dict:
    """解析 YAML 子集（支持块列表作为映射值）。

    与 loads 的区别：遇到 `key:` 后若下一行是 `- `，则该 key 是列表。
    """
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cleaned = _strip_comment(raw)
        if cleaned.strip():
            lines.append((len(cleaned) - len(cleaned.lstrip()), cleaned.strip()))

    def parse_block(i: int, indent: int):
        """返回 (value, next_i)。根据首行是 `- ` 决定 list 还是 dict。"""
        if i >= len(lines):
            return {}, i
        node: dict | list
        node = [] if lines[i][1].startswith("- ") else {}

        while i < len(lines) and lines[i][0] == indent:
            ind, body = lines[i]
            if isinstance(node, list):
                if not body.startswith("- "):
                    break
                item = body[2:].strip()
                # 列表项可能是标量，也可能是 `k: v`（本文件用不到，按标量处理）
                node.append(_scalar(item))
                i += 1
                continue

            if body.startswith("- "):
                break
            key, _, rest = body.partition(":")
            key = key.strip().strip("\"'")
            rest = rest.strip()
            if rest:
                node[key] = _value(rest)
                i += 1
            else:
                # 子块：判断下一行是同级更深缩进的 list 还是 dict
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    child, i = parse_block(i + 1, lines[i + 1][0])
                    node[key] = child
                else:
                    node[key] = None
                    i += 1
        return node, i

    if not lines:
        return {}
    value, _ = parse_block(0, lines[0][0])
    return value if isinstance(value, dict) else {}


# ── 读 ─────────────────────────────────────────────────────────────────────

DEFAULTS = {
    "setup": {"status": "pending", "mode": None, "at": None},
    "unclassified_slug": "未分类",
    "categories": ["办公事务", "审核流程", "家庭提醒", "Hermes 开发", "内容生产"],
    "project_categories": {},
    "session_map": {},
    "reminders": {"enabled": False},
}


def load(path: Path) -> dict:
    """读 META.yaml，缺字段用默认值补齐（不写盘）。"""
    cfg = loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    out = dict(DEFAULTS)
    for k, v in DEFAULT_DEFAULTS_ITEMS():
        out[k] = v
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def DEFAULT_DEFAULTS_ITEMS():
    """深拷贝默认值，避免调用方改到模块级常量。"""
    return [(k, (dict(v) if isinstance(v, dict) else
                 list(v) if isinstance(v, list) else v)) for k, v in DEFAULTS.items()]


def session_map(path: Path) -> dict:
    return load(path).get("session_map") or {}


def find_session(path: Path, session_id: str) -> dict | None:
    entry = session_map(path).get(session_id)
    return entry if isinstance(entry, dict) else None


# ── 写（保留注释与顺序）───────────────────────────────────────────────────


class MetaWriteError(RuntimeError):
    """META.yaml 写失败（例如要写的键在结构里找不到位置）。"""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".meta-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _set_simple(text: str, section: str | None, key: str, value: str) -> str:
    """在 `section:` 块内把 `key:` 的值替换掉（保留缩进与行尾注释）。

    section=None 表示顶层键。找不到就追加到对应块末尾。
    """
    lines = text.splitlines()
    rendered = _render_scalar(value)

    target_indent = None
    in_section = section is None
    sec_indent = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if section is not None:
            if indent == 0 and stripped.startswith(f"{section}:"):
                in_section = True
                sec_indent = indent
                continue
            if in_section and indent == 0 and not stripped.startswith(f"{section}:"):
                # 出了这个 section，准备在它的末尾插入
                lines.insert(_section_end(lines, section), f"  {key}: {rendered}")
                return "\n".join(lines) + "\n"
            if not in_section:
                continue
            target_indent = indent
            if stripped.startswith(f"{key}:"):
                comment = ""
                if "#" in line:
                    comment = "  " + line[line.index("#"):].rstrip()
                lines[i] = f"{' ' * indent}{key}: {rendered}{comment}"
                return "\n".join(lines) + "\n"
        else:
            if indent == 0 and stripped.startswith(f"{key}:"):
                comment = ""
                if "#" in line:
                    comment = "  " + line[line.index("#"):].rstrip()
                lines[i] = f"{key}: {rendered}{comment}"
                return "\n".join(lines) + "\n"

    # 没找到：插入
    if section is None:
        lines.append(f"{key}: {rendered}")
    else:
        pos = _section_end(lines, section)
        insert_indent = "  "
        for line in lines:
            if line.strip().startswith(f"{section}:"):
                break
        else:
            lines.append("")
            lines.append(f"{section}:")
        lines.insert(pos, f"{insert_indent}{key}: {rendered}")
    return "\n".join(lines) + "\n"


def _section_end(lines: list[str], section: str) -> int:
    """返回 section 块之后的插入位置（第一个缩进回到 0 的非空非注释行之前）。"""
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{section}:") and not line.startswith(" "):
            start = i
            break
    if start is None:
        return len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j]
        if s.strip() and not s.startswith(" ") and not s.strip().startswith("#"):
            return j
    return len(lines)


def _render_scalar(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    s = str(value)
    # 含特殊字符时加引号（时间戳含冒号，必须加引号）
    if s == "" or any(c in s for c in ":#[]{},&*!|>%@`") or s != s.strip():
        return '"' + s.replace('"', '\\"') + '"'
    return s


def set_setup(path: Path, *, status: str | None = None, mode: str | None = None,
              at: str | None = None) -> None:
    """更新 setup 块。**at 一旦写入就不再改变**（DI-001 D12：此后永不改变）。

    写入顺序是硬约束（Q-04）：**先 `at`，再 `mode`，最后 `status`**。
    反过来的话，「mode 已经是 fresh、at 还空」这个中间态会真的落到磁盘上，
    整个 fresh 作用域就在那一瞬间失效（Q-04 判它为异常并清空作用域）。
    每次只调一次 `_atomic_write`，所以磁盘上要么没有这一批改动，要么全有。
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    current = load(path).get("setup") or {}

    if at is not None:
        if current.get("at"):
            # 已有锚点：拒绝改写（单向门）
            raise MetaWriteError(
                f"setup.at 已存在（{current['at']}），按 DI-001 D12 永不改变，拒绝覆盖")
        text = _set_simple(text, "setup", "at", at)
    if mode is not None:
        text = _set_simple(text, "setup", "mode", mode)
    if status is not None:
        text = _set_simple(text, "setup", "status", status)
    _atomic_write(path, text if text.endswith("\n") else text + "\n")



def set_reminders(path: Path, enabled: bool) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = _set_simple(text, "reminders", "enabled", enabled)
    _atomic_write(path, text if text.endswith("\n") else text + "\n")


def set_categories(path: Path, categories: list[str]) -> None:
    """整体替换 categories 块列表，保留前后注释。"""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("categories:") and not line.startswith(" "):
            start = i
            break
    new_block = ["categories:"] + [f"  - {c}" for c in categories]
    if start is None:
        lines.extend([""] + new_block)
    else:
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith(" ")):
            if lines[end].strip().startswith("#"):
                end += 1
                continue
            end += 1
        lines[start:end] = new_block
    _atomic_write(path, "\n".join(lines) + "\n")


def set_session_project(path: Path, session_id: str, project: str,
                        source: str = "manual", at: str | None = None) -> None:
    """写一条会话归属。**不调用任何宿主移动接口**（#86626）。

    source 三值：manual / import-confirmed / suggested-confirmed
    """
    if source not in ("manual", "import-confirmed", "suggested-confirmed"):
        raise MetaWriteError(f"source 取值非法：{source}")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    stamp = at or _now_iso()

    lines = text.splitlines()
    # 找 session_map: 块
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("session_map:") and not line.startswith(" "):
            start = i
            break
    if start is None:
        lines.extend(["", "session_map: {}"])
        start = len(lines) - 1

    # 若写成 `session_map: {}` 空映射，先展开成块
    if lines[start].strip().endswith("{}"):
        lines[start] = "session_map:"

    entry = (f"  {session_id}: {{project: {project}, source: {source}, at: \"{stamp}\"}}")
    # 已存在同 id 则替换
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith(f"{session_id}:"):
            lines[j] = entry
            _atomic_write(path, "\n".join(lines) + "\n")
            return
        if lines[j].strip() and not lines[j].startswith(" "):
            lines.insert(j, entry)
            _atomic_write(path, "\n".join(lines) + "\n")
            return
    lines.append(entry)
    _atomic_write(path, "\n".join(lines) + "\n")


def _now_iso() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
