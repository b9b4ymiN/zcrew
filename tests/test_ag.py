"""Offline tests for the optional Antigravity ACP adapter (no Google auth/model calls)."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import io
import json
import os
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


def capture_main(argv, *, repo, env=None):
    """Run ag.main() with both streams captured so the suite prints nothing."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ag.main(argv, repo=repo, env={} if env is None else env)
    return code, out.getvalue(), err.getvalue()


def make_fake_sdk(spawn):
    """Fake `acp` + `acp.schema` pair exposing exactly the names load_sdk() needs."""
    schema = types.ModuleType('acp.schema')
    schema.TextContentBlock = lambda **kw: dict(kw)
    top = types.ModuleType('acp')
    top.PROTOCOL_VERSION = 1
    top.spawn_agent_process = spawn
    top.schema = schema
    return {'acp': top, 'acp.schema': schema}


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

    def write_record(self, name, *, status='blocked', cwd=None):
        record = {'runIds': [name], 'threadId': 'session-1',
                  'cwd': str(self.repo) if cwd is None else cwd,
                  'worker': 'antigravity',
                  'antigravity': {'status': status, 'sessionId': 'session-1', 'reports': []}}
        path = self.compat.state_dir({}) / f'{name}.json'
        self.compat.save_record(path, record)
        return record

    def test_run_and_correction_reuse_same_session(self):
        received = []
        async def fake_turn(agy, repo, prompt, session, mode, timeout, report, events, on_session):
            received.append(session)
            if on_session:
                on_session('session-77')
            report.write_text('done', encoding='utf-8')
            return 'session-77', 'end_turn', 0
        with mock.patch.object(ag, 'acp_turn', fake_turn):
            code, out, _ = capture_main(['run', 'task.md', '--agy', 'agy.exe'], repo=self.repo)
            self.assertEqual(code, 0)
            self.assertIn('status: completed', out.replace('\n', ' '))
            record_path, = self.compat.record_files({})
            record = self.compat._read_json(record_path)
            self.assertEqual(record['threadId'], 'session-77')
            self.assertEqual(record['antigravity']['status'], 'completed')
            self.assertEqual(received, [None])
            code, _, _ = capture_main(['fix', record['runIds'][0], 'task.md', '--agy', 'agy.exe'], repo=self.repo)
            self.assertEqual(code, 0)
            record = self.compat._read_json(record_path)
            self.assertEqual(record['rounds'], 2)
            self.assertEqual(len(record['runIds']), 2)
            self.assertEqual(received, [None, 'session-77'])

    def test_dirty_scope_blocks_dispatch(self):
        self.compat.git_snapshot = lambda path: {'base':'abc','dirty':['unit.py']}
        with mock.patch.object(ag, 'acp_turn') as call:
            code, _, _ = capture_main(['run', 'task.md', '--agy', 'agy.exe'], repo=self.repo)
        self.assertEqual(code, 2)
        call.assert_not_called()
        self.assertEqual(self.compat.record_files({}), [])

    def test_permission_denied_does_not_claim_success(self):
        async def fake_turn(agy, repo, prompt, session, mode, timeout, report, events, on_session):
            on_session('session-2')
            return 'session-2', 'end_turn', 1
        with mock.patch.object(ag, 'acp_turn', fake_turn):
            code, _, _ = capture_main(['run', 'task.md', '--agy', 'agy.exe'], repo=self.repo)
        self.assertEqual(code, 1)
        data = self.compat._read_json(self.compat.record_files({})[0])
        self.assertEqual(data['antigravity']['status'], 'blocked')

    def test_failed_session_retains_partial_record(self):
        async def fake_turn(agy, repo, prompt, session, mode, timeout, report, events, on_session):
            on_session('session-33')
            raise RuntimeError('stopped')
        with mock.patch.object(ag, 'acp_turn', fake_turn):
            code, _, _ = capture_main(['run', 'task.md', '--agy', 'agy.exe'], repo=self.repo)
        self.assertEqual(code, 2)
        data = self.compat._read_json(self.compat.record_files({})[0])
        self.assertEqual(data['antigravity']['status'], 'failed')
        self.assertEqual(data['threadId'], 'session-33')

    def test_gate_refuses_accept_on_non_completed_run(self):
        self.write_record('agy_1', status='blocked')
        for extra in (['--accept', 'looks fine'], ['--accept=looks fine']):
            with mock.patch.object(ag.subprocess, 'run') as run:
                code, _, err = capture_main(['gate', 'agy_1', *extra], repo=self.repo)
            self.assertEqual(code, 2)
            run.assert_not_called()
            self.assertIn('cannot accept', err)

    def test_gate_accept_passes_completed_run_through(self):
        self.write_record('agy_2', status='completed')
        with mock.patch.object(ag.subprocess, 'run') as run:
            run.return_value = types.SimpleNamespace(returncode=0)
            code, _, _ = capture_main(['gate', 'agy_2', '--accept=reviewed'], repo=self.repo)
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_find_run_never_matches_a_missing_cwd(self):
        self.write_record('agy_3', cwd='')
        with mock.patch.object(ag.subprocess, 'run') as run:
            code, _, err = capture_main(['gate', 'agy_3'], repo=self.repo)
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn('different repository', err)

    @unittest.skipUnless(os.name == 'nt', 'case-folded path matching is Windows behaviour')
    def test_repo_match_folds_case_on_windows(self):
        self.write_record('agy_4', cwd=str(self.repo).swapcase())
        _, record = ag.find_run(self.compat, {}, 'agy_4', self.repo)
        self.assertEqual(record['runIds'], ['agy_4'])

    def test_status_reports_matching_run(self):
        self.write_record('agy_7', status='blocked')
        code, out, _ = capture_main(['status'], repo=self.repo)
        self.assertEqual(code, 0)
        self.assertIn('agy_7', out)
        self.assertIn('blocked', out)

    def test_status_ignores_missing_and_foreign_cwd(self):
        self.write_record('agy_5', cwd='')
        self.write_record('agy_6', cwd=str(self.root / 'elsewhere'))
        code, out, _ = capture_main(['status'], repo=self.repo)
        self.assertEqual(code, 0)
        self.assertEqual(out, '')


class LoadSdkTests(unittest.TestCase):
    def test_reports_missing_when_acp_absent(self):
        with mock.patch.dict(sys.modules, {'acp': None, 'acp.schema': None}):
            with self.assertRaises(ag.AgError) as ctx:
                ag.load_sdk()
        message = str(ctx.exception)
        self.assertIn('missing optional dependency', message)
        self.assertIn('>=1.0,<2', message)

    def test_reports_incompatible_when_a_required_name_is_missing(self):
        schema = types.ModuleType('acp.schema')
        schema.TextContentBlock = lambda **kw: dict(kw)
        broken = types.ModuleType('acp')  # lacks PROTOCOL_VERSION / spawn_agent_process
        broken.schema = schema
        with mock.patch.dict(sys.modules, {'acp': broken, 'acp.schema': schema}):
            with self.assertRaises(ag.AgError) as ctx:
                ag.load_sdk()
        self.assertIn('incompatible', str(ctx.exception))
        self.assertIn('>=1.0,<2', str(ctx.exception))

    def test_reports_incompatible_when_version_out_of_range(self):
        for version in ('0.9.5', '2.0.0'):
            with self.subTest(version=version):
                modules = make_fake_sdk(lambda *args: None)
                with mock.patch.dict(sys.modules, modules), \
                        mock.patch.object(ag, '_sdk_version', lambda: version):
                    with self.assertRaises(ag.AgError) as ctx:
                        ag.load_sdk()
                self.assertIn('incompatible', str(ctx.exception))
                self.assertIn(version, str(ctx.exception))


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
        async def go():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                modules = make_fake_sdk(lambda c, *argv: (calls.append(('spawn', argv)) or Ctx(c)))
                with mock.patch.dict(sys.modules, modules), \
                        mock.patch.object(ag, '_sdk_version', lambda: '1.0.0'):
                    sess, reason, denied = await ag.acp_turn('agy', root, 'hello', None, 'auto', 30, root/'report', root/'events')
                    self.assertEqual((sess, reason, denied), ('sess1', 'end_turn', 0))
                    sess, reason, denied = await ag.acp_turn('agy', root, 'fix', 'sess1', 'auto', 30, root/'report', root/'events')
                self.assertEqual([c[0] for c in calls].count('new'), 1)
                self.assertEqual([c[0] for c in calls].count('resume'), 1)
                self.assertEqual([c[0] for c in calls].count('mode'), 2)
                self.assertEqual(calls[0][1], ('agy', 'acp', '--sandbox', '--disable-telemetry'))
                prompt_kwargs = next(c for c in calls if c[0] == 'prompt')[1]
                self.assertEqual(prompt_kwargs['prompt'], [{'type': 'text', 'text': 'hello'}])
        asyncio.run(go())


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        patch = mock.patch.object(ag.subprocess, 'run',
                                  return_value=types.SimpleNamespace(returncode=0))
        patch.start()
        self.addCleanup(patch.stop)

    def fake_spawn(self, init, session, init_error=None):
        spawned = []
        class Conn:
            async def initialize(self, **kw):
                if init_error:
                    raise init_error
                return init
            async def new_session(self, **kw):
                return session
        class Ctx:
            async def __aenter__(self):
                return Conn(), object()
            async def __aexit__(self, *args):
                return False
        def spawn(client, *argv):
            spawned.append(argv)
            return Ctx()
        return spawn, spawned

    def run_doctor_with(self, spawn, *extra, version='1.0.0'):
        modules = make_fake_sdk(spawn)
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(ag, '_sdk_version', lambda: version):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ag.main(['doctor', '--agy', 'agy.exe', *extra], repo=self.repo, env={})
        return code, out.getvalue(), err.getvalue()

    def test_success_prints_version_resume_modes_and_tier(self):
        init = {'agentInfo': {'version': '1.5.5'},
                'agentCapabilities': {'sessionCapabilities': {'resume': {}}}}
        session = {'session_id': 's1',
                   'modes': {'availableModes': [{'id': 'auto'}, {'id': 'default'},
                                                {'id': 'plan'}, {'id': 'accept-edits'}]},
                   '_meta': {'antigravity': {'tier': {'name': 'Google AI Pro'}}}}
        spawn, spawned = self.fake_spawn(init, session)
        code, out, _ = self.run_doctor_with(spawn)
        self.assertEqual(code, 0)
        self.assertEqual(spawned, [('agy.exe', 'acp', '--sandbox', '--disable-telemetry')])
        for expected in ('1.5.5', 'resume', 'auto', 'Google AI Pro', 'handshake OK'):
            self.assertIn(expected, out)

    def test_missing_resume_capability_fails(self):
        init = {'agentInfo': {'version': '1.5.5'},
                'agentCapabilities': {'sessionCapabilities': {}}}
        spawn, _ = self.fake_spawn(init, {'session_id': 's1', 'modes': {'availableModes': [{'id': 'auto'}]}})
        code, out, _ = self.run_doctor_with(spawn)
        self.assertEqual(code, 1)
        self.assertIn('NOT supported', out)

    def test_absent_resume_capability_fails(self):
        init = {'agentInfo': {'version': '1.5.5'}}
        spawn, _ = self.fake_spawn(init, {'session_id': 's1', 'modes': {'availableModes': [{'id': 'auto'}]}})
        code, out, _ = self.run_doctor_with(spawn)
        self.assertEqual(code, 1)
        self.assertIn('resume', out)

    def test_missing_auto_mode_fails(self):
        init = {'agentInfo': {'version': '1.5.5'},
                'agentCapabilities': {'sessionCapabilities': {'resume': {}}}}
        session = {'session_id': 's1',
                   'modes': {'availableModes': [{'id': 'default'}, {'id': 'plan'}]}}
        spawn, _ = self.fake_spawn(init, session)
        code, out, _ = self.run_doctor_with(spawn)
        self.assertEqual(code, 1)
        self.assertIn("'auto' not offered", out)

    def test_offline_skips_the_handshake(self):
        spawn, spawned = self.fake_spawn({}, {})
        code, out, _ = self.run_doctor_with(spawn, '--offline')
        self.assertEqual(code, 0)
        self.assertEqual(spawned, [])
        self.assertIn('--offline', out)

    def test_handshake_error_suggests_sign_in(self):
        spawn, _ = self.fake_spawn({}, {}, init_error=RuntimeError('request failed: 401 Unauthorized'))
        code, out, _ = self.run_doctor_with(spawn)
        self.assertEqual(code, 1)
        self.assertIn('sign in', out)


@unittest.skipUnless(importlib.util.find_spec('acp'), 'agent-client-protocol not installed')
class RealSdkContractTests(unittest.TestCase):
    """Assert the installed SDK really exposes the API surface ag.py calls.

    This is the test that would have caught `text_block` disappearing in 1.0.0.
    """

    def installed_version(self):
        from importlib.metadata import version
        return version('agent-client-protocol')

    def locate(self, name):
        import acp
        import pkgutil
        found = getattr(acp, name, None)
        if found is not None:
            return found
        for info in pkgutil.walk_packages(acp.__path__, prefix='acp.'):
            module = __import__(info.name, fromlist=[name])
            found = getattr(module, name, None)
            if found is not None:
                return found
        self.fail(f'agent-client-protocol {self.installed_version()} does not expose {name}')

    def test_connection_methods_accept_the_keywords_ag_passes(self):
        connection = self.locate('ClientSideConnection')
        expected = {
            'initialize': ('protocol_version',),
            'new_session': ('cwd', 'mcp_servers'),
            'resume_session': ('session_id', 'cwd', 'mcp_servers'),
            'set_session_mode': ('session_id', 'mode_id'),
            'prompt': ('session_id', 'prompt'),
            'cancel': ('session_id',),
        }
        for method, keywords in expected.items():
            with self.subTest(method=method):
                parameters = inspect.signature(getattr(connection, method)).parameters
                for keyword in keywords:
                    self.assertIn(keyword, parameters)

    def test_client_callback_parameters_match_capture_client(self):
        client = self.locate('Client')
        expected = {
            'request_permission': ('session_id', 'tool_call', 'options'),
            'session_update': ('session_id', 'update'),
        }
        for method, names in expected.items():
            with self.subTest(method=method):
                parameters = inspect.signature(getattr(client, method)).parameters
                for name in names:
                    self.assertIn(name, parameters)

    def test_module_names_blocks_and_version_range(self):
        from acp import PROTOCOL_VERSION, spawn_agent_process
        from acp.schema import NewSessionResponse, PromptResponse, SessionMode, TextContentBlock
        self.assertTrue(PROTOCOL_VERSION)
        parameters = inspect.signature(spawn_agent_process).parameters
        # ag.py passes the client and command positionally: (to_client, command, *args)
        self.assertEqual(list(parameters)[:2], ['to_client', 'command'])
        TextContentBlock(type='text', text='contract probe')
        self.assertIn('session_id', NewSessionResponse.model_fields)
        self.assertIn('stop_reason', PromptResponse.model_fields)
        self.assertIn('id', SessionMode.model_fields)
        self.assertTrue(ag._version_supported(self.installed_version()),
                        self.installed_version())


if __name__ == '__main__':
    unittest.main()
