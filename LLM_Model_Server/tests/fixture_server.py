"""CPU-only lifecycle fixture; it performs no LLM inference."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--port',type=int,required=True)
p.add_argument('--name',default='planner')
p.add_argument('--fail',action='store_true')
p.add_argument('--wrong-model',action='store_true')
p.add_argument('--stubborn-child',type=Path)
args = p.parse_args()
if args.stubborn_child:
    child = subprocess.Popen([sys.executable,'-c',
        'import signal,time,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'], stdout=subprocess.PIPE)
    child.stdout.readline()
    child.stdout.close()
    args.stubborn_child.write_text(str(child.pid))
if args.fail:
    print('intentional fixture startup failure',flush=True)
    sys.exit(7)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            data = {'status':'ok'}
        elif self.path == '/v1/models':
            data = {'data':[{'id':'other' if args.wrong_model else args.name}]}
        else:
            self.send_error(404)
            return
        content = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self,*args):
        pass

ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
