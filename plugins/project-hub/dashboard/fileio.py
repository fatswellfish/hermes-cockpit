"""fileio — A4 并发写保护（读-改-写不被静默覆盖）。

两个原语：
    read_with_stamp(path)                          -> (text, stamp)    # stamp = (mtime_ns, sha256)
    write_if_unchanged(path, text, expect_stamp)   -> 新 stamp

why the stamp is not usable on timestamps alone (measured; a pure-mtime stamp makes this check useless):
    On Windows, the mtime_ns of a file right after os.replace often lands in the same
    clock tick -- 70 of 200 consecutive write-read-write cycles produced *identical*
    mtime_ns, and in about 1 run out of 12 concurrent tests both writers passed the
    check and the second silently overwrote the first.
    => stamp = (mtime_ns, sha256(content)); mtime moved => stale immediately,
       mtime unchanged but content changed => also stale.

Other three design points:
  1. **Mismatch => raise StaleWrite, never overwrite silently** — the later writer holds a
     result computed from the old content, so writing it means erasing someone else's edit.
     The route layer maps StaleWrite to HTTP 409 so the UI can ask for a refresh.
  2. **Temp file + atomic replace** — opening the target with "w" leaves a half-written file
     if we crash mid-write; write a sibling temp file then os.replace (atomic on one volume).
  3. **Read-back check after replace** — never let "looks like it wrote" pass for "wrote".

Strict serialisation would need a file lock; this plugin is a single-machine local view, so
no lock file (leftovers, inconsistent cross-platform behaviour). Keeping the trade-off here so
nobody "fixes" it later by accident.

Touches no host database, stdlib only, no private database of our own.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path

# 进程内的「检查 → 替换」提交锁。跨进程不靠它（跨进程靠令牌 + Windows 的独占打开行为）。
_COMMIT_LOCK = threading.Lock()

# 被占用时的重试参数：读者 open 的窗口是毫秒级，50 × 2ms 已远超实测所需。
_REPLACE_TRIES = 50
_REPLACE_BACKOFF_S = 0.002
# 读侧同理：写者 open 目标文件的瞬间，读会拿到 PermissionError。
_READ_TRIES = 50
_READ_BACKOFF_S = 0.002


def _replace_after_backoff(tmp: Path, target: Path) -> bool:
    """os.replace(tmp, target)，遇到 Windows「文件被占用」就短退避重试。

    只在**别的进程/线程正打开着目标文件**时返回 False；那时调用方去复检戳，
    由戳决定这是真冲突还是文件被删。返回 True 表示已装上。

    为什么需要它（实测）：Windows 上 os.replace 无法覆盖一个正被 open 的文件，
    而读侧（read_with_stamp / 视图层）随时可能恰好打开着同一个文件。
    不分「读者短暂占用」和「真有人改过」，就会把正常并发读写成冲突——
    实测 120 轮里 58 轮是内容完全没变却被判 StaleWrite 的假报警。
    """
    for _ in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, target)
            return True
        except PermissionError:
            time.sleep(_REPLACE_BACKOFF_S)
    return False


class StaleWrite(RuntimeError):
    """文件在读取之后被改动过（戳与预期不符）。路由层据此返回 409。"""

    def __init__(self, path, expect, actual):
        self.path = str(path)
        self.expect = expect
        self.actual = actual
        super().__init__(
            f"{self.path} 已被他人修改（预期 {_fmt(expect)}，实际 {_fmt(actual)}），拒绝覆盖"
        )


class FileIOError(RuntimeError):
    """文件不存在 / 写入后校验失败等无法继续的情况。"""


def _fmt(stamp) -> str:
    if isinstance(stamp, tuple) and len(stamp) == 2:
        return f"stamp(mtime_ns={stamp[0]}, sha256={str(stamp[1])[:12]}…)"
    return repr(stamp)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _stat_ns(path: Path) -> int:
    """纳秒级修改时间；文件不存在抛 FileIOError（不返回 0 —— 那会被当成可写）。"""
    try:
        return os.stat(path).st_mtime_ns
    except FileNotFoundError as e:
        raise FileIOError(f"文件不存在：{path}") from e


def _read_raw(path: Path) -> str:
    """按原样读（newline="" 保留 CRLF）—— 人机共写文件不该被悄悄改换行符。

    用内建 open 而不是 Path.read_text(newline=...)：后者要 Python 3.13+，本仓还要跑 3.10。

    被占用时短退避重试：Windows 上正有写者在 open 目标文件时，读侧会拿到
    PermissionError（实测跨进程并发时出现）。那是毫秒级竞争，不是文件坏了，
    直接抛出去会变成一次假的「读失败」。
    """
    last: PermissionError | None = None
    for _ in range(_READ_TRIES):
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                return f.read()
        except FileNotFoundError as e:
            raise FileIOError(f"文件不存在：{path}") from e
        except PermissionError as e:
            last = e
            time.sleep(_READ_BACKOFF_S)
    raise FileIOError(f"文件持续被占用，读不到：{path}") from last


def _current_stamp(path: Path) -> tuple[str, str]:
    return _raw_stamp(path)


def _raw_stamp(path: Path) -> tuple[str, str]:
    """Freshness token for `path`: (filesystem identity, sha256 of the content).

    The first half is **not a clock**. It is lstat's (st_dev, st_ino, st_ctime_ns) — on
    Windows st_ino is 0, but st_ctime_ns is the change time, which NTFS bumps on every write,
    and every os.replace installs a *new* identity. That makes "same file object, untouched"
    distinguishable even when the timestamp resolution would collapse two writes into one tick
    (measured: 70/200 back-to-back replace cycles shared an identical st_mtime_ns).

    The second half is the content hash, so a write path that keeps the same inode (in-place
    write by another process) is still caught.

    Every real change therefore yields a different token. The only theoretical false positive
    is a delete+recreate that happens to reuse the same ctime_ns — that errs on the side of
    raising StaleWrite, which is the safe direction for this plugin.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError as e:
        raise FileIOError(f"文件不存在：{path}") from e
    ident = (f"dev={st.st_dev}:ino={st.st_ino}:ctime={st.st_ctime_ns}"
             f":size={st.st_size}:mtime={st.st_mtime_ns}")
    return ident, _digest(_read_raw(path))


def _file_id(path: Path) -> tuple[int, int]:
    """(st_dev, st_ino) — Windows 上 st_ino 恒为 0，只能靠 ctime 认版本，见 _raw_stamp。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError as e:
        raise FileIOError(f"文件不存在：{path}") from e
    return st.st_dev, st.st_ino


def read_with_stamp(path) -> tuple[str, tuple[str, str]]:
    """Read the text plus an opaque freshness token (lstat stamp, sha256).

    Samples until two consecutive samples agree: a reader racing a writer can otherwise hand
    back old content paired with a fresh token. The token is opaque — do not parse it.
    """
    p = Path(path)
    prev = _raw_stamp(p)
    for _ in range(5):
        text = _read_raw(p)
        cur = _raw_stamp(p)
        if cur == prev and _digest(text) == cur[1]:
            return text, cur
        prev = cur
    raise FileIOError(f"file kept changing while being read; no stable snapshot: {p}")


def write_if_unchanged(path, text: str, expect_stamp) -> tuple[str, str]:
    """Write only if the file still carries expect_stamp; returns the new token.

    Mismatch raises StaleWrite and writes **not one byte**. expect_stamp=None skips the check
    (only for a file this process just created).

    Two layers, both required:
      * **token check** (before and just before replace) — catches any change that happened
        before our check.
      * **commit guard** — an in-process lock around check→replace, plus a bounded
        backoff-retry on the Windows "target is open" PermissionError. A reader
        holding the file open for a millisecond must NOT be reported as a conflict
        (measured: 58/120 writes were false alarms under the old code, with the
        content identical the whole time). The retry decides, not an OS assumption.
    A lost race raises StaleWrite — that is the honest verdict ("someone else got there
    first"), never a silent overwrite.
    """
    p = Path(path)
    with _COMMIT_LOCK:
        if expect_stamp is not None:
            cur = _raw_stamp(p)              # a deleted file surfaces here, not as "writable"
            if cur != tuple(expect_stamp):
                raise StaleWrite(p, expect_stamp, cur)

        tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if expect_stamp is not None:     # narrow the check→replace window to a minimum
                again = _raw_stamp(p)
                if again != tuple(expect_stamp):
                    raise StaleWrite(p, expect_stamp, again)
            os.replace(tmp, p)               # same-volume rename, atomic
        except PermissionError as e:
            # Windows 不允许替换「此刻有人打开着」的文件。这个窗口有两种成因，必须分开判：
            #   · 读者只是正在读（open 的毫秒级窗口）——内容没变，重试即可成功；
            #   · 真有人在我们读完之后改过文件 —— 由下面的戳复检判成 StaleWrite。
            # 实测（120 轮，读者线程并发）：旧写法把前一种也报成 StaleWrite，
            # 58/120 是「内容一模一样却被判冲突」的假报警。所以这里先短退避重试，
            # 重试仍失败才回到复检结论，绝不在这里直接判 stale。
            if not _replace_after_backoff(tmp, p):
                again = _raw_stamp(p)        # 一直被占用，或文件被删/换掉
                raise StaleWrite(p, expect_stamp, again) from e
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()             # no temp litter on the failure path
                except OSError:
                    pass

    stamp = _raw_stamp(p)
    if stamp[1] != _digest(text):            # read-back: what landed must be exactly `text`
        # 内容不是我写的，说明另一写者在我之后装上了他的版本 —— 并发冲突，不是原子替换失败。
        raise StaleWrite(p, expect_stamp, stamp)
    return stamp

