"""Q-02 双向自证 —— scripts/check_boundary.py 的扫描器真的能抓到越界，且不误报。

两条判据缺一不可：
  · 造违规 → 退出码 1 **且报出行号**（只报 1 不报行号，人还得自己找）
  · 删掉违规 → 退出码 0（只验「能抓」不验「能放」，把扫描器改成永远返回 1 也能过测）

为什么必须做成测试：「扫描器干净」这个结论以前靠人手跑一次，改坏了没人知道。

Q-01 新规则一并锁死：
  · `on_session_start` / `register_hook` / `ctx.hook` 出现在适配层之外 → 违规（HOST_HOOK）
  · `ctx.rest(` → 一律放行（它打的是本插件自己的后端，裁定明文豁免）

跑法：python -m pytest tests/ -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import check_boundary as cb  # noqa: E402

SCANNER = REPO / "scripts" / "check_boundary.py"

# 违规片段：插在文件第 3 行（前面已经有两行），断言报出的行号就是它
VIOLATION_PY = 'db = sqlite3.connect("C:/Users/x/AppData/Local/hermes/state.db")\n'
HOOK_VIOLATIONS = [
    ("on_session_start", "def on_session_start(ctx):\n"),
    ("register_hook", 'ctx.register_hook("session_start", handler)\n'),
    ("ctx.hook", 'ctx.hook("pre_tool_call", handler)\n'),
]
EXEMPT_PY = 'r = ctx.rest("/overview")\n'


def _clean_file() -> str:
    return (
        '"""干净的插件模块。"""\n'
        "import sys\n"
        "\n"
        "\n"
        "def hello():\n"
        "    return 1\n"
    )


class _FakeRepo:
    """最小仓库骨架：plugins/hermes-cockpit/dashboard/<name>.py + 一个适配层。"""

    def __init__(self, extra_source: str = ""):
        self.dir = Path(tempfile.mkdtemp(prefix="cockpit-boundary-"))
        self.pkg = self.dir / "plugins" / "hermes-cockpit"
        self.dash = self.pkg / "dashboard"
        self.dash.mkdir(parents=True)
        (self.pkg / "__init__.py").write_text("", encoding="utf-8")
        # 适配层本体：豁免，里面怎么写都不算违规
        (self.dash / "host_adapter.py").write_text(
            '"""adapter"""\nimport sqlite3\n'
            "def open_ro(p):\n    return sqlite3.connect(p)\n"
            "def on_session_start(ctx):\n    pass\n", encoding="utf-8")
        (self.dash / "plugin_api.py").write_text(extra_source, encoding="utf-8")

    @property
    def target(self) -> Path:
        return self.dash / "plugin_api.py"

    def insert_line(self, snippet: str, line_no: int) -> None:
        lines = self.target.read_text(encoding="utf-8").splitlines(keepends=True)
        lines.insert(line_no - 1, snippet)
        self.target.write_text("".join(lines), encoding="utf-8")

    def scan_cli(self) -> subprocess.CompletedProcess:
        """按 WBS 的 CLI 契约跑一次：退出码 + stdout(JSON) + stderr。"""
        return subprocess.run(
            [sys.executable, str(SCANNER), "--root", str(self.dir), "--json"],
            capture_output=True, text=True, encoding="utf-8",
        )

    def findings(self) -> list[dict]:
        out = self.scan_cli()
        return json.loads(out.stdout or "[]")

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class TestViolationCaught(unittest.TestCase):
    """正向：造违规 → 退出码 1 且报出行号。"""

    def setUp(self):
        self.repo = _FakeRepo(_clean_file())

    def tearDown(self):
        self.repo.cleanup()

    def _append_and_scan(self, snippet: str, line_no: int, expect_kind: str):
        self.repo.insert_line(snippet, line_no)
        out = self.repo.scan_cli()
        self.assertEqual(out.returncode, 1,
                         f"未报违规。stdout={out.stdout!r} stderr={out.stderr!r}")
        hits = [f for f in json.loads(out.stdout)
                if f["file"].endswith("dashboard/plugin_api.py")]
        self.assertTrue(hits, out.stdout)
        self.assertEqual(hits[0]["line"], line_no, f"行号不对：{hits[0]}")
        self.assertEqual(hits[0]["kind"], expect_kind)
        return hits

    def test_host_db_connect_reports_line_number(self):
        hits = self._append_and_scan(VIOLATION_PY, 3, "HOST_DB_CONNECT")
        self.assertIn("state.db", hits[0]["text"])

    def test_write_sql_reports_line_number(self):
        self._append_and_scan('SQL = "DELETE FROM sessions WHERE 1"\n', 3, "WRITE_SQL")

    def test_host_db_literal_reports_line_number(self):
        self._append_and_scan('P = "state.db"\n', 3, "HOST_DB_LITERAL")

    def test_host_rpc_reports_line_number(self):
        self._append_and_scan('r = host.request("session.create", {})\n', 3, "HOST_RPC")

    def test_hermes_home_reports_line_number(self):
        self._append_and_scan('H = "C:/Users/x/.hermes/x"\n', 3, "HERMES_HOME_ACCESS")

    def test_line_number_follows_the_actual_offset(self):
        """行号不是常量：插到最后一行，报的就是最后一行的行号。"""
        lines = self.repo.target.read_text(encoding="utf-8").splitlines(keepends=True)
        line_no = len(lines) + 1                 # 追加在末尾
        self._append_and_scan(VIOLATION_PY, line_no, "HOST_DB_CONNECT")


class TestHookRule(unittest.TestCase):
    """Q-01：hook 注册只在适配层；`ctx.rest(` 放行。"""

    def test_each_hook_token_flagged_outside_adapter(self):
        for label, snippet in HOOK_VIOLATIONS:
            with self.subTest(label):
                repo = _FakeRepo(_clean_file())
                try:
                    repo.insert_line(snippet, 3)
                    out = repo.scan_cli()
                    self.assertEqual(out.returncode, 1, out.stderr)
                    findings = json.loads(out.stdout)
                    self.assertEqual(len(findings), 1, findings)
                    self.assertEqual(findings[0]["kind"], "HOST_HOOK")
                    self.assertEqual(findings[0]["line"], 3)
                finally:
                    repo.cleanup()

    def test_ctx_rest_is_exempt(self):
        """`ctx.rest(` 一律放行 —— 它是本插件自己的对外接口。"""
        repo = _FakeRepo(EXEMPT_PY + EXEMPT_PY)
        try:
            out = repo.scan_cli()
            self.assertEqual(out.returncode, 0, f"ctx.rest 被误报：{out.stdout}")
            self.assertEqual(json.loads(out.stdout), [])
        finally:
            repo.cleanup()

    def test_hook_inside_adapter_file_is_exempt(self):
        """适配层本体里写 hook 注册是应当的，不算违规。"""
        repo = _FakeRepo(_clean_file())
        try:
            out = repo.scan_cli()
            self.assertEqual(out.returncode, 0, out.stdout)
        finally:
            repo.cleanup()


class TestCleanPasses(unittest.TestCase):
    """反向：删掉违规 → 退出码 0。没有这一半，把扫描器改成永远返回 1 也能过测。"""

    def test_clean_repo_exits_zero(self):
        repo = _FakeRepo(
            '"""干净模块"""\n'
            "import sys\n"
            'r = ctx.rest("/overview")\n'
            "def handler(ctx):\n"
            "    return r\n")
        try:
            out = repo.scan_cli()
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(json.loads(out.stdout), [])
        finally:
            repo.cleanup()

    def test_remove_violation_turns_red_green(self):
        """同一个目录：先红（有违规）→ 删掉违规 → 绿（退出码 0）。"""
        repo = _FakeRepo(VIOLATION_PY + _clean_file())
        try:
            self.assertEqual(repo.scan_cli().returncode, 1)
            repo.target.write_text(_clean_file(), encoding="utf-8")
            out = repo.scan_cli()
            self.assertEqual(out.returncode, 0, out.stdout)
            self.assertEqual(json.loads(out.stdout), [])
        finally:
            repo.cleanup()

    def test_real_repo_is_clean(self):
        """仓库本体必须干净（不是靠扫空目录通过）。"""
        out = subprocess.run([sys.executable, str(SCANNER), "--json"],
                             capture_output=True, text=True, encoding="utf-8", cwd=str(REPO))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


class TestScannerDoesNotOverreach(unittest.TestCase):
    """扫描器的三个易错点：文档串/注释提及不算；近似名不算；豁免是按路径做的。"""

    def test_docstring_mentioning_hook_is_not_a_violation(self):
        text = 'def f():\n    """这里提到 on_session_start 只是文档，不是注册。"""\n    return 1\n'
        self.assertEqual(cb.scan_text(text, "x.py", ".py"), [])

    def test_comment_mentioning_hook_is_not_a_violation(self):
        text = "# 不要在这里 register_hook，归适配层\nVALUE = 1\n"
        self.assertEqual(cb.scan_text(text, "x.py", ".py"), [])

    def test_similar_names_are_not_flagged(self):
        text = ("def my_register_hook_impl():\n    pass\n"
                "def _on_session_started():\n    pass\n"
                'r = ctx.restful("/x")\n')
        self.assertEqual(cb.scan_text(text, "x.py", ".py"), [])

    def test_scan_text_alone_flags_hook_even_for_adapter_path(self):
        """scan_text 不认识「谁是适配层」——豁免是 scan() 按路径做的，这里锁死该分工。"""
        findings = cb.scan_text("def on_session_start(ctx):\n", "dashboard/host_adapter.py", ".py")
        self.assertEqual([f.kind for f in findings], ["HOST_HOOK"])


if __name__ == "__main__":
    unittest.main()
