"""Select a runtime/model profile and manage its independent HTTP server."""
import argparse
import json
from pathlib import Path
import sys

from .config import load_config, engine_command, profile_path
from .lifecycle import preflight, run_foreground, start, status, stop


def add_selection_arguments(parser):
    parser.add_argument('--config',type=Path,help='TOML preset; relative paths inside are relative to this file')
    parser.add_argument('--runtime',choices=['vllm','llamacpp'],help='select runtime preset or override its runtime')
    parser.add_argument('--model-path',type=Path,help='local HF directory or GGUF file')
    parser.add_argument('--model-name',help='model alias sent by the HTTP client')
    adapter=parser.add_mutually_exclusive_group()
    adapter.add_argument('--adapter-path',type=Path,help='PEFT directory for vLLM; GGUF LoRA file for llama.cpp')
    adapter.add_argument('--no-adapter',action='store_true',help='explicitly use selected model without an adapter')
    parser.add_argument('--llamacpp-path',type=Path,help='optional native llama-server executable')
    parser.add_argument('--gpu-layers',type=int,help='llama.cpp: -1 all, 0 CPU, N layers')
    parser.add_argument('--threads',type=int,help='llama.cpp CPU thread count')
    parser.add_argument('--port',type=int,help='override loopback HTTP port')


def selected_config(args, *, validate_weights=True):
    overrides={}
    for key in ('runtime','model_path','model_name','adapter_path','llamacpp_path','gpu_layers','threads','port'):
        value=getattr(args,key,None)
        if value is not None:
            overrides[key]=value.resolve() if isinstance(value,Path) else value
    if args.no_adapter:
        overrides['adapter_path']=None
    return load_config(args.config or profile_path(args.runtime),
                       validate_weights=validate_weights,overrides=overrides)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    add_selection_arguments(parser)
    parser.add_argument('action',choices=['doctor','command','client-config','start','status','stop','run'])
    args=parser.parse_args(argv)
    try:
        config=selected_config(args,validate_weights=args.action not in ('stop','status','client-config'))
        if args.action=='doctor':
            result=preflight(config);code=0 if result['ok'] else 2
        elif args.action=='command':
            result={'argv':engine_command(config),**config.selection};code=0
        elif args.action=='client-config':
            result={'selection':config.selection,'planner_class':'ModelServerPlanner',
                    'planner_settings':config.planner_settings,'server':status(config)}
            code=0
        elif args.action=='run':
            run_foreground(config);return 0
        else:
            result={'start':start,'status':status,'stop':stop}[args.action](config);code=0
        print(json.dumps(result,ensure_ascii=False,indent=2));return code
    except (ValueError,RuntimeError,OSError) as exc:
        print(json.dumps({'error':str(exc),'action':args.action},ensure_ascii=False),file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
