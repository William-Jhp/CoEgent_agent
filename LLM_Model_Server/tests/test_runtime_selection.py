import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from local_llm_server.config import load_config, engine_command, engine_environment, ServerConfig
from local_llm_server.lifecycle import preflight


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.gguf=self.root/'model.gguf'
        self.gguf.write_bytes(b'GGUF'+struct.pack('<IQQ',3,0,0))
        self.binary=self.root/'runtime/bin/llama-server';self.binary.parent.mkdir(parents=True)
        self.binary.write_text('#!/bin/sh\nexit 0\n');self.binary.chmod(0o755)
        self.config=self.root/'server.toml'
        self.text='''runtime = "llamacpp"
model_path = "model.gguf"
model_name = "gguf-planner"
llamacpp_path = "runtime/bin/llama-server"
run_dir = "run"
log_dir = "logs"
gpu_layers = -1
threads = 4
max_model_len = 4096
max_num_seqs = 1
min_available_ram_gb = 4.0
min_available_vram_gb = 3.0
'''
        self.config.write_text(self.text)

    def test_gguf_launch_is_a_native_server_with_selected_alias(self):
        cfg=load_config(self.config);cmd=engine_command(cfg)
        self.assertEqual(cmd[0],str(self.binary))
        self.assertEqual(cmd[cmd.index('--model')+1],str(self.gguf))
        self.assertEqual(cmd[cmd.index('--alias')+1],'gguf-planner')
        self.assertEqual(cmd[cmd.index('--gpu-layers')+1],'all')
        self.assertIn('--jinja',cmd);self.assertIn('--no-webui',cmd)
        self.assertNotIn('--enable-lora',cmd)
        self.assertEqual(cfg.runtime,'llamacpp');self.assertIsNone(cfg.adapter_path)
        self.assertTrue(engine_environment(cfg)['LD_LIBRARY_PATH'].startswith(str(self.binary.parent)))

    def test_context_size_accounts_for_parallel_slots(self):
        cfg=replace(load_config(self.config),max_num_seqs=2)
        cmd=engine_command(cfg)
        self.assertEqual(cmd[cmd.index('--ctx-size')+1],'8192')
        self.assertEqual(cmd[cmd.index('--parallel')+1],'2')

    def test_gguf_adapter_is_passed_explicitly(self):
        adapter=self.root/'adapter.gguf';adapter.write_bytes(self.gguf.read_bytes())
        self.config.write_text(self.text+'adapter_path = "adapter.gguf"\n')
        cmd=engine_command(load_config(self.config))
        self.assertEqual(cmd[cmd.index('--lora')+1],str(adapter))

    def test_hf_adapter_cannot_be_silently_applied_to_gguf(self):
        (self.root/'adapter').mkdir()
        self.config.write_text(self.text+'adapter_path = "adapter"\n')
        with self.assertRaisesRegex(ValueError,'GGUF'): load_config(self.config)

    def test_wrong_model_and_unsupported_runtime_are_rejected(self):
        for text in [self.text.replace('llamacpp','unknown',1),
                     self.text.replace('model.gguf','missing.gguf'),
                     self.text.replace('gpu_layers = -1','gpu_layers = true')]:
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(ValueError): load_config(self.config)

    def test_invalid_gguf_magic_is_rejected(self):
        self.gguf.write_bytes(b'not a GGUF checkpoint')
        with self.assertRaisesRegex(ValueError,'GGUF'): load_config(self.config)

    def test_model_and_alias_overrides_are_resolved_from_config_directory(self):
        cfg=load_config(self.config,overrides={'model_path':'model.gguf','model_name':'selected'})
        self.assertEqual(cfg.model_path,self.gguf);self.assertEqual(cfg.model_name,'selected')

    def test_cpu_only_preflight_does_not_require_vllm_or_gpu(self):
        cfg=replace(load_config(self.config),gpu_layers=0)
        with patch('local_llm_server.lifecycle.resource_snapshot',return_value={'available_ram_gb':10,'gpus':[]}), \
             patch('local_llm_server.lifecycle.runtime_version',return_value='llama.cpp-test'):
            self.assertTrue(preflight(cfg)['ok'])

    def test_gpu_mode_requires_a_visible_gpu_and_configured_margin(self):
        cfg=load_config(self.config)
        with patch('local_llm_server.lifecycle.resource_snapshot',return_value={'available_ram_gb':10,'gpus':[]}), \
             patch('local_llm_server.lifecycle.runtime_version',return_value='llama.cpp-test'):
            self.assertFalse(preflight(cfg)['ok'])

    def test_cli_selects_runtime_profile_and_model_before_launch(self):
        from local_llm_server.cli import main
        output=io.StringIO()
        with patch('local_llm_server.cli.profile_path',return_value=self.config,create=True), redirect_stdout(output):
            code=main(['--runtime','llamacpp','--model-name','selected','command'])
        self.assertEqual(code,0)
        data=json.loads(output.getvalue())
        self.assertEqual(data['runtime'],'llamacpp')
        self.assertEqual(data['model'],'selected')
        self.assertEqual(data['argv'][0],str(self.binary))

    def test_client_config_exposes_http_planner_constructor_settings(self):
        from local_llm_server.cli import main
        output=io.StringIO()
        with redirect_stdout(output):
            code=main(['--config',str(self.config),'client-config'])
        self.assertEqual(code,0)
        data=json.loads(output.getvalue())
        self.assertEqual(data['planner_class'],'ModelServerPlanner')
        self.assertEqual(data['planner_settings']['model'],'gguf-planner')
        self.assertEqual(data['selection']['runtime'],'llamacpp')

    def test_model_override_requires_explicit_adapter_decision(self):
        base=self.root/'base';base.mkdir();other=self.root/'other';other.mkdir()
        for path in [base,other]:
            (path/'config.json').write_text('{"model_type":"gemma4"}');(path/'model.safetensors').touch()
        adapter=self.root/'peft';adapter.mkdir()
        (adapter/'adapter_config.json').write_text('{"r":16}')
        (adapter/'adapter_model.safetensors').touch()
        self.config.write_text('model_path="base"\nadapter_path="peft"\n')
        with self.assertRaisesRegex(ValueError,'adapter'):
            load_config(self.config,overrides={'model_path':'other'})
        cfg=load_config(self.config,overrides={'model_path':'other','adapter_path':None})
        self.assertIsNone(cfg.adapter_path)
        cmd=engine_command(cfg)
        self.assertNotIn('--enable-lora',cmd)
        self.assertEqual(cmd[cmd.index('--served-model-name')+1],cfg.model_name)

    def test_adapter_only_preset_requires_explicit_adapter_decision(self):
        self.config.write_text('adapter_path="peft"\n')
        with self.assertRaisesRegex(ValueError,'adapter'):
            load_config(self.config,validate_weights=False,overrides={'model_path':'model'})
        cfg=load_config(self.config,validate_weights=False,
                        overrides={'model_path':'model','adapter_path':None})
        self.assertIsNone(cfg.adapter_path)

    def test_shell_entrypoint_preserves_caller_relative_paths(self):
        script=Path(__file__).resolve().parents[1]/'scripts/server.sh'
        result=subprocess.run(['bash',str(script),'--config','server.toml',
                               '--model-path','model.gguf','command'],cwd=self.root,
                              env={**os.environ,'SERVER_PYTHON':sys.executable},
                              capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['model_path'],str(self.gguf))


if __name__=='__main__': unittest.main()
