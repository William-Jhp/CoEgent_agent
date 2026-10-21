import json
import asyncio
from pathlib import Path
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from local_llm_server.smoke import schema_probe, chat_completion


class SchemaProbeTests(unittest.TestCase):
    def serve(self,content,finish='stop'):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/v1/chat/completions' or 'json_schema' not in payload.get('response_format',{}):
                    self.send_error(400)
                    return
                raw = json.dumps({'choices':[{'message':{'content':content},'finish_reason':finish}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self,*args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:'+str(server.server_address[1])

    def test_actual_plan_response_cannot_accept_truncated_valid_json(self):
        endpoint = self.serve('{"plan":{"steps":[]}}',finish='length')
        with self.assertRaisesRegex(ValueError,'truncated'):
            chat_completion(endpoint,{'model':'planner','response_format':{'json_schema':{}}},2)

    def planning_report(self,finish):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
        from smoke_coegent import generate
        from local_llm_server.config import ServerConfig
        plan = {'schema_version':'planner.v1','goal':'Check battery',
                'sequence':[{'type':'skill','name':'get_battery','args':{}}],
                'success_criteria':['Battery read'],'final_outcome':'execute'}
        endpoint = self.serve(json.dumps(plan),finish=finish)
        config = ServerConfig(Path('.'),Path('.'),port=int(endpoint.rsplit(':',1)[1]))
        return asyncio.run(generate(config,'현재 배터리를 확인해.',512))

    def test_plan_generator_validates_http_plan_without_execution(self):
        report = self.planning_report('stop')
        self.assertEqual(report['status'],'PLAN_READY')
        self.assertEqual(report['execution_status'],'NOT_EXECUTED')
        self.assertFalse(any(e['kind']=='skill_start' for e in report['trace']))

    def test_actual_plan_generator_rejects_truncated_response(self):
        report = self.planning_report('length')
        self.assertEqual(report['status'],'PLANNER_ERROR')
        self.assertIsNone(report['plan'])
        self.assertIn('truncated',report['reason'])

    def test_valid_response_passes_the_contract_probe(self):
        result = schema_probe(self.serve('{"probe":"schema_applied"}'),'planner',2)
        self.assertTrue(result['passed'])

    def test_ignored_schema_is_not_reported_as_success(self):
        with self.assertRaisesRegex(ValueError,'schema'):
            schema_probe(self.serve('{"unexpected":true}'),'planner',2)

    def test_truncated_output_is_not_reported_as_success(self):
        with self.assertRaisesRegex(ValueError,'truncated'):
            schema_probe(self.serve('{"probe":"schema_applied"}',finish='length'),'planner',2)


if __name__=='__main__': unittest.main()
