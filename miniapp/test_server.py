import asyncio
import json
import re

from aiohttp.test_utils import TestClient, TestServer

from miniapp import server


def test_api_base_is_injected_as_a_json_string(monkeypatch):
    api_base = "https://example.test/path'\\\n</script>"
    monkeypatch.setattr(server, 'API_BASE', api_base)
    html = "<script>window.__RUBY_API_BASE__ = window.__RUBY_API_BASE__ || '';</script>"

    result = server._inject_api_base(html)

    match = re.search(r'window\.__RUBY_API_BASE__ = (.*);', result)
    assert match is not None
    assert json.loads(match.group(1)) == api_base


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
