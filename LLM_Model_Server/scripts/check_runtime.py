"""Check the selected runtime contract without loading model weights."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from local_llm_server.cli import add_selection_arguments, selected_config
from local_llm_server.config import engine_command, engine_environment
from local_llm_server.lifecycle import runtime_version


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    add_selection_arguments(parser)
    parser.add_argument('--out',type=Path,default=ROOT/'results/runtime_contract.json')
    args=parser.parse_args()
    config=selected_config(args)
    argv=engine_command(config)
    report={'runtime':config.runtime,'runtime_version':runtime_version(config),
            'model_path':str(config.model_path),'adapter_path':config.selection['adapter_path'],
            'weights_loaded':False,'inference_run':False}
    if config.runtime=='llamacpp':
        result=subprocess.run([str(config.llamacpp_path),'--help'],env=engine_environment(config),
                              capture_output=True,text=True,timeout=20,check=True)
        help_text=result.stdout+'\n'+result.stderr
        missing=[flag for flag in argv[1:] if flag.startswith('--') and flag not in help_text]
        if missing: raise RuntimeError('native runtime does not support flags: '+str(missing))
        report.update({'runtime_argv_flags_available':True,'executable':str(config.llamacpp_path),
                       'argv':argv,'model_format':'GGUF'})
    else:
        from vllm.entrypoints.cli.serve import ServeSubcommand
        from vllm.utils.argparse_utils import FlexibleArgumentParser
        from vllm.engine.arg_utils import EngineArgs
        from vllm.model_executor.models.registry import ModelRegistry
        from vllm.model_executor.models.interfaces import supports_lora
        runtime_parser=FlexibleArgumentParser()
        command=ServeSubcommand();command.subparser_init(runtime_parser.add_subparsers())
        runtime_args=runtime_parser.parse_args(argv[3:]);command.validate(runtime_args)
        runtime_args.model=runtime_args.model_tag
        model=EngineArgs.from_cli_args(runtime_args).create_model_config()
        checkpoint=json.loads((config.model_path/'config.json').read_text())
        if Path(model.model).resolve()!=config.model_path or model.hf_config.model_type!=checkpoint['model_type']:
            raise RuntimeError('runtime contract inspected a different model checkpoint')
        cls,architecture=ModelRegistry.resolve_model_cls(model.architectures,model)
        if config.adapter_path is not None and not supports_lora(cls):
            raise RuntimeError('selected runtime model class does not support LoRA')
        report.update({'runtime_argv_accepted':True,'hf_model_type':model.hf_config.model_type,
                       'model_class':cls.__module__+'.'+cls.__qualname__,
                       'checkpoint_architectures':checkpoint.get('architectures'),
                       'selected_architectures':model.architectures,
                       'resolved_architecture':architecture,'supports_lora':supports_lora(cls),
                       'is_multimodal':model.is_multimodal_model})
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2));return 0


if __name__=='__main__': raise SystemExit(main())
