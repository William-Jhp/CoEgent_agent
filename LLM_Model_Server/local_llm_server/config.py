"""Validate preselected runtime/model profiles; no model imports or downloads."""
from dataclasses import dataclass, fields
import json
import math
import os
from pathlib import Path
import re
import struct
import tomllib

from .runtimes import engine_command, engine_environment

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ServerConfig:
    model_path: Path
    adapter_path: Path | None = None
    runtime: str = 'vllm'
    model_name: str = 'coegent-planner'
    base_model_name: str = 'coegent-base'
    host: str = '127.0.0.1'
    port: int = 18520
    python_path: Path = Path('.venv/bin/python')
    llamacpp_path: Path = ROOT/'runtimes/llamacpp/bin/llama-server'
    chat_template_path: Path | None = None
    run_dir: Path = Path('run')
    log_dir: Path = Path('logs')
    gpu_memory_utilization: float = 0.4
    gpu_layers: int = -1
    threads: int = 8
    min_available_vram_gb: float = 4.0
    max_model_len: int = 4096
    max_num_seqs: int = 1
    max_lora_rank: int = 16
    min_available_ram_gb: float = 6.0
    startup_timeout_s: float = 300.0
    request_timeout_s: float = 90.0
    stop_timeout_s: float = 20.0

    @property
    def endpoint(self):
        return f'http://{self.host}:{self.port}'

    @property
    def state_path(self):
        return self.run_dir/'server.json'

    @property
    def log_path(self):
        return self.log_dir/'server.log'

    @property
    def selection(self):
        return {'runtime':self.runtime,'model_path':str(self.model_path),
                'adapter_path':str(self.adapter_path) if self.adapter_path is not None else None,
                'model':self.model_name,'endpoint':self.endpoint}

    @property
    def engine_settings(self):
        settings={'max_model_len':self.max_model_len,'max_num_seqs':self.max_num_seqs}
        if self.runtime=='llamacpp':
            settings.update(executable=str(self.llamacpp_path),gpu_layers=self.gpu_layers,
                            threads=self.threads,chat_template_path=(str(self.chat_template_path)
                            if self.chat_template_path is not None else None))
        else:
            settings.update(executable=str(self.python_path),base_model_name=self.base_model_name,
                            gpu_memory_utilization=self.gpu_memory_utilization,
                            max_lora_rank=self.max_lora_rank)
        return settings

    @property
    def planner_settings(self):
        return {'endpoint':self.endpoint,'model':self.model_name,
                'timeout_s':self.request_timeout_s,'constrained':True}


def profile_path(runtime=None):
    if runtime not in (None,'vllm','llamacpp'):
        raise ValueError('runtime must be vllm or llamacpp')
    return ROOT/'config'/('llamacpp.toml' if runtime=='llamacpp' else 'server.toml')


def _resolve(directory,value,key):
    if not isinstance(value,(str,Path)) or not str(value):
        raise ValueError(f'{key} must be a nonempty path')
    candidate=directory/Path(value).expanduser()
    return Path(os.path.abspath(candidate)) if key=='python_path' else candidate.resolve()


def _gguf(path,label):
    try:
        with path.open('rb') as file:
            header=file.read(24)
        if (path.suffix.lower()!='.gguf' or len(header)!=24 or header[:4]!=b'GGUF'
                or struct.unpack('<I',header[4:8])[0] not in (2,3)):
            raise ValueError('unsupported header or format')
    except (OSError,ValueError) as exc:
        raise ValueError(f'{label} must be an existing GGUF file: {path}: {exc}') from exc


def load_config(path, *, validate_weights=True, overrides=None):
    path=Path(path).resolve()
    try:
        data=tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:
        raise ValueError(f'cannot read config: {exc}') from exc
    overrides=dict(overrides or {})
    known={f.name for f in fields(ServerConfig)}
    if (set(data)|set(overrides))-known:
        raise ValueError(f'unknown config fields: {sorted((set(data)|set(overrides))-known)}')
    if 'model_path' not in data and 'model_path' not in overrides:
        raise ValueError('model_path is required')
    if data.get('adapter_path') is not None and 'model_path' in overrides and 'adapter_path' not in overrides:
        if 'model_path' not in data:
            raise ValueError('selecting model requires explicit adapter_path or --no-adapter')
        previous=_resolve(path.parent,data['model_path'],'model_path')
        selected=_resolve(path.parent,overrides['model_path'],'model_path')
        if previous!=selected:
            raise ValueError('changing model requires explicit adapter_path or --no-adapter')
    data.update(overrides)
    defaults={'python_path':'../.venv/bin/python','llamacpp_path':'../runtimes/llamacpp/bin/llama-server',
              'run_dir':'../run','log_dir':'../logs'}
    for key in ('model_path','adapter_path','python_path','llamacpp_path','chat_template_path','run_dir','log_dir'):
        value=data.get(key,defaults.get(key))
        if value is None and key in ('adapter_path','chat_template_path'):
            data[key]=None
        else:
            data[key]=_resolve(path.parent,value,key)
    config=ServerConfig(**data)
    if config.runtime not in ('vllm','llamacpp'):
        raise ValueError('runtime must be vllm or llamacpp')
    if config.host not in ('127.0.0.1','localhost'):
        raise ValueError('host must be loopback: 127.0.0.1 or localhost')
    for key,low,high in [('port',1,65535),('max_model_len',128,131072),('max_num_seqs',1,64),
                         ('max_lora_rank',1,512),('threads',1,1024),('gpu_layers',-1,1000)]:
        value=getattr(config,key)
        if type(value) is not int or not low<=value<=high:
            raise ValueError(f'{key} must be an integer in {low}..{high}')
    for key in ('gpu_memory_utilization','min_available_ram_gb','min_available_vram_gb',
                'startup_timeout_s','request_timeout_s','stop_timeout_s'):
        value=getattr(config,key)
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ValueError(f'{key} must be a positive finite number')
    if config.runtime=='vllm' and config.max_lora_rank not in {1,8,16,32,64,128,256,320,512}:
        raise ValueError('max_lora_rank is not supported by the pinned runtime')
    if config.gpu_memory_utilization>0.9:
        raise ValueError('gpu_memory_utilization must be <= 0.9')
    for key in ('model_name','base_model_name'):
        value=getattr(config,key)
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value):
            raise ValueError(f'invalid {key}')
    if config.runtime=='vllm' and config.adapter_path is not None and config.model_name==config.base_model_name:
        raise ValueError('adapter and base model names must differ')
    if not validate_weights:
        return config
    if config.runtime=='llamacpp':
        _gguf(config.model_path,'model')
        if config.adapter_path is not None:
            _gguf(config.adapter_path,'adapter')
        if config.chat_template_path is not None and not config.chat_template_path.is_file():
            raise ValueError('chat_template_path does not exist')
        if not config.llamacpp_path.is_file() or not os.access(config.llamacpp_path,os.X_OK):
            raise ValueError('llama.cpp executable missing; run scripts/setup_llamacpp.sh')
    else:
        if not (config.model_path/'config.json').is_file() or not any(config.model_path.glob('*.safetensors')):
            raise ValueError(f'model checkpoint missing: {config.model_path}')
        try:
            model=json.loads((config.model_path/'config.json').read_text())
            if not isinstance(model.get('model_type'),str) or not model['model_type']:
                raise ValueError('HF model_type is required')
        except (OSError,ValueError,AttributeError) as exc:
            raise ValueError(f'invalid HF model config: {exc}') from exc
        if config.adapter_path is not None:
            if not (config.adapter_path/'adapter_model.safetensors').is_file():
                raise ValueError(f'adapter weights missing: {config.adapter_path}')
            try:
                rank=json.loads((config.adapter_path/'adapter_config.json').read_text())['r']
            except (OSError,ValueError,KeyError,TypeError) as exc:
                raise ValueError(f'invalid adapter config: {exc}') from exc
            if type(rank) is not int or rank<=0 or rank>config.max_lora_rank:
                raise ValueError('adapter rank exceeds max_lora_rank or is invalid')
    return config
