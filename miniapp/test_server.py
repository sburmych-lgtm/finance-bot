import asyncio
import json
import re

from aiohttp.test_utils import TestClient, TestServer

from miniapp import server


SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Permissions-Policy': (
        'camera=(), microphone=(), geolocation=(), payment=(), usb=(), '
        'serial=(), accelerometer=(), gyroscope=(), magnetometer=()'
    ),
}


def test_api_base_is_injected_as_a_json_string(monkeypatch):
    api_base = "https://example.test/path'\\\n</script>"
    monkeypatch.setattr(server, 'API_BASE', api_base)
    html = "<script>window.__RUBY_API_BASE__ = window.__RUBY_API_BASE__ || '';</script>"

    result = server._inject_api_base(html)

    match = re.search(r'window\.__RUBY_API_BASE__ = (.*);', result)
    assert match is not None
    assert json.loads(match.group(1)) == api_base
    assert result.count('</script>') == 1
    assert api_base not in result


def test_index_uses_a_unique_nonce_and_local_api_origin_in_csp(monkeypatch):
    monkeypatch.setattr(server, 'API_BASE', 'http://127.0.0.1:18082/api')

    async def exercise():
        async with TestClient(TestServer(server.build_app())) as client:
            responses = []
            for _ in range(2):
                response = await client.get('/')
                responses.append((response.headers['Content-Security-Policy'], await response.text()))
            return responses

    responses = asyncio.run(exercise())
    nonces = []
    for csp, html in responses:
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]{32,})'", csp)
        inline = re.search(r'<script nonce="([A-Za-z0-9_-]+)">\s*// API base', html)
        assert nonce is not None
        assert inline is not None
        assert inline.group(1) == nonce.group(1)
        nonces.append(nonce.group(1))

        script_src = next(part for part in csp.split(';') if part.strip().startswith('script-src'))
        assert "'unsafe-inline'" not in script_src
        assert 'https://telegram.org' in script_src
        assert "connect-src 'self' http://127.0.0.1:18082" in csp
        assert "object-src 'none'" in csp
        assert "base-uri 'none'" in csp
        assert "frame-ancestors 'self' https://telegram.org https://*.telegram.org" in csp

    assert nonces[0] != nonces[1]


def test_malicious_api_config_stays_data_and_cannot_extend_csp(monkeypatch):
    api_base = 'https://api.example.test/path</script><script>alert(1)</script>'
    monkeypatch.setattr(server, 'API_BASE', api_base)

    async def exercise():
        async with TestClient(TestServer(server.build_app())) as client:
            response = await client.get('/')
            return response.headers['Content-Security-Policy'], await response.text()

    csp, html = asyncio.run(exercise())
    assignment = re.search(r'window\.__RUBY_API_BASE__ = (.*);', html)
    assert assignment is not None
    assert json.loads(assignment.group(1)) == api_base
    assert '</script><script>alert(1)</script>' not in html
    assert "connect-src 'self' https://api.example.test" in csp
    assert 'alert(1)' not in csp


def test_security_headers_cover_html_static_javascript_health_and_missing_routes():
    async def exercise():
        async with TestClient(TestServer(server.build_app())) as client:
            results = {}
            for path in ('/', '/privacy', '/terms', '/css/tokens.css', '/js/app.js', '/health', '/missing'):
                response = await client.get(path)
                results[path] = (response.status, dict(response.headers), await response.read())
            return results

    results = asyncio.run(exercise())
    assert results['/missing'][0] == 404
    for _status, headers, _body in results.values():
        for name, value in SECURITY_HEADERS.items():
            assert headers[name] == value
        assert 'Content-Security-Policy' in headers
        assert headers['Cache-Control'] == 'no-cache, no-store, must-revalidate'
        assert headers['X-Ruby-Build'] == server.BUILD


def test_cache_busting_build_header_and_traversal_guards():
    async def exercise():
        async with TestClient(TestServer(server.build_app())) as client:
            index_response = await client.get('/')
            index_html = await index_response.text()
            js_response = await client.get('/js/app.js')
            js = await js_response.text()
            js_traversal = await client.get('/js/%2e%2e%2fserver.py.js')
            css_traversal = await client.get('/css/%2e%2e/server.py')
            return index_html, js_response, js, js_traversal.status, css_traversal.status

    index_html, js_response, js, js_traversal, css_traversal = asyncio.run(exercise())
    assert f'./js/app.js?v={server.BUILD}' in index_html
    assert f"./api.js?v={server.BUILD}" in js
    assert js_response.headers['X-Ruby-Build'] == server.BUILD
    assert js_response.headers['Cache-Control'] == 'no-cache, no-store, must-revalidate'
    assert js_traversal in {400, 404}
    assert css_traversal == 404


def test_public_privacy_and_terms_pages_are_served_truthfully():
    async def exercise():
        async with TestClient(TestServer(server.build_app())) as client:
            pages = {}
            for path in ('/privacy', '/privacy.html', '/terms', '/terms.html'):
                response = await client.get(path)
                pages[path] = (
                    response.status,
                    response.headers.get('Cache-Control'),
                    await response.text(),
                )
            return pages

    pages = asyncio.run(exercise())

    for status, cache_control, html in pages.values():
        assert status == 200
        assert cache_control == 'no-cache, no-store, must-revalidate'
        assert f'./css/legal.css?v={server.BUILD}' in html

    privacy = pages['/privacy'][2]
    assert 'Політика приватності' in privacy
    assert 'не заявляє окремого прикладного шифрування' in privacy
    assert 'дані зашифровано' not in privacy.lower()
    assert 'ВИДАЛИТИ' in privacy

    terms = pages['/terms'][2]
    assert 'Умови користування' in terms
    assert 'не замінюють консультацію бухгалтера' in terms
