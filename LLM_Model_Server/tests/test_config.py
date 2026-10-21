import json
import sys
import tempfile
import unittest
from pathlib import Path

from local_llm_server.config import load_config, engine_command, engine_environment


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'base').mkdir()
        (self.root / 'adapter').mkdir()
        (self.root / 'base/config.json').write_text('{"model_type":"gemma4"}')
        (self.root / 'base/model.safetensors').touch()
        (self.root / 'adapter/adapter_config.json').write_text(json.dumps({'r':16}))
        (self.root / 'adapter/adapter_model.safetensors').touch()
        self.path = self.root / 'server.toml'
        self.text = '''model_path = "base"
adapter_path = "adapter"
model_name = "planner"
base_model_name = "base"
host = "127.0.0.1"
port = 18520
python_path = "python"
run_dir = "run"
log_dir = "logs"
gpu_memory_utilization = 0.4
max_model_len = 4096
max_num_seqs = 1
max_lora_rank = 16
min_available_ram_gb = 6.0
startup_timeout_s = 300.0
'''
        self.path.write_text(self.text)

    def test_local_paths_and_adapter_are_passed_to_runtime(self):
        config = load_config(self.path)
        cmd = engine_command(config)
        self.assertEqual(cmd[:4], [str(self.root/'python'), '-m', 'vllm.entrypoints.cli.main', 'serve'])
        self.assertEqual(cmd[4], str(self.root/'base'))
        self.assertEqual(cmd[cmd.index('--lora-modules')+1], 'planner='+str(self.root/'adapter'))
        self.assertEqual(cmd[cmd.index('--served-model-name')+1], 'base')
        self.assertEqual(cmd[cmd.index('--gpu-memory-utilization')+1], '0.4')
        self.assertEqual(cmd[cmd.index('--max-num-seqs')+1], '1')
        self.assertIn('--enable-lora', cmd)
        self.assertIn('--enforce-eager', cmd)
        env = engine_environment(config)
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')
        self.assertEqual(env['TRANSFORMERS_OFFLINE'], '1')
        self.assertEqual(env['VLLM_WORKER_MULTIPROC_METHOD'], 'spawn')

    def test_text_architecture_is_explicit_for_gemma4_lora(self):
        cmd = engine_command(load_config(self.path))
        self.assertEqual(json.loads(cmd[cmd.index('--hf-overrides')+1]),
                         {'architectures':['Gemma4ForCausalLM']})

    def test_unsupported_lora_rank_is_rejected(self):
        self.path.write_text(self.text.replace('max_lora_rank = 16','max_lora_rank = 17'))
        with self.assertRaisesRegex(ValueError,'rank'): load_config(self.path)

    def test_different_model_architecture_is_not_silently_overridden(self):
        (self.root/'base/config.json').write_text('{"model_type":"qwen2"}')
        cmd = engine_command(load_config(self.path))
        self.assertNotIn('--hf-overrides',cmd)

    def test_json_decoder_cannot_spend_output_budget_on_whitespace(self):
        cmd = engine_command(load_config(self.path))
        self.assertEqual(json.loads(cmd[cmd.index('--structured-outputs-config')+1]),
                         {'backend':'xgrammar','disable_any_whitespace':True})

    def test_missing_adapter_cannot_silently_use_base(self):
        (self.root/'adapter/adapter_model.safetensors').unlink()
        with self.assertRaisesRegex(ValueError, 'adapter'):
            load_config(self.path)

    def test_venv_interpreter_symlink_preserves_the_environment(self):
        (self.root/'python').symlink_to(sys.executable)
        config = load_config(self.path)
        self.assertEqual(engine_command(config)[0],str(self.root/'python'))

    def test_rank_exceeding_runtime_limit_is_rejected(self):
        (self.root/'adapter/adapter_config.json').write_text('{"r":32}')
        with self.assertRaisesRegex(ValueError, 'rank'):
            load_config(self.path)

    def test_bad_numbers_and_nonlocal_bind_are_rejected(self):
        for before, after in [('port = 18520','port = true'),
                              ('max_num_seqs = 1','max_num_seqs = 0'),
                              ('gpu_memory_utilization = 0.4','gpu_memory_utilization = 1.2'),
                              ('host = "127.0.0.1"','host = "0.0.0.0"'),
                              ('startup_timeout_s = 300.0','startup_timeout_s = nan')]:
            with self.subTest(after=after):
                self.path.write_text(self.text.replace(before,after))
                with self.assertRaises(ValueError): load_config(self.path)

    def test_unknown_config_field_is_not_ignored(self):
        self.path.write_text(self.text+'typo_port = 18521\n')
        with self.assertRaisesRegex(ValueError,'unknown'):
            load_config(self.path)


if __name__ == '__main__':
    unittest.main()
