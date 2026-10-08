"""Copy retains actual rendered paragraphs without leaking form/editor values."""
import asyncio
import json
from pathlib import Path

import pytest

from tests.test_selector_target import TargetBrowser, task_for, run_js
from tests.test_automations import play_hub


@pytest.mark.parametrize('rendered', ['Leading inline link and trailing prose.\n\nSecond paragraph.', '[17] literal text\nNext line'])
def test_copy_uses_rendered_spacing_and_keeps_literal_numbered_lines(tmp_path, rendered):
    async def main():
        hub = play_hub(TargetBrowser(), tmp_path)
        async def call(command, args):
            return True, {'content': 'Leading\n[0] link: inline link (/topic)\nand trailing prose.', 'rendered_text': rendered}
        hub.call = call
        values = {}
        await hub.play_step(task_for(hub), {'do': 'copy', 'css': '#article', 'as': 'intro'}, values)
        assert values['intro'] == rendered
    asyncio.run(main())


def test_empty_rendered_selector_does_not_copy_hidden_fallback(tmp_path):
    async def main():
        hub = play_hub(TargetBrowser(), tmp_path)
        async def call(command, args):
            return True, {'content': 'Hidden fallback', 'rendered_text': ''}
        hub.call = call
        with pytest.raises(RuntimeError, match='no visible text'):
            await hub.play_step(task_for(hub), {'do': 'copy', 'css': '#hidden', 'as': 'intro'}, {})
    asyncio.run(main())


def selector_cases():
    source = (Path(__file__).resolve().parents[1] / 'extension/ghost_page.js').read_text()
    helper = source[source.index('  function selectorText('):source.index('  /** Walk the page, number')]
    return run_js('''
const parentOf = n => n.parent || null;
const isVisible = n => !n.hidden;
const shadowOf = n => n.shadow || null;
const el = (tag, text, children=[]) => ({tagName:tag, innerText:text, children});
''' + helper + '''
const prose = el('P', 'First inline link.\\n\\nSecond paragraph.', [el('A','inline link.')]);
const control = el('DIV','PRIVATE', [el('TEXTAREA','PRIVATE')]);
const editor = el('DIV','PRIVATE'); editor.isContentEditable = true;
const child = el('P','PRIVATE'); child.parent = editor;
const hidden = el('P','Hidden'); hidden.parent = {hidden:true};
const frame = el('DIV','frame', [el('IFRAME','private')]);
const shadow = el('DIV','private'); shadow.shadow = {};
const excessive = el('DIV','many', Array.from({length:10001}, () => el('SPAN','x')));
process.stdout.write(JSON.stringify({prose:selectorText(prose,4000), bounded:selectorText(prose,5),
 control:selectorText(control,4000), editor:selectorText(editor,4000), child:selectorText(child,4000),
 hidden:selectorText(hidden,4000), frame:selectorText(frame,4000), shadow:selectorText(shadow,4000),
 excessive:selectorText(excessive,4000)}));
''')


def test_production_plain_text_guard_spacing_bounds_and_private_subtrees():
    result = selector_cases()
    assert result['prose'] == 'First inline link.\n\nSecond paragraph.'
    assert result['bounded'] == 'First'
    assert result['hidden'] == ''
    assert all(result[key] is None for key in ['control', 'editor', 'child', 'frame', 'shadow', 'excessive'])


def test_production_background_forwards_rendered_text_but_preserves_sheet_override():
    source = (Path(__file__).resolve().parents[1] / 'extension/background.js').read_text()
    function = source[source.index('async function readPage('):source.index('\n}', source.index('async function readPage(')) + 2]
    result = run_js('''
const getActiveTabId = async () => 7, actorOf = () => 'a';
const readTabContent = async () => ({text:'numbered', rendered_text:'Rendered paragraph.'});
let sheet = false;
const withSheetCells = async (tab,text) => sheet ? 'CSV cells' : text;
const chrome = {tabs:{get:async () => ({url:'https://example.test',title:'Example'})}};
''' + function + '''
(async () => { const plain = await readPage({selector:'#article'}); sheet = true;
const cells = await readPage({selector:'#article'}); process.stdout.write(JSON.stringify({plain,cells})); })();
''')
    assert result['plain']['rendered_text'] == 'Rendered paragraph.'
    assert result['cells']['rendered_text'] is None
    assert result['cells']['content'] == 'CSV cells'


def test_real_headless_chrome_inline_links_and_paragraphs(tmp_path):
    """Use an isolated profile: innerText spacing is a browser behavior, not a DOM mock."""
    import html
    import re
    import shutil
    import subprocess
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required for actual rendered-text verification')
    helper = (Path(__file__).resolve().parents[1] / 'extension/ghost_page.js').read_text()
    fixture = tmp_path / 'copy.html'
    fixture.write_text('''<!doctype html><html><body>
<div id="article"><p>Leading <a href="#topic">inline link</a> and <em>trailing prose.</em></p><p>Second paragraph.</p></div>
<div id="private"><textarea>PRIVATE_DEFAULT</textarea><input value="PRIVATE_CURRENT"></div>
<div contenteditable="true"><p id="editor">PRIVATE_EDITOR</p></div>
<div style="display:none"><p id="hidden">PRIVATE_HIDDEN</p></div>
<script>globalThis.__ghostBuild = 'rendered-copy-test';</script>
<script>''' + helper + '''</script><script>
const results = {};
for (const id of ['article','private','editor','hidden']) results[id] = __ghostPage.enumerate('test',4000,'#'+id).rendered_text;
const proof = document.createElement('pre'); proof.id = 'proof'; proof.textContent = JSON.stringify(results); document.body.append(proof);
</script></body></html>''')
    done = subprocess.run([chrome, '--headless=new', '--disable-gpu', '--no-first-run',
                           '--disable-background-networking', f'--user-data-dir={tmp_path / "profile"}',
                           '--dump-dom', fixture.as_uri()], capture_output=True, text=True, timeout=30, check=True)
    match = re.search(r'<pre id="proof">(.*?)</pre>', done.stdout, re.S)
    assert match, done.stderr[-1000:]
    result = json.loads(html.unescape(match[1]))
    assert result == {'article': 'Leading inline link and trailing prose.\n\nSecond paragraph.',
                      'private': None, 'editor': None, 'hidden': ''}
