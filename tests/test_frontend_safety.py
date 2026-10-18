"""Offline checks for the monolithic template; Node is optional for syntax QA."""
import json
from pathlib import Path
import re
import shutil
import subprocess

from bs4 import BeautifulSoup
import pytest


TEMPLATE = Path(__file__).resolve().parents[1] / 'templates' / 'index.html'


def test_frontend_assets_are_local():
    soup = BeautifulSoup(TEMPLATE.read_text(encoding='utf-8'), 'html.parser')
    assets = [n.get('src') for n in soup.select('script[src]')]
    assets += [n.get('href') for n in soup.select('link[href]')]
    assert assets and all(url.startswith('/static/') for url in assets)


def test_inline_javascript_parses():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is not available for JS syntax verification')
    soup = BeautifulSoup(TEMPLATE.read_text(encoding='utf-8'), 'html.parser')
    for script in soup.find_all('script', src=False):
        result = subprocess.run([node, '--check'], input=script.get_text(),
                                text=True, encoding='utf-8', capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr


def test_metadata_helpers_escape_html_and_inline_arguments():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is not available for helper verification')
    template = TEMPLATE.read_text(encoding='utf-8')
    helpers = '\n'.join(re.findall(r'function (?:escapeHtml|safeParam)\(value\) \{.*?\n\}', template, re.S))
    code = helpers + '\nconsole.log(JSON.stringify([escapeHtml(' + json.dumps('<b>"Song\'s & artist</b>') + '), safeParam("Artist\'s Record.wav")]));'
    result = subprocess.run([node], input=code, text=True, encoding='utf-8',
                            capture_output=True, timeout=10, check=True)
    escaped, encoded = json.loads(result.stdout)
    assert '<' not in escaped and '>' not in escaped and '"' not in escaped
    assert '&lt;b&gt;' in escaped and '&#39;' in escaped and '&amp;' in escaped
    assert "'" not in encoded and '%27' in encoded
