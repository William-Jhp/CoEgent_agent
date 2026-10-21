"""Construct native runtime commands without importing an inference framework."""
import json
import os


def engine_command(config):
    if config.runtime == 'llamacpp':
        command = [str(config.llamacpp_path),'--model',str(config.model_path),
                   '--alias',config.model_name,'--host',config.host,'--port',str(config.port),
                   '--ctx-size',str(config.max_model_len*config.max_num_seqs),
                   '--parallel',str(config.max_num_seqs),'--threads',str(config.threads),
                   '--gpu-layers','all' if config.gpu_layers == -1 else str(config.gpu_layers),
                   '--jinja','--no-webui']
        if config.adapter_path is not None:
            command += ['--lora',str(config.adapter_path)]
        if config.chat_template_path is not None:
            command += ['--chat-template-file',str(config.chat_template_path)]
        return command
    alias = config.base_model_name if config.adapter_path is not None else config.model_name
    command = [str(config.python_path),'-m','vllm.entrypoints.cli.main','serve',
               str(config.model_path),'--served-model-name',alias,
               '--host',config.host,'--port',str(config.port),'--dtype','bfloat16',
               '--tensor-parallel-size','1','--max-model-len',str(config.max_model_len),
               '--max-num-seqs',str(config.max_num_seqs),
               '--max-num-batched-tokens',str(config.max_model_len),
               '--gpu-memory-utilization',str(config.gpu_memory_utilization)]
    model_type = json.loads((config.model_path/'config.json').read_text()).get('model_type')
    if model_type == 'gemma4':
        command += ['--hf-overrides','{"architectures":["Gemma4ForCausalLM"]}',
                    '--limit-mm-per-prompt','{"image":0,"audio":0,"video":0}']
    if config.adapter_path is not None:
        command += ['--enable-lora','--lora-modules',f'{config.model_name}={config.adapter_path}',
                    '--max-loras','1','--max-lora-rank',str(config.max_lora_rank)]
    return command+['--structured-outputs-config',
                   '{"backend":"xgrammar","disable_any_whitespace":true}',
                   '--generation-config','vllm','--enforce-eager']


def engine_environment(config):
    environment = {**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
                   'HF_HUB_DISABLE_TELEMETRY':'1','PYTHONUNBUFFERED':'1'}
    if config.runtime == 'llamacpp':
        directory = str(config.llamacpp_path.parent)
        environment['LD_LIBRARY_PATH'] = directory+(
            os.pathsep+environment['LD_LIBRARY_PATH'] if environment.get('LD_LIBRARY_PATH') else '')
    else:
        environment.update({'VLLM_WORKER_MULTIPROC_METHOD':'spawn',
                            'TOKENIZERS_PARALLELISM':'false','VLLM_NO_USAGE_STATS':'1'})
    return environment
