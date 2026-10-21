"""ctypes binding to the same installed llama.cpp server core, without sockets."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parents[2]
_CANCEL = ctypes.CFUNCTYPE(ctypes.c_int)


class NativeLlamaEngine:
    def __init__(self, model_path, *, chat_template, n_ctx=4096, threads=8,
                 gpu_layers=-1, model_name='coegent-e4b-gguf', adapter=None):
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._handle = None
        runtime = ROOT/'LLM_Model_Server/runtimes/llamacpp'
        library = runtime/'bin/libcoegent-llamacpp.so'
        if not library.is_file():
            raise ValueError('native llama.cpp binding missing; run .venv/bin/python LLM_Model_Server/scripts/build_inprocess.py')
        manifest = json.loads((runtime/'inprocess_manifest.json').read_text())
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        if manifest['abi'] != 1 or sha(library) != manifest['bridge_sha256'] or any(
                sha(library.parent/name) != digest for name,digest in manifest['linked_libraries'].items()):
            raise ValueError('native runtime changed; rebuild the inprocess binding')
        self._lib = ctypes.CDLL(str(library))
        self._lib.coegent_llama_abi_version.restype = ctypes.c_int
        self._lib.coegent_llama_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
        self._lib.coegent_llama_open.restype = ctypes.c_int
        self._lib.coegent_llama_request.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_double, _CANCEL, ctypes.POINTER(ctypes.c_void_p)]
        self._lib.coegent_llama_request.restype = ctypes.c_int
        self._lib.coegent_llama_free.argtypes = [ctypes.c_void_p]
        self._lib.coegent_llama_free.restype = None
        self._lib.coegent_llama_close.argtypes = [ctypes.c_void_p]
        self._lib.coegent_llama_close.restype = None
        if self._lib.coegent_llama_abi_version() != 1:
            raise ValueError('unsupported native binding ABI')
        argv = ['coegent-inprocess', '--model', str(model_path), '--alias', model_name,
            '--ctx-size', str(n_ctx), '--parallel', '1', '--threads', str(threads),
            '--gpu-layers', 'all' if gpu_layers == -1 else str(gpu_layers), '--jinja',
            '--chat-template-file', str(chat_template)]
        if adapter is not None: argv += ['--lora', str(adapter)]
        handle, error = ctypes.c_void_p(), ctypes.c_void_p()
        code = self._lib.coegent_llama_open(json.dumps({'argv':argv}).encode(), ctypes.byref(handle), ctypes.byref(error))
        message = self._take(error)
        if code: raise ValueError('llama.cpp initialization: '+message)
        self._handle = handle

    def _take(self, value):
        if not value.value: return ''
        try: return ctypes.string_at(value).decode('utf-8')
        finally: self._lib.coegent_llama_free(value)

    def _check_owner(self):
        if os.getpid() != self._pid:
            raise RuntimeError('native engine cannot be used after fork; create it in the child')

    def request(self, route, payload=None, *, timeout_s=90, cancelled=None):
        self._check_owner()
        with self._lock:
            if self._closing.is_set() or self._handle is None:
                raise RuntimeError('native engine is closed')
            callback = _CANCEL(lambda: self._closing.is_set() or (cancelled is not None and cancelled.is_set()))
            result = ctypes.c_void_p()
            code = self._lib.coegent_llama_request(self._handle, route.encode(),
                json.dumps({} if payload is None else payload, ensure_ascii=False, allow_nan=False).encode(),
                timeout_s, callback, ctypes.byref(result))
            body = self._take(result)
            if code == 2: raise TimeoutError(body)
            if code: raise ValueError('native llama.cpp: '+body)
            envelope = json.loads(body)
            if envelope['status'] != 200:
                raise ValueError(f"native llama.cpp status {envelope['status']}: {envelope['body']}")
            return json.loads(envelope['body'])

    def close(self):
        self._check_owner()
        self._closing.set()
        with self._lock:
            if self._handle is not None:
                self._lib.coegent_llama_close(self._handle)
                self._handle = None

    def __del__(self):
        try:
            if getattr(self,'_pid',None) == os.getpid(): self.close()
        except Exception:
            pass
