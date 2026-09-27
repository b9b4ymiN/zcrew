from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctx = _load("context_report")
zcrew = _load("zcrew")

SENTINEL = "SENTINEL-SECRET-TEXT-9f3a"


def _usage(inp: int, read: int, create: int, out: int) -> dict:
    return {
        "input_tokens": inp, "cache_read_input_tokens": read,
        "cache_creation_input_tokens": create, "output_tokens": out,
    }


def assistant(msg_id: str, usage: dict, blocks: list, ts: str = "2026-01-01T00:00:00Z") -> dict:
    return {"type": "assistant", "sessionId": "sess-1", "timestamp": ts,
            "message": {"id": msg_id, "role": "assistant", "usage": usage, "content": blocks}}


def tool_use(tid: str, name: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": {"command": SENTINEL}}


def result(tid: str, content, ts: str = "2026-01-01T00:00:01Z") -> dict:  # noqa: ANN001
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def write_jsonl(path: Path, entries: list, bad_lines: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
        for _ in range(bad_lines):
            fh.write("{not json\n")
    return path


def run_cli(argv: list[str], env: dict) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = zcrew.main(argv, env=env, paths=zcrew.resolve_paths(env={}))
    return code, buf.getvalue()


def sample_entries() -> list:
    return [
        # one API message split over two entries with the same id and usage
        assistant("m1", _usage(10, 1000, 90, 5), [{"type": "thinking", "thinking": SENTINEL}],
                  ts="2026-01-01T00:00:00Z"),
        assistant("m1", _usage(10, 1000, 90, 5), [tool_use("t1", "Bash")]),
        result("t1", SENTINEL + "x" * (4000 - len(SENTINEL))),  # 4000 chars
        assistant("m2", _usage(5, 2000, 0, 7), [tool_use("t2", "Read"), tool_use("t3", "mcp__srv__a")]),
        result("t2", [{"type": "text", "text": SENTINEL + "y" * (10000 - len(SENTINEL))},
                      {"type": "image", "source": {"data": "Zm9v" * 1000}}]),
        result("t3", [{"type": "text", "text": "ab"}, {"type": "text", "text": "cde"}]),
        assistant("m3", _usage(1, 1500, 0, 3), [tool_use("t4", "mcp__srv__b")],
                  ts="2026-01-01T01:30:00Z"),
        result("t4", "z" * 995),
        result("orphan", "q" * 10),
    ]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.claude_home = self.root / "claude"
        self.env = {"CLAUDE_CONFIG_DIR": str(self.claude_home), "CODEX_HOME": str(self.root / "codex")}
        self.project = str(self.root / "My Proj")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def project_dir(self, project: str | None = None) -> Path:
        return self.claude_home / "projects" / ctx.project_slug(project or self.project)


class SlugTests(unittest.TestCase):
    def test_windows_path(self) -> None:
        if os.name == "nt":
            self.assertEqual(ctx.project_slug(r"C:\Programing\Microchip\msom"), "C--Programing-Microchip-msom")

    def test_every_non_alnum_char_becomes_dash(self) -> None:
        slug = ctx.project_slug(os.path.join(os.path.abspath(os.sep), "a b", "proj_ไทย.x"))
        self.assertTrue(slug.endswith("a-b-proj-----x"), slug)
        self.assertRegex(slug, r"^[A-Za-z0-9-]+$")


class LocateTests(Base):
    def test_latest_session_is_default(self) -> None:
        old = write_jsonl(self.project_dir() / "old.jsonl", [])
        new = write_jsonl(self.project_dir() / "new.jsonl", [])
        past = time.time() - 100
        os.utime(old, (past, past))
        self.assertEqual(ctx.find_claude_session(self.project, None, self.env), new)

    def test_session_by_id_and_path(self) -> None:
        a = write_jsonl(self.project_dir() / "aaa.jsonl", [])
        write_jsonl(self.project_dir() / "bbb.jsonl", [])
        self.assertEqual(ctx.find_claude_session(self.project, "aaa", self.env), a)
        other = write_jsonl(self.root / "elsewhere" / "x.jsonl", [])
        self.assertEqual(ctx.find_claude_session(self.project, str(other), self.env), other)
        with self.assertRaises(ctx.ContextError):
            ctx.find_claude_session(self.project, "missing", self.env)

    def test_no_transcript_message(self) -> None:
        code, out = run_cli(["context", self.project], self.env)
        self.assertEqual(code, 1)
        self.assertIn("no Claude Code transcripts", out)


class ParseTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.path = write_jsonl(self.project_dir() / "s1.jsonl", sample_entries(), bad_lines=2)
        self.report = ctx.build_report(ctx.parse_claude(self.path), "claude", self.path)
        self.tools = {t["name"]: t for t in self.report["tools"]}

    def test_turns_dedupe_split_messages(self) -> None:
        self.assertEqual(self.report["turns"], 3)
        self.assertEqual(self.report["context_tokens"], {"first": 1100, "peak": 2005, "last": 1501})
        self.assertEqual(self.report["output_tokens"], 15)
        self.assertEqual(self.report["duration_s"], 5400)
        self.assertEqual(self.report["session_id"], "sess-1")

    def test_pairing_and_content_sizes(self) -> None:
        self.assertEqual(self.tools["Bash"]["chars"], 4000)
        self.assertEqual(self.tools["Read"]["chars"], 10000)
        self.assertEqual(self.tools["Read"]["images"], 1)
        self.assertEqual(self.tools["mcp__srv__a"]["chars"], 5)
        self.assertEqual(self.tools[ctx.UNKNOWN_TOOL]["chars"], 10)
        self.assertEqual(self.report["tool_output"]["chars"], 15010)
        self.assertEqual(self.report["tool_output"]["approx_tokens"], 15010 // 4)
        self.assertEqual(self.report["tools"][0]["name"], "Read")

    def test_mcp_grouping(self) -> None:
        self.assertEqual(self.report["mcp_servers"], [
            {"server": "srv", "calls": 2, "chars": 1000, "share": round(100 * 1000 / 15010, 1)},
        ])

    def test_bad_lines_skipped(self) -> None:
        self.assertEqual(self.report["lines_skipped"], 2)

    def test_top_limits_rows(self) -> None:
        report = ctx.build_report(ctx.parse_claude(self.path), "claude", self.path, top=2)
        self.assertEqual(len(report["tools"]), 2)
        self.assertEqual(report["tools_omitted"], 3)

    def test_output_never_contains_transcript_text(self) -> None:
        for extra in ([], ["--json"]):
            code, out = run_cli(["context", self.project, *extra], self.env)
            self.assertEqual(code, 0)
            self.assertNotIn(SENTINEL, out)
            self.assertNotIn("Zm9v", out)

    def test_json_schema(self) -> None:
        code, out = run_cli(["context", self.project, "--json", "--session", "s1"], self.env)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {
            "commander", "session_id", "start", "end", "duration_s", "turns", "context_tokens",
            "output_tokens", "tool_output", "tools", "tools_omitted", "mcp_servers", "lines_skipped", "hints",
        })
        self.assertEqual(set(data["tools"][0]), {"name", "calls", "chars", "avg", "share", "max", "images"})

    def test_human_output(self) -> None:
        code, out = run_cli(["context", self.project], self.env)
        self.assertEqual(code, 0)
        self.assertIn("peak 2,005", out)
        self.assertIn("MCP servers:", out)
        self.assertIn("srv: 2 calls", out)


def _hint_report(tools: list, peak: int = 1000) -> dict:
    return {"tools_all": tools, "context_tokens": {"first": 0, "peak": peak, "last": 0}}


def _row(name: str, calls: int, avg: int, share: float) -> dict:
    return {"name": name, "calls": calls, "avg": avg, "share": share}


class HintTests(unittest.TestCase):
    def test_no_hints_below_thresholds(self) -> None:
        self.assertEqual(ctx.hints_for(_hint_report([
            _row("Bash", 3, 100, 30.0), _row("Read", 2, 8000, 20.0), _row(ctx.WAIT_TOOL, 10, 10, 1.0),
        ], peak=150_000)), [])

    def test_hints_above_thresholds(self) -> None:
        hints = ctx.hints_for(_hint_report([
            _row("Bash", 3, 100, 20.0), _row("PowerShell", 1, 100, 10.5), _row("Read", 2, 8001, 20.0),
            _row(ctx.WAIT_TOOL, 11, 10, 1.0),
        ], peak=150_001))
        self.assertEqual(len(hints), 4)
        self.assertIn("redirect to a log file", hints[0])
        self.assertIn("read ranges", hints[1])
        self.assertIn("zcrew update", hints[2])
        self.assertIn("summarize", hints[3])


class CodexTests(Base):
    def rollout(self, name: str, cwd: str | None, subagent: bool = False) -> Path:
        meta = {"id": "cx-1", "cwd": cwd, "source": {"subagent": {}} if subagent else "cli"}
        if cwd is None:
            del meta["cwd"]
        usage1 = {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 10}
        usage2 = {"input_tokens": 3000, "cached_input_tokens": 900, "output_tokens": 20}
        entries = [
            {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": meta},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                                                  "input": SENTINEL}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": [
                {"type": "input_text", "text": SENTINEL + "a" * (100 - len(SENTINEL))}, {"type": "input_image", "image_url": "x"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "c2", "name": "codegraph_search",
                                                  "namespace": "mcp__codegraph", "arguments": "{}"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c2", "output": "b" * 20}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 1}, "last_token_usage": usage1}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 1}, "last_token_usage": usage1}}},
            {"timestamp": "2026-01-01T00:10:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 2}, "last_token_usage": usage2}}},
        ]
        return write_jsonl(self.root / "codex" / "sessions" / "2026" / name, entries)

    def test_match_by_cwd_skipping_subagents_and_unknown(self) -> None:
        mine = self.rollout("rollout-a.jsonl", self.project)
        past = time.time() - 100
        os.utime(mine, (past, past))
        self.rollout("rollout-b.jsonl", self.project, subagent=True)
        self.rollout("rollout-c.jsonl", None)
        self.rollout("rollout-d.jsonl", str(self.root / "other"))
        self.assertEqual(ctx.find_codex_session(self.project, None, self.env), mine)

    def test_codex_report(self) -> None:
        self.rollout("rollout-a.jsonl", self.project)
        code, out = run_cli(["context", self.project, "--commander", "codex", "--json"], self.env)
        self.assertEqual(code, 0)
        self.assertNotIn(SENTINEL, out)
        data = json.loads(out)
        self.assertEqual(data["turns"], 2)
        self.assertEqual(data["context_tokens"], {"first": 1000, "peak": 3000, "last": 3000})
        tools = {t["name"]: t for t in data["tools"]}
        self.assertEqual(tools["exec"]["chars"], 100)
        self.assertEqual(tools["exec"]["images"], 1)
        self.assertEqual(tools["mcp__codegraph__codegraph_search"]["chars"], 20)
        self.assertEqual(data["mcp_servers"][0]["server"], "codegraph")

    def test_codex_no_session(self) -> None:
        code, out = run_cli(["context", self.project, "--commander", "codex"], self.env)
        self.assertEqual(code, 1)
        self.assertIn("no Codex session", out)


if __name__ == "__main__":
    unittest.main()
