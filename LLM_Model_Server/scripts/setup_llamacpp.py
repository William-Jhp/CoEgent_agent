"""Install a local llama.cpp build into this server's private runtime directory."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from local_llm_server.runtime_install import install_llamacpp


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-build',type=Path,
        default=ROOT.parent/'deployment_runtime_benchmark_v0_1/llama.cpp/build')
    parser.add_argument('--destination',type=Path,default=ROOT/'runtimes/llamacpp')
    parser.add_argument('--license',type=Path,help='source license for an out-of-tree build')
    args=parser.parse_args()
    try:
        result=install_llamacpp(args.from_build,args.destination,license_path=args.license)
        print(json.dumps({'runtime':result['runtime'],'version':result['version'],
                          'destination':str(args.destination.resolve()),'files':len(result['files'])},indent=2))
        return 0
    except (OSError,ValueError,RuntimeError) as exc:
        print(json.dumps({'error':str(exc)}),file=sys.stderr);return 2


if __name__=='__main__': raise SystemExit(main())
