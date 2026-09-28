"""MCP protocol, exact-action approvals and explicit assistant data sharing."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
import frame_agent as agent
import frame_assistant as assistant
import frame_mcp as mcp
import server


class Approvals(unittest.TestCase):
    def test_requires_human_decision_exact_action_and_single_use(self):
        gate = agent.Approvals()
        action = {'name': 'power', 'arguments': {'action': 'reboot'}}
        token = gate.request(action)['confirmation']
        with self.assertRaises(ValueError):
            gate.consume(token, action)
        gate.decide(token, True)
        with self.assertRaises(ValueError):
            gate.consume(token, {'name': 'power', 'arguments': {'action': 'poweroff'}})
        gate.consume(token, action)
        with self.assertRaises(ValueError):
            gate.consume(token, action)

    def test_expiry_rejection_and_non_boolean_approval(self):
        gate = agent.Approvals()
        token = gate.request({})['confirmation']
        gate.decide(token, 'true')
        with self.assertRaises(ValueError):
            gate.inspect(token)
        token = gate.request({})['confirmation']
        with mock.patch.object(agent.time, 'monotonic', return_value=float('inf')):
            with self.assertRaises(ValueError):
                gate.decide(token, True)

    def test_concurrent_consumption_executes_once(self):
        gate = agent.Approvals()
        token = gate.request({})['confirmation']
        gate.decide(token, True)
        results = []
        def consume():
            try:
                gate.consume(token, {})
                results.append(True)
            except ValueError:
                results.append(False)
        threads = [threading.Thread(target=consume) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results.count(True), 1)

    def test_action_never_runs_before_approval(self):
        with mock.patch.object(agent, 'approvals', agent.Approvals()), mock.patch.object(server, 'flatpak') as install:
            body = {'name': 'install', 'arguments': {'id': 'org.example.App'}}
            result = agent.call(server, body)
            install.assert_not_called()
            body['confirmation'] = result['confirmation']
            with self.assertRaises(ValueError): agent.call(server, body)
            agent.approvals.decide(body['confirmation'], True)
            agent.call(server, body)
            install.assert_called_once_with({'id': 'org.example.App', 'action': 'install'})
            with self.assertRaises(ValueError): agent.call(server, body)

    def test_file_content_change_invalidates_approval(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(agent, 'approvals', agent.Approvals()), mock.patch.object(server, 'push_file') as push:
            path = Path(tmp) / 'note.txt'
            path.write_text('first')
            body = {'name': 'send_file', 'arguments': {'path': str(path)}}
            result = agent.call(server, body)
            agent.approvals.decide(result['confirmation'], True)
            body['confirmation'] = result['confirmation']
            path.write_text('second')
            with self.assertRaises(ValueError): agent.call(server, body)
            push.assert_not_called()

    def test_no_arbitrary_commands_or_arguments(self):
        for name, args in [('shell', {'command': 'true'}), ('panel', {'id': 'org.example.App', 'args': '--evil'}),
                           ('power', {'action': 'factory-reset'}), ('send_text', {'text': ''})]:
            with self.assertRaises(ValueError): agent.call(server, {'name': name, 'arguments': args})


class Assistant(unittest.TestCase):
    def setUp(self):
        self.received = []
        owner = self
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                owner.received.append((dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/other')
                    self.end_headers()
                    return
                data = json.dumps({'choices': [{'message': {'content': '<script>not executed</script>'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.body = {'endpoint': 'http://127.0.0.1:%d/chat' % self.httpd.server_port, 'model': 'local', 'prompt': 'Hello', 'consent': True}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def test_no_opt_in_no_request_or_capture(self):
        capture = mock.Mock()
        for consent in (False, None, 'true', 1):
            with self.assertRaises(ValueError): assistant.chat({**self.body, 'consent': consent, 'screenshot': True}, capture)
        capture.assert_not_called()
        self.assertEqual(self.received, [])

    def test_text_only_keyless_and_optional_screenshot(self):
        capture = mock.Mock(return_value=b'png')
        self.assertIn('script', assistant.chat(self.body, capture)['reply'])
        capture.assert_not_called()
        headers, body = self.received[-1]
        self.assertNotIn('Authorization', headers)
        self.assertEqual(body['messages'], [{'role': 'user', 'content': 'Hello'}])
        assistant.chat({**self.body, 'screenshot': True, 'key': 'test-key'}, capture)
        capture.assert_called_once()
        headers, body = self.received[-1]
        self.assertEqual(headers['Authorization'], 'Bearer test-key')
        self.assertEqual(body['messages'][0]['content'][1]['image_url']['url'], 'data:image/png;base64,cG5n')

    def test_redirects_do_not_forward_context_or_credentials(self):
        with self.assertRaises(ValueError):
            assistant.chat({**self.body, 'endpoint': self.body['endpoint'].replace('/chat', '/redirect'), 'key': 'secret'}, mock.Mock())
        self.assertEqual(len(self.received), 1)

    def test_bad_urls_fail_before_capture(self):
        for url in ('file:///etc/passwd', 'http://example.com/chat', 'https://user:pass@example.com', 'https://example.com?key=secret'):
            capture = mock.Mock()
            with self.assertRaises(ValueError): assistant.chat({**self.body, 'endpoint': url, 'screenshot': True}, capture)
            capture.assert_not_called()


class Protocol(unittest.TestCase):
    def test_stdio_initialize_list_call_errors_and_eof(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'shell'}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'},
        ]
        result = subprocess.run([sys.executable, str(Path(mcp.__file__))], input='\n'.join(map(json.dumps, messages)) + '\n', text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = list(map(json.loads, result.stdout.splitlines()))
        self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        self.assertIn('screenshot', [t['name'] for t in replies[1]['result']['tools']])
        self.assertTrue(replies[2]['result']['isError'])

    def test_mcp_cannot_approve_and_returns_review_url(self):
        client = mock.Mock(url='http://127.0.0.1:47810')
        client.request.return_value = {'approvalPath': '/assistant#confirm=token'}
        result = mcp.call(client, 'power', {'action': 'reboot'})
        self.assertIn('http://127.0.0.1:47810/assistant', result['content'][0]['text'])
        with self.assertRaises(ValueError): mcp.call(client, 'approve', {'confirmation': 'token'})
        with self.assertRaises(ValueError): mcp.call(client, 'status', {'path': '/api/open'})

    def test_loopback_only_backend(self):
        for url in ('https://example.com', 'http://127.0.0.1/api', 'http://secret@localhost:1234', 'file:///tmp/x'):
            with self.assertRaises(ValueError): mcp.Client(url)


if __name__ == '__main__':
    unittest.main()
