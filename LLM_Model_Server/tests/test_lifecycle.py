from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import signal
import time
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_llm_server.config import ServerConfig
from local_llm_server.lifecycle import start, status, stop, process_identity, preflight, _port_free


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port = s.getsockname()[1]
        self.config = ServerConfig(root,root,model_name='planner',base_model_name='base',
                                   python_path=Path(sys.executable),port=port,
                                   run_dir=root/'run',log_dir=root/'logs',
                                   startup_timeout_s=3,stop_timeout_s=2)
        self.fixture = Path(__file__).with_name('fixture_server.py')
        self.command = [sys.executable,str(self.fixture),'--port',str(port),'--name','planner']
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        if self.config.state_path.exists():
            try: stop(self.config)
            except RuntimeError: self.config.state_path.unlink()

    def launch(self,extra=()):
        with patch('local_llm_server.lifecycle.preflight',return_value={'ok':True}), \
             patch('local_llm_server.lifecycle.engine_command',return_value=self.command+list(extra)):
            return start(self.config)

    def test_independent_process_becomes_ready_and_stops(self):
        result = self.launch()
        self.assertEqual(result['status'],'ready')
        self.assertNotEqual(result['pid'],os.getpid())
        self.assertEqual(status(self.config)['status'],'ready')
        self.assertEqual(stop(self.config)['status'],'stopped')
        self.assertFalse(self.config.state_path.exists())
        self.assertEqual(status(self.config)['status'],'stopped')

    def test_stop_removes_worker_after_leader_exits(self):
        child_file = Path(self.temp.name)/'worker.pid'
        self.launch(['--stubborn-child',str(child_file)])
        child_pid = int(child_file.read_text())
        self.addCleanup(lambda: os.kill(child_pid,signal.SIGKILL) if process_identity(child_pid) else None)
        self.config = replace(self.config,stop_timeout_s=0.2)
        stop(self.config)
        end = time.monotonic()+2
        while process_identity(child_pid) and time.monotonic()<end: time.sleep(0.05)
        self.assertIsNone(process_identity(child_pid),'orphan worker survived managed stop')
        self.assertFalse(self.config.state_path.exists())

    def test_stop_can_recover_workers_after_unexpected_leader_exit(self):
        child_file = Path(self.temp.name)/'worker.pid'
        leader = self.launch(['--stubborn-child',str(child_file)])
        child_pid = int(child_file.read_text())
        self.addCleanup(lambda: os.kill(child_pid,signal.SIGKILL) if process_identity(child_pid) else None)
        os.kill(leader['pid'],signal.SIGKILL)
        end = time.monotonic()+2
        while process_identity(leader['pid']) and time.monotonic()<end: time.sleep(0.05)
        self.assertEqual(status(self.config)['status'],'orphan_workers')
        self.config = replace(self.config,stop_timeout_s=0.2)
        stop(self.config)
        self.assertIsNone(process_identity(child_pid))

    def test_hidden_cuda_device_is_rejected(self):
        with patch.dict(os.environ,{'CUDA_VISIBLE_DEVICES':''}), \
             patch('local_llm_server.lifecycle.resource_snapshot',return_value={
                 'available_ram_gb':10,'gpus':[{'index':0,'total_mib':48000,'free_mib':40000}]}), \
             patch('local_llm_server.lifecycle.runtime_version',return_value='0.19.1'):
            self.assertFalse(preflight(self.config)['ok'])

    def test_same_alias_different_runtime_or_model_is_not_reported_ready(self):
        self.launch()
        for config in [replace(self.config,runtime='llamacpp'),
                       replace(self.config,model_path=Path(self.temp.name)/'different-model')]:
            with self.subTest(config=config):
                info=status(config)
                self.assertEqual(info['status'],'config_changed')
                self.assertEqual(info['running_runtime'],'vllm')

    def test_duplicate_start_keeps_original_process(self):
        first = self.launch()
        with patch('local_llm_server.lifecycle.preflight',return_value={'ok':True}):
            with self.assertRaisesRegex(RuntimeError,'already'):
                start(self.config)
        self.assertEqual(status(self.config)['pid'],first['pid'])

    def test_changed_engine_settings_are_not_reported_ready(self):
        self.launch()
        for config in [replace(self.config,max_model_len=8192),
                       replace(self.config,python_path=Path('/different/python'))]:
            with self.subTest(config=config):
                self.assertEqual(status(config)['status'],'config_changed')

    def test_failed_start_preserves_log_and_releases_state(self):
        with self.assertRaisesRegex(RuntimeError,'exited'):
            self.launch(['--fail'])
        self.assertIn('intentional fixture startup failure',self.config.log_path.read_text())
        self.assertFalse(self.config.state_path.exists())

    def test_wrong_model_is_not_ready_and_timeout_cleans_up(self):
        self.config = replace(self.config,startup_timeout_s=0.5)
        with self.assertRaisesRegex(RuntimeError,'ready'):
            self.launch(['--wrong-model'])
        self.assertFalse(self.config.state_path.exists())

    def test_changed_process_identity_cannot_be_signalled(self):
        original = self.launch()
        record = json.loads(self.config.state_path.read_text())
        record['start_ticks'] = '0'
        self.config.state_path.write_text(json.dumps(record))
        try:
            with self.assertRaisesRegex(RuntimeError,'identity'):
                stop(self.config)
            self.assertIsNotNone(process_identity(original['pid']))
        finally:
            record['start_ticks'] = process_identity(original['pid'])['start_ticks']
            self.config.state_path.write_text(json.dumps(record))

    def test_busy_port_is_not_taken_over(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',self.config.port))
            sock.listen()
            with self.assertRaisesRegex(RuntimeError,'port'):
                self.launch()
        self.assertFalse(self.config.state_path.exists())

    def test_closed_http_connection_does_not_block_runtime_switch(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            listener.bind(('127.0.0.1',self.config.port));listener.listen()
            client.connect(('127.0.0.1',self.config.port))
            connection,_=listener.accept()
            connection.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(1),b'')
            client.close();connection.close()
        self.assertTrue(_port_free(self.config),'TIME_WAIT connection blocks immediate restart')

    def test_resource_failure_prevents_start(self):
        with patch('local_llm_server.lifecycle.resource_snapshot',return_value={
                'available_ram_gb':1.0,'gpus':[]}), \
             patch('local_llm_server.lifecycle.runtime_version',return_value='0.19.1'):
            report = preflight(self.config)
        self.assertFalse(report['ok'])
        self.assertTrue(any('RAM' in s for s in report['errors']))

    def test_cli_can_stop_when_model_files_are_no_longer_available(self):
        from local_llm_server.cli import main
        self.launch()
        path = Path(self.temp.name)/'server.toml'
        path.write_text(f'''model_path = "missing-base"
adapter_path = "missing-adapter"
model_name = "planner"
base_model_name = "base"
port = {self.config.port}
python_path = "{sys.executable}"
run_dir = "run"
log_dir = "logs"
''')
        with redirect_stdout(io.StringIO()):
            code = main(['--config',str(path),'stop'])
        self.assertEqual(code,0)
        self.assertFalse(self.config.state_path.exists())


if __name__=='__main__': unittest.main()
