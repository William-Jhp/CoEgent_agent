"""Linux process-group lifecycle for this project's independent model server."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from urllib.request import urlopen

from .config import engine_command, engine_environment

_children = {}


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid':pid,'pgid':int(fields[2]),'sid':int(fields[3]),'start_ticks':fields[19]}
    except (OSError, IndexError, ValueError):
        return None


def resource_snapshot():
    available = None
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                available = int(line.split()[1]) / 1024**2
    except OSError:
        pass
    gpus = []
    try:
        result = subprocess.run(['nvidia-smi','--query-gpu=index,name,memory.total,memory.free',
                                 '--format=csv,noheader,nounits'],capture_output=True,text=True,
                                timeout=10,check=True)
        for line in result.stdout.splitlines():
            index,name,total,free = [s.strip() for s in line.split(',')]
            gpus.append({'index':int(index),'name':name,'total_mib':float(total),'free_mib':float(free)})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {'available_ram_gb':available,'gpus':gpus}


def runtime_version(config):
    try:
        command = ([str(config.llamacpp_path),'--version'] if config.runtime=='llamacpp' else
                   [str(config.python_path),'-c',
                    'import importlib.metadata; print(importlib.metadata.version("vllm"))'])
        result=subprocess.run(command,capture_output=True,text=True,timeout=20,check=True,
                              env=engine_environment(config))
        if config.runtime=='llamacpp':
            lines=(result.stdout+'\n'+result.stderr).splitlines()
            return next((line.strip() for line in lines if line.strip().startswith('version:')),None)
        return result.stdout.strip()
    except (OSError,subprocess.SubprocessError):
        return None


def preflight(config):
    resources=resource_snapshot()
    version=runtime_version(config)
    errors=[]
    if version is None:
        setup='setup_llamacpp.sh' if config.runtime=='llamacpp' else 'setup.sh'
        errors.append(f'{config.runtime} runtime unavailable; run scripts/{setup}')
    ram=resources['available_ram_gb']
    if ram is None or ram<config.min_available_ram_gb:
        errors.append(f'RAM: available {ram}, require >= {config.min_available_ram_gb} GiB')
    needs_gpu=config.runtime=='vllm' or config.gpu_layers!=0
    if needs_gpu:
        visible=os.environ.get('CUDA_VISIBLE_DEVICES','0').split(',')[0]
        index=int(visible) if visible.isdecimal() else None
        gpu=(next((g for g in resources['gpus'] if index is None or g['index']==index),None)
             if visible not in ('','-1') else None)
        if gpu is None:
            errors.append('CUDA GPU unavailable')
        else:
            required=(gpu['total_mib']*config.gpu_memory_utilization+1024 if config.runtime=='vllm'
                      else config.min_available_vram_gb*1024)
            if gpu['free_mib']<required:
                errors.append(f'GPU: free {gpu["free_mib"]} MiB, require >= {required:.0f} MiB')
    return {'ok':not errors,**config.selection,'runtime_version':version,
            'resources':resources,'errors':errors,'log':str(config.log_path)}


@contextmanager
def _lock(config):
    config.run_dir.mkdir(parents=True,exist_ok=True)
    with (config.run_dir/'controller.lock').open('a') as file:
        fcntl.flock(file,fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file,fcntl.LOCK_UN)


def _write_record(config,record):
    fd,path = tempfile.mkstemp(prefix='server.',suffix='.json',dir=config.run_dir)
    try:
        with os.fdopen(fd,'w') as file:
            json.dump(record,file,indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(path,config.state_path)
    finally:
        Path(path).unlink(missing_ok=True)


def _record(config):
    if not config.state_path.exists():
        return None
    try:
        record = json.loads(config.state_path.read_text())
        if (not isinstance(record,dict) or type(record.get('pid')) is not int
                or record['pid'] <= 1 or record.get('pgid') != record['pid']
                or not isinstance(record.get('start_ticks'),str)
                or not record['start_ticks'].isdecimal()
                or record.get('sid') != record['pid']
                or not isinstance(record.get('instance'),str)
                or len(record['instance']) != 32
                or not all(c in '0123456789abcdef' for c in record['instance'])
                or not isinstance(record.get('endpoint'),str)
                or not isinstance(record.get('model'),str)
                or record.get('phase') not in ('starting','ready')):
            raise ValueError('invalid process identity')
        return record
    except (ValueError,TypeError,OSError) as exc:
        raise RuntimeError(f'invalid state file: {exc}') from exc


def _matches(record):
    current = process_identity(record['pid'])
    return (record['pid'] != os.getpid() and current is not None
            and current['pgid']==record['pid'] and current['sid']==record['pid']
            and current['start_ticks']==record['start_ticks'])


def _ready(config):
    try:
        with urlopen(config.endpoint+'/health',timeout=0.5) as response:
            if response.status != 200:
                return False
        with urlopen(config.endpoint+'/v1/models',timeout=0.5) as response:
            raw = response.read(1024*1024+1)
            if len(raw)>1024*1024:
                return False
            data = json.loads(raw)
        return any(m.get('id')==config.model_name for m in data.get('data',[]) if isinstance(m,dict))
    except (OSError,ValueError,TypeError,AttributeError):
        return False


def _port_free(config):
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
            # Finished HTTP connections may retain TIME_WAIT after the runtime exits.
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind((config.host,config.port))
            sock.listen(1)
        return True
    except OSError:
        return False


def _reap(pid):
    child = _children.pop(pid,None)
    if child is not None:
        try: child.wait(timeout=1)
        except subprocess.TimeoutExpired: _children[pid]=child


def _owned_members(record):
    """Keep ownership verifiable after the leader exits; never trust PGID alone."""
    leader = process_identity(record['pid'])
    if leader is not None and not _matches(record):
        raise RuntimeError('process identity mismatch; refusing to signal')
    members = []
    marker = ('LOCAL_LLM_SERVER_INSTANCE='+record['instance']).encode()
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        identity = process_identity(int(path.name))
        if identity is None or identity['pgid']!=record['pgid'] or identity['sid']!=record['sid']:
            continue
        if identity['pid']==os.getpid() or int(identity['start_ticks'])<int(record['start_ticks']):
            raise RuntimeError('process group identity mismatch; state retained')
        if leader is None:
            try:
                verified = marker in (path/'environ').read_bytes().split(b'\0')
            except OSError:
                if process_identity(identity['pid']) is None:
                    continue
                verified = False
            if not verified:
                raise RuntimeError('worker ownership cannot be verified; state retained')
        members.append(identity)
    return members


def _terminate(record,timeout):
    if not _owned_members(record):
        _reap(record['pid'])
        return
    try:
        os.killpg(record['pgid'],signal.SIGTERM)
    except ProcessLookupError:
        _reap(record['pid'])
        return
    end = time.monotonic()+timeout
    while _owned_members(record) and time.monotonic()<end:
        time.sleep(0.05)
    if _owned_members(record):
        os.killpg(record['pgid'],signal.SIGKILL)
        end = time.monotonic()+3
        while _owned_members(record) and time.monotonic()<end:
            time.sleep(0.05)
    _reap(record['pid'])
    if _owned_members(record):
        raise RuntimeError('server workers did not stop; state retained')


def status(config):
    record = _record(config)
    result = {**config.selection,'log':str(config.log_path)}
    if record is None:
        return {**result,'status':'stopped'}
    current = process_identity(record['pid'])
    if current is None:
        _reap(record['pid'])
        return {**result,'status':'orphan_workers' if _owned_members(record) else 'stopped',
                'pid':record['pid'],'stale_state':True}
    if not _matches(record):
        return {**result,'status':'identity_mismatch','pid':record['pid']}
    if (record['endpoint']!=config.endpoint or record['model']!=config.model_name
            or record.get('runtime','vllm')!=config.runtime
            or record.get('model_path')!=str(config.model_path)
            or record.get('adapter_path')!=config.selection['adapter_path']
            or record.get('engine_settings')!=config.engine_settings):
        return {**result,'status':'config_changed','pid':record['pid'],
                'running_endpoint':record['endpoint'],'running_model':record['model'],
                'running_runtime':record.get('runtime','vllm'),
                'running_model_path':record.get('model_path'),
                'running_adapter_path':record.get('adapter_path')}
    return {**result,'pid':record['pid'],
            'status':'ready' if _ready(config) else 'starting' if record['phase']=='starting' else 'unhealthy'}


def start(config):
    with _lock(config):
        previous = _record(config)
        if previous is not None:
            if process_identity(previous['pid']) is not None:
                if not _matches(previous):
                    raise RuntimeError('process identity mismatch; review state file')
                raise RuntimeError(f'server already running: PID {previous["pid"]}')
            if _owned_members(previous):
                raise RuntimeError('server workers still running; use stop first')
            _reap(previous['pid'])
            config.state_path.unlink()
        if not _port_free(config):
            raise RuntimeError(f'port {config.port} is already in use; refusing takeover')
        report = preflight(config)
        if not report['ok']:
            raise RuntimeError('preflight failed: '+'; '.join(report['errors']))
        config.log_dir.mkdir(parents=True,exist_ok=True)
        command = engine_command(config)
        instance = uuid.uuid4().hex
        environment = {**engine_environment(config),'LOCAL_LLM_SERVER_INSTANCE':instance}
        with config.log_path.open('a') as log:
            log.write('\n'+json.dumps({'event':'server_start','time':time.time(),'argv':command})+'\n')
            log.flush()
            process = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                       env=environment,start_new_session=True,
                                       close_fds=True)
        _children[process.pid]=process
        identity = process_identity(process.pid)
        if identity is None:
            code = process.wait(timeout=2)
            _reap(process.pid)
            raise RuntimeError(f'server exited during startup ({code}); see {config.log_path}')
        record = {**identity,'instance':instance,**config.selection,
                  'engine_settings':config.engine_settings,
                  'phase':'starting','created_at':time.time(),'log':str(config.log_path)}
        try:
            _write_record(config,record)
        except BaseException:
            _terminate(record,config.stop_timeout_s)
            raise
    try:
        end = time.monotonic()+config.startup_timeout_s
        while time.monotonic()<end:
            code = process.poll()
            if code is not None:
                raise RuntimeError(f'server exited during startup ({code}); see {config.log_path}')
            if _ready(config):
                with _lock(config):
                    current = _record(config)
                    if current != record or not _matches(record):
                        raise RuntimeError('server stopped or identity changed during startup')
                    record['phase']='ready'
                    _write_record(config,record)
                return status(config)
            time.sleep(0.1)
        raise RuntimeError(f'server did not become ready in {config.startup_timeout_s}s; see {config.log_path}')
    except BaseException:
        with _lock(config):
            current = _record(config)
            if current is not None and current['pid']==record['pid'] and current['start_ticks']==record['start_ticks']:
                _terminate(record,config.stop_timeout_s)
                config.state_path.unlink(missing_ok=True)
        raise


def stop(config):
    with _lock(config):
        record = _record(config)
        if record is None:
            return {'status':'stopped','endpoint':config.endpoint}
        _terminate(record,config.stop_timeout_s)
        config.state_path.unlink(missing_ok=True)
        return {'status':'stopped','pid':record['pid'],'endpoint':record['endpoint']}


def run_foreground(config):
    with _lock(config):
        record = _record(config)
        if record is not None and _owned_members(record):
            raise RuntimeError('managed server or workers already running')
        if not _port_free(config):
            raise RuntimeError(f'port {config.port} is already in use')
        report = preflight(config)
        if not report['ok']:
            raise RuntimeError('preflight failed: '+'; '.join(report['errors']))
    command = engine_command(config)
    os.execve(command[0],command,engine_environment(config))
