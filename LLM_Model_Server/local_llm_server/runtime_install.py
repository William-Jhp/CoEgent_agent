"""Privately bundle a trusted local llama.cpp build and verify library isolation."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

from .lifecycle import _lock, process_identity

PROJECT_LIBRARIES=('libllama','libggml','libmtmd')


def _hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda:file.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def _dependencies(binary,environment=None):
    result=subprocess.run(['ldd',str(binary)],env=environment,capture_output=True,
                          text=True,timeout=20,check=True)
    if 'not found' in result.stdout:
        raise RuntimeError('runtime shared library unavailable: '+result.stdout)
    dependencies=[]
    for line in result.stdout.splitlines():
        match=re.match(r'\s*(\S+)\s+=>\s+(\S+)',line)
        if match and match.group(2).startswith('/'):
            dependencies.append({'name':match.group(1),'path':match.group(2)})
    return dependencies


def _ensure_stopped(state_path):
    if not state_path.exists():
        return
    try:
        record=json.loads(state_path.read_text())
    except (OSError,ValueError) as exc:
        raise RuntimeError('cannot verify runtime state before install') from exc
    if record.get('runtime')=='llamacpp' and process_identity(record.get('pid')) is not None:
        raise RuntimeError('llama.cpp runtime is running; stop it before reinstalling')


def _owned_destination(destination):
    if not destination.exists():
        return
    try:
        manifest=json.loads((destination/'manifest.json').read_text())
        allowed=set(manifest['files'])|{'manifest.json'}
        actual={str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file() or p.is_symlink()}
        if actual-allowed or manifest.get('runtime')!='llamacpp':
            raise ValueError('unmanaged files present')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise RuntimeError('refusing to overwrite an unmanaged runtime directory') from exc


def install_llamacpp(build_dir,destination,*,state_path=None,license_path=None):
    build_dir=Path(build_dir).resolve();destination=Path(destination).resolve()
    state_path=Path(state_path or Path(__file__).resolve().parents[1]/'run/server.json')
    binary=build_dir/'bin/llama-server'
    license_file=Path(license_path).resolve() if license_path else build_dir.parent/'LICENSE'
    if not binary.is_file() or not os.access(binary,os.X_OK):
        raise ValueError(f'llama-server not found in build: {binary}')
    if not license_file.is_file():
        raise ValueError(f'llama.cpp license not found: {license_file}')
    if destination.is_relative_to(build_dir) or build_dir.is_relative_to(destination):
        raise ValueError('destination must not overlap the source build')
    with _lock(SimpleNamespace(run_dir=state_path.parent)):
        _ensure_stopped(state_path)
    dependencies=_dependencies(binary)
    candidates={Path(d['path']) for d in dependencies if d['name'].startswith(PROJECT_LIBRARIES)}
    candidates.update(build_dir.rglob('libggml*.so*'))
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.llamacpp-stage-',dir=destination.parent))
    backup=None
    try:
        bindir=stage/'bin';bindir.mkdir()
        shutil.copy2(binary,bindir/'llama-server')
        for source in sorted(candidates):
            resolved=source.resolve()
            if not resolved.is_relative_to(build_dir) or not resolved.is_file():
                raise RuntimeError(f'project library escapes source build: {source}')
            target=bindir/resolved.name
            if target.exists() and _hash(target)!=_hash(resolved):
                raise RuntimeError('conflicting runtime library names: '+resolved.name)
            if not target.exists():
                shutil.copy2(resolved,target)
            if source.name!=resolved.name:
                alias=bindir/source.name
                if not alias.exists():
                    alias.symlink_to(resolved.name)
        shutil.copy2(license_file,stage/'LICENSE')
        environment={**os.environ,'LD_LIBRARY_PATH':str(bindir)+(
            os.pathsep+os.environ['LD_LIBRARY_PATH'] if os.environ.get('LD_LIBRARY_PATH') else '')}
        installed_dependencies=_dependencies(bindir/'llama-server',environment)
        for dependency in installed_dependencies:
            if dependency['name'].startswith(PROJECT_LIBRARIES):
                if not Path(dependency['path']).resolve().is_relative_to(stage):
                    raise RuntimeError('runtime still depends on external project libraries')
        version=subprocess.run([str(bindir/'llama-server'),'--version'],env=environment,
                               capture_output=True,text=True,timeout=20,check=True)
        lines=(version.stdout+'\n'+version.stderr).splitlines()
        version_line=next((line for line in lines if line.startswith('version:')),None)
        if version_line is None:
            raise RuntimeError('installed executable did not report a llama.cpp version')
        files={str(p.relative_to(stage)):{'sha256':_hash(p),
               'symlink':os.readlink(p) if p.is_symlink() else None}
               for p in stage.rglob('*') if p.is_file()}
        manifest={'runtime':'llamacpp','version':version_line,'source_build':str(build_dir),
                  'source_binary_sha256':_hash(binary),'files':files,
                  'dependencies':[{**d,'path':d['path'].replace(str(stage),str(destination))}
                                  for d in installed_dependencies]}
        (stage/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        with _lock(SimpleNamespace(run_dir=state_path.parent)):
            _ensure_stopped(state_path);_owned_destination(destination)
            if destination.exists():
                backup=Path(tempfile.mkdtemp(prefix='.llamacpp-backup-',dir=destination.parent))
                backup.rmdir();destination.rename(backup)
            try:
                stage.rename(destination)
            except BaseException:
                if backup is not None: backup.rename(destination);backup=None
                raise
        if backup is not None:
            shutil.rmtree(backup)
        return manifest
    finally:
        if stage.exists(): shutil.rmtree(stage)
