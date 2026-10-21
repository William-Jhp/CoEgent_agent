"""Build only a thin binding; link the exact installed server/context libraries."""
import hashlib,json,shlex,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    runtime=ROOT/'runtimes/llamacpp';manifest=json.loads((runtime/'manifest.json').read_text())
    source=Path(manifest['source_build']).parent;build=Path(manifest['source_build'])
    revision=subprocess.check_output(['git','-C',str(source),'rev-parse','--short=7','HEAD'],text=True).strip()
    if revision not in manifest['version']:raise RuntimeError('source/runtime version mismatch')
    subprocess.run(['git','-C',str(source),'diff','--quiet','HEAD','--','*.h'],check=True)
    flags=(build/'tools/server/CMakeFiles/server-context.dir/flags.make').read_text().splitlines()
    defines=shlex.split(next(s.split('=',1)[1] for s in flags if s.startswith('CXX_DEFINES =')))
    directories=[source,source/'tools/server',source/'common',source/'include',source/'ggml/include',source/'vendor',source/'tools/mtmd',build/'tools/server']
    binary=runtime/'bin/libcoegent-llamacpp.so';temporary=binary.with_suffix('.tmp.so')
    linked=[runtime/'bin/libllama-server-impl.so',runtime/'bin/libllama-common.so.0.3.0',runtime/'bin/libllama.so.0.3.0']
    command=['c++','-std=c++17','-O2','-fPIC','-shared','-pthread',*defines,*[f'-I{p}' for p in directories],str(ROOT/'native/inprocess.cpp'),*[str(p) for p in linked],'-Wl,-rpath,$ORIGIN','-Wl,-rpath,/usr/local/cuda/lib64','-o',str(temporary)]
    subprocess.run(command,check=True)
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    temporary.replace(binary)
    versions={'abi':1,'source_revision':revision,'runtime_version':manifest['version'],'bridge_sha256':sha(binary),'source_sha256':sha(ROOT/'native/inprocess.cpp'),'linked_libraries':{p.name:sha(p) for p in linked},'command':command}
    (runtime/'inprocess_manifest.json').write_text(json.dumps(versions,indent=2)+'\n')
    print(binary)
if __name__=='__main__':main()
