"""Explicit, per-request forwarding to a user-chosen chat-completions endpoint."""
import base64
import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Endpoint redirected; enter its final URL explicitly')


def chat(body, screenshot):
    if body.get('consent') is not True:
        raise ValueError('Opt in before sending a message')
    endpoint, model, prompt = (body.get(k) for k in ('endpoint', 'model', 'prompt'))
    if any(not isinstance(v, str) or not v.strip() for v in (endpoint, model, prompt)):
        raise ValueError('Endpoint, model and message are required')
    if len(prompt) > 32000 or len(model) > 200 or len(endpoint) > 2048:
        raise ValueError('Message, model or endpoint is too long')
    url = urlsplit(endpoint)
    if not url.hostname or url.username or url.password or url.fragment or url.query:
        raise ValueError('Use an endpoint URL without credentials, query or fragment')
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('Use HTTPS, or HTTP on loopback for a local model')
    key = body.get('key', '')
    if not isinstance(key, str) or len(key) > 4096 or '\n' in key or '\r' in key:
        raise ValueError('Invalid API key')
    content = prompt
    if body.get('screenshot') is True:
        png = screenshot()
        if len(png) > 12 * 1024**2:
            raise ValueError('Screenshot is too large')
        content = [{'type': 'text', 'text': prompt}, {'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(png).decode()}}]
    payload = {'model': model, 'messages': [{'role': 'user', 'content': content}], 'stream': False}
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    # No environment proxy or redirects: credentials/context go only to the chosen URL.
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=60) as response:
            raw = response.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2:
            raise ValueError('Endpoint response is too large')
        answer = json.loads(raw)['choices'][0]['message']['content']
        if not isinstance(answer, str):
            raise ValueError('Expected a text reply')
    except Exception:
        # Provider error bodies and URLs can contain credentials or echoed prompts.
        raise ValueError('Endpoint request failed or returned an unsupported reply; check URL, model and credentials') from None
    return {'reply': answer}
