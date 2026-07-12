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
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

ROOT = Path(__file__).resolve().parent
API_BASE = os.environ.get('API_BASE_URL', '').rstrip('/')
BUILD = (
    os.environ.get('BUILD_ID')
    or os.environ.get('RAILWAY_GIT_COMMIT_SHA', '')[:12]
    or str(int(time.time()))
)


# ── Rewriters ────────────────────────────────────────────────────
# Match three import shapes:
#   from './foo.js'          (bare / named / default / namespace)
#   import('./bar.js')       (dynamic)
#   import './side-fx.js'    (side-effect-only, no `from`)
# Both quote styles, both relative prefixes.
_JS_IMPORT_RE = re.compile(
    r"""((?:from|import)\s+|import\s*\(\s*)(['"])([./][^'"?#]+\.js)\2""",
    re.MULTILINE,
)
_INLINE_SCRIPT_RE = re.compile(
    r'<script(?![^>]*\bsrc\s*=)(?P<attrs>[^>]*)>',
    re.IGNORECASE,
)


def _bust_js_imports(text: str) -> str:
    """Append ?v=BUILD to every relative .js import in the given source."""
    return _JS_IMPORT_RE.sub(rf"\1\2\3?v={BUILD}\2", text)


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


def _nonce_inline_scripts(text: str, nonce: str) -> str:
    """Authorize inline bootstrap scripts with this response's CSP nonce."""
    return _INLINE_SCRIPT_RE.sub(
        lambda match: f'<script nonce="{nonce}"{match.group("attrs")}>',
        text,
    )


def _inject_api_base(html: str) -> str:
    if not API_BASE:
        return html
    encoded_api_base = json.dumps(API_BASE, ensure_ascii=False)
    # JSON quoting protects the JavaScript string; escaping HTML-significant
    # characters also prevents a configured value from closing the script tag.
    encoded_api_base = (encoded_api_base
                        .replace('&', r'\u0026')
                        .replace('<', r'\u003c')
                        .replace('>', r'\u003e'))
    return re.sub(
        r"window\.__RUBY_API_BASE__\s*=\s*window\.__RUBY_API_BASE__\s*\|\|\s*['\"][^'\"]*['\"]\s*;",
        lambda _match: f"window.__RUBY_API_BASE__ = {encoded_api_base};",
        html,
        count=1,
    )


def _api_connect_source() -> str | None:
    """Return a header-safe API origin, never untrusted URL text."""
    if not API_BASE:
        return None
    try:
        parsed = urlsplit(API_BASE)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        host = parsed.hostname.encode('idna').decode('ascii')
    except UnicodeError:
        return None
    if ':' in host:
        if not re.fullmatch(r'[0-9a-fA-F:]+', host):
            return None
        host = f'[{host}]'
    elif not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        return None
    authority = f'{host}:{port}' if port is not None else host
    return f'{parsed.scheme}://{authority}'


def _content_security_policy(nonce: str) -> str:
    connect_sources = ["'self'"]
    api_source = _api_connect_source()
    if api_source:
        connect_sources.append(api_source)
    # Existing components use trusted inline style attributes. Keep that
    # compatibility exception scoped to styles; scripts remain nonce-only.
    directives = (
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' https://telegram.org",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        f"connect-src {' '.join(connect_sources)}",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'self' https://telegram.org https://*.telegram.org",
        "worker-src 'self' blob:",
        "manifest-src 'self'",
    )
    return '; '.join(directives)


# ── Headers / middleware ─────────────────────────────────────────
def _apply_response_headers(response: web.StreamResponse, nonce: str) -> None:
    if isinstance(response, web.StreamResponse):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        # Expose our build id so we can confirm "the right one is live"
        response.headers['X-Ruby-Build'] = BUILD
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = (
            'camera=(), microphone=(), geolocation=(), payment=(), usb=(), '
            'serial=(), accelerometer=(), gyroscope=(), magnetometer=()'
        )
        response.headers['Content-Security-Policy'] = _content_security_policy(nonce)


@web.middleware
async def no_cache_middleware(request: web.Request, handler):
    nonce = secrets.token_urlsafe(32)
    request['csp_nonce'] = nonce
    try:
        response = await handler(request)
    except web.HTTPException as exception:
        _apply_response_headers(exception, nonce)
        raise
    _apply_response_headers(response, nonce)
    return response


# ── Route handlers ───────────────────────────────────────────────
async def index(request: web.Request) -> web.Response:
    path = ROOT / 'index.html'
    try:
        html = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return web.Response(status=404, text='index.html missing')
    html = _inject_api_base(html)
    html = _bust_html(html)
    html = _nonce_inline_scripts(html, request['csp_nonce'])
    return web.Response(
        body=html,
        content_type='text/html',
        charset='utf-8',
    )


def _legal_document(filename: str, nonce: str) -> web.Response:
    path = ROOT / filename
    try:
        html = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return web.Response(status=404, text='legal document missing')
    return web.Response(
        body=_nonce_inline_scripts(_bust_html(html), nonce),
        content_type='text/html',
        charset='utf-8',
    )


async def privacy(request: web.Request) -> web.Response:
    return _legal_document('privacy.html', request['csp_nonce'])


async def terms(request: web.Request) -> web.Response:
    return _legal_document('terms.html', request['csp_nonce'])


async def favicon(_request: web.Request) -> web.Response:
    return web.Response(status=204)


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
    app.router.add_get('/privacy', privacy)
    app.router.add_get('/privacy.html', privacy)
    app.router.add_get('/terms', terms)
    app.router.add_get('/terms.html', terms)
    app.router.add_get('/favicon.ico', favicon)
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
