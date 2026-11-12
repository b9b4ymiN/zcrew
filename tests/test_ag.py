"""Offline tests for the optional Antigravity ACP adapter (no Google auth/model calls)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'ag.py'
spec = importlib.util.spec_from_file_location('zcrew_ag_test', SCRIPT)
assert spec and spec.loader
ag = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ag
spec.loader.exec_module(ag)


class FakeCompat:
    def __init__(self, root):
        self.root = root

    def state_dir(self, env):
        return self.root / 'gate'

    def record_files(self, env):
        return list((self.root / 'gate').glob('*.json'))

    def _read_json(self, path):
        return json.loads(path.read_text(encoding='utf-8'))

    def save_record(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')

    def parse_contract(self, text):
        return {'missing': [], 'scope': ['unit.py'], 'do_not': [], 'verify': [], 'sections': {}}

    def git_snapshot(self, repo):
        return {'base': 'abcd1234', 'dirty': []}


class AgRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'task.md').write_text('OBJECTIVE: Write tests', encoding='utf-8')
        self.compat = FakeCompat(self.root)
        self.stub = lambda name: self.compat if name == 'bridge_compat' else types.SimpleNamespace(matches_any=lambda p, scope: p in scope, max_rounds=lambda path, env: 4)
        self.patch = mock.patch.object(ag, 'sibling', side_effect=self.stub)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_run_and_correction_reuse_same_session(self):
        received = []
        async def fake_turn(agy, repo, prompt, session, mode, timeout, report, events, on_session):
            received.append(session)
            if on_session:
                on_session('session-77')
            report.write_text('done', encoding='utf-8')
            return 'session-77', 'end_turn', 0
        with mock.patch.object(ag, 'acp_turn', fake_turn):
            r = ag.main(['run', 'task.md', '--agy', 'agy.exe'], repo=self.repo, env={})
            self.assertEqual(r, 0)
            record_path, = self.compat.record_files({})
            record = self.compat._read_json(record_path)
            self.assertEqual(record['threadId'], 'session-77')
            self.assertEqual(record['antigravity']['status'], 'completed')
            self.assertEqual(received, [None])
            r = ag.main(['fix', record['runIds'][0], 'task.md', '--agy', 'agy.exe'], repo=self.repo, env={})
            self.assertEqual(r, 0)
            record = self.compat._read_json(record_path)
            self.assertEqual(record['rounds'], 2)
            self.assertEqual(len(record['runIds']), 2)
            self.assertEqual(received, [None, 'session-77'])

    def test_dirty_scope_blocks_dispatch(self):
        self.compat.git_snapshot = lambda path: {'base':'abc','dirty':['unit.py']}
        with mock.patch.object(ag, 'acp_turn') as call:
            result = ag.main(['run', 'task.md', '--agy', 'agy.exe'], repo=self.repo, env={})
        self.assertEqual(result, 2)
        call.assert_not_called()
        self.assertEqual(self.compat.record_files({}), [])

    def test_permission_denied_does_not_claim_success(self):
        async def fake_turn(agy, repo, prompt, session, mode, timeout, report, events, on_session):
            on_session('session-2')
            return 'session-2', 'end_turn', 1
        with mock.patch.object(ag, 'acp_turn', fake_turn):
            result = ag.main(['run', 'task.md', '--agy', 'agy.exe'], repo=self.repo, env={})
        self.assertEqual(result, 1)
        data = self.compat._read_json(self.compat.record_files({})[0])
        self.assertEqual(data['antigravity']['status'], 'blocked')

    def test_failed_session_retains_partial_record(self):
        async def fake_turn(agy, repo, prompt, session, mode, timeout, report, events, on_session):
            on_session('session-33')
            raise RuntimeError('stopped')
        with mock.patch.object(ag, 'acp_turn', fake_turn):
            result = ag.main(['run', 'task.md', '--agy', 'agy.exe'], repo=self.repo, env={})
        self.assertEqual(result, 2)
        data = self.compat._read_json(self.compat.record_files({})[0])
        self.assertEqual(data['antigravity']['status'], 'failed')
        self.assertEqual(data['threadId'], 'session-33')


class AcpInterfaceTests(unittest.TestCase):
    def test_permission_denied_and_events_reduced(self):
        async def go():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                cap = ag.CaptureClient(root/'report', root/'events')
                perm = await cap.request_permission('session-3', {'title':'dangerous edit'}, [])
                self.assertEqual(perm, {'outcome': {'outcome':'cancelled'}})
                await cap.session_update('session-3', {'sessionUpdate':'tool_call','title':'Read file','raw':'SECRET'})
                await cap.session_update('session-3', {'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'hello'}})
                self.assertNotIn('SECRET', (root/'events').read_text())
                self.assertIn('Permission denied', (root/'report').read_text())
                self.assertTrue((root/'report').read_text().endswith('hello'))
                self.assertEqual(cap.denied, 1)
        asyncio.run(go())

    def test_new_then_resume_uses_official_sdk(self):
        calls = []
        class Ctx:
            def __init__(self, client):
                self.client = client
            async def __aenter__(self):
                return Conn(self.client), object()
            async def __aexit__(self, *args):
                return False
        class Conn:
            def __init__(self, client): self.client = client
            async def initialize(self, **kw): calls.append(('init', kw))
            async def new_session(self, **kw):
                calls.append(('new', kw)); return types.SimpleNamespace(session_id='sess1')
            async def resume_session(self, **kw): calls.append(('resume', kw))
            async def set_session_mode(self, **kw): calls.append(('mode', kw))
            async def prompt(self, **kw):
                calls.append(('prompt', kw)); return types.SimpleNamespace(stop_reason='end_turn')
        fake = types.ModuleType('acp')
        fake.PROTOCOL_VERSION = 1
        fake.spawn_agent_process = lambda c, *argv: (calls.append(('spawn', argv)) or Ctx(c))
        fake.text_block = lambda text: {'type':'text','text':text}
        async def go():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with mock.patch.dict(sys.modules, {'acp':fake}):
                    sess, reason, denied = await ag.acp_turn('agy',root,'hello',None,'auto',30,root/'report',root/'events')
                    self.assertEqual((sess,reason,denied),('sess1','end_turn',0))
                    sess, reason, denied = await ag.acp_turn('agy',root,'fix','sess1','auto',30,root/'report',root/'events')
                self.assertEqual([c[0] for c in calls].count('new'),1)
                self.assertEqual([c[0] for c in calls].count('resume'),1)
                self.assertEqual([c[0] for c in calls].count('mode'),2)
                self.assertEqual(calls[0][1],('agy','acp','--sandbox','--disable-telemetry'))
        asyncio.run(go())


if __name__ == '__main__':
    unittest.main()