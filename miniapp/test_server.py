import json
import re

from miniapp import server


def test_api_base_is_injected_as_a_json_string(monkeypatch):
    api_base = "https://example.test/path'\\\n</script>"
    monkeypatch.setattr(server, 'API_BASE', api_base)
    html = "<script>window.__RUBY_API_BASE__ = window.__RUBY_API_BASE__ || '';</script>"

    result = server._inject_api_base(html)

    match = re.search(r'window\.__RUBY_API_BASE__ = (.*);', result)
    assert match is not None
    assert json.loads(match.group(1)) == api_base
