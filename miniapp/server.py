"""Static aiohttp server for Ruby Finance Mini App.

Serves the miniapp/ static files and injects __RUBY_API_BASE__
into index.html at request time, so the frontend can reach the
backend API service.

Env:
  PORT          — bind port (Railway provides this, default 8080)
  API_BASE_URL  — full https URL to the bot service exposing /api/*
                  (e.g. https://worker-production.up.railway.app)
"""
from __future__ import annotations
import os
import re
from aiohttp import web

ROOT = os.path.dirname(os.path.abspath(__file__))
API_BASE = os.environ.get('API_BASE_URL', '').rstrip('/')


def _inject_api_base(html: str) -> str:
    if not API_BASE:
        return html
    # Replace the runtime placeholder so the frontend uses the right backend.
    return re.sub(
        r"window\.__RUBY_API_BASE__\s*=\s*window\.__RUBY_API_BASE__\s*\|\|\s*['\"][^'\"]*['\"]\s*;",
        f"window.__RUBY_API_BASE__ = '{API_BASE}';",
        html,
        count=1,
    )


async def index(_request: web.Request) -> web.Response:
    path = os.path.join(ROOT, 'index.html')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        return web.Response(status=404, text='index.html missing')
    return web.Response(
        body=_inject_api_base(html),
        content_type='text/html',
        charset='utf-8',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            # Telegram requires same-origin or wildcard for the WebApp iframe.
            'X-Content-Type-Options': 'nosniff',
        },
    )


async def health(_request: web.Request) -> web.Response:
    return web.json_response({'ok': True, 'service': 'ruby-finance-miniapp', 'api_base': API_BASE or None})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/index.html', index)
    app.router.add_get('/health', health)
    for url, dirname in (('/css/', 'css'), ('/js/', 'js'), ('/assets/', 'assets')):
        full = os.path.join(ROOT, dirname)
        if os.path.isdir(full):
            app.router.add_static(url, full, show_index=False)
    return app


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    web.run_app(build_app(), host='0.0.0.0', port=port, print=lambda *_: None)
