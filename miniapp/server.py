"""Static aiohttp server for Ruby Finance Mini App.

Serves the miniapp/ static files. Two cache-bust mechanisms ensure
Telegram WebView never serves stale code after a redeploy:

1. **Build ID query string** appended to every JS/CSS URL in the
   served index.html AND to every relative ES-module import inside
   served JS files (regex rewrite of `from './foo.js'` → `from './foo.js?v=…'`).
   On each container restart, BUILD changes, so URLs change, so the
   WebView treats them as different resources and refetches.

2. **`Cache-Control: no-cache, no-store, must-revalidate`** headers on
   every response — belt and suspenders.

The Mini App lifecycle is short and bytes are tiny; refetching on
every cold start is cheap. Code freshness wins.

Env:
  PORT          — bind port (Railway provides this)
  API_BASE_URL  — full https URL to the bot service exposing /api/*
  BUILD_ID      — override the auto-generated build id (defaults to unix-epoch
                  seconds at process start, or RAILWAY_GIT_COMMIT_SHA if set)
"""
from __future__ import annotations
import os
import re
import time
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent
API_BASE = os.environ.get('API_BASE_URL', '').rstrip('/')
BUILD = (
    os.environ.get('BUILD_ID')
    or os.environ.get('RAILWAY_GIT_COMMIT_SHA', '')[:12]
    or str(int(time.time()))
)


# ── Rewriters ────────────────────────────────────────────────────
# Match `from './foo.js'` / `from "../bar.js"` / `import('./baz.js')`
_JS_IMPORT_RE = re.compile(
    r"""(from\s+|import\s*\(\s*)(['"])([./][^'"?#]+\.js)(['"]\)?)""",
    re.MULTILINE,
)


def _bust_js_imports(text: str) -> str:
    """Append ?v=BUILD to every relative .js import in the given source."""
    return _JS_IMPORT_RE.sub(rf"\1\2\3?v={BUILD}\4", text)


def _bust_html(text: str) -> str:
    """Replace static asset URLs in index.html with versioned ones."""
    # CSS links: <link ... href="./css/foo.css" ...>
    text = re.sub(
        r'(<link[^>]+href=)(["\'])(\./css/[^"\']+\.css)(["\'])',
        rf'\1\2\3?v={BUILD}\4',
        text,
    )
    # Main JS entry: <script ... src="./js/app.js"></script>
    text = re.sub(
        r'(<script[^>]+src=)(["\'])(\./js/[^"\']+\.js)(["\'])',
        rf'\1\2\3?v={BUILD}\4',
        text,
    )
    return text


def _inject_api_base(html: str) -> str:
    if not API_BASE:
        return html
    return re.sub(
        r"window\.__RUBY_API_BASE__\s*=\s*window\.__RUBY_API_BASE__\s*\|\|\s*['\"][^'\"]*['\"]\s*;",
        f"window.__RUBY_API_BASE__ = '{API_BASE}';",
        html,
        count=1,
    )


# ── Headers / middleware ─────────────────────────────────────────
@web.middleware
async def no_cache_middleware(request: web.Request, handler):
    response = await handler(request)
    if isinstance(response, web.StreamResponse):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        # Expose our build id so we can confirm "the right one is live"
        response.headers['X-Ruby-Build'] = BUILD
    return response


# ── Route handlers ───────────────────────────────────────────────
async def index(_request: web.Request) -> web.Response:
    path = ROOT / 'index.html'
    try:
        html = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return web.Response(status=404, text='index.html missing')
    html = _inject_api_base(html)
    html = _bust_html(html)
    return web.Response(
        body=html,
        content_type='text/html',
        charset='utf-8',
        headers={'X-Content-Type-Options': 'nosniff'},
    )


async def health(_request: web.Request) -> web.Response:
    return web.json_response({
        'ok': True,
        'service': 'ruby-finance-miniapp',
        'build': BUILD,
        'api_base': API_BASE or None,
    })


async def serve_js(request: web.Request) -> web.Response:
    """Serve a JS file, rewriting relative imports so they too carry ?v=BUILD."""
    rel = request.match_info.get('tail', '')
    # Block path traversal
    if '..' in rel.split('/'):
        return web.Response(status=400, text='nope')
    full = ROOT / 'js' / rel
    if not full.is_file():
        return web.Response(status=404, text='js not found')
    text = full.read_text(encoding='utf-8')
    text = _bust_js_imports(text)
    return web.Response(
        body=text,
        content_type='application/javascript',
        charset='utf-8',
    )


def build_app() -> web.Application:
    app = web.Application(middlewares=[no_cache_middleware])
    app.router.add_get('/', index)
    app.router.add_get('/index.html', index)
    app.router.add_get('/health', health)
    # JS — through the rewriter so internal imports are also versioned
    app.router.add_get('/js/{tail:.+\\.js}', serve_js)
    # Other static assets — straight pass-through (CSS already versioned at <link>)
    for url, dirname in (('/css/', 'css'), ('/assets/', 'assets')):
        full = ROOT / dirname
        if full.is_dir():
            app.router.add_static(url, str(full), show_index=False)
    return app


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    web.run_app(build_app(), host='0.0.0.0', port=port, print=lambda *_: None)
