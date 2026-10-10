"""Execute the page's field-description helpers without a browser session."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


SOURCE = (Path(__file__).resolve().parents[1] / "extension" / "ghost_page.js").read_text()
HELPERS = SOURCE[SOURCE.index("  function labelText("):SOURCE.index("  function parentOf(")]
NODE = shutil.which("node")


def describe_case(case):
    if not NODE:
        pytest.skip("Node.js is needed to execute the production JavaScript helpers")
    # Model the DOM properties actually used by the helpers. Textarea textContent
    # deliberately includes default contents, as it does in the browser.
    program = """
const Node = {ELEMENT_NODE: 1, TEXT_NODE: 3};
function element(spec) {
  if (typeof spec === 'string') return {nodeType: 3, textContent: spec};
  const children = (spec.children || []).map(element);
  return {
    nodeType: 1, tagName: (spec.tag || 'span').toUpperCase(),
    childNodes: children, textContent: children.map(n => n.textContent).join(''),
    value: spec.value || '', isContentEditable: !!spec.editable,
    labels: (spec.labels || []).map(element),
    getAttribute: key => (spec.attrs || {})[key] ?? null,
    getRootNode: () => ({getElementById: id => refs[id]}),
  };
}
const data = CASE;
const refs = Object.fromEntries(Object.entries(data.refs || {}).map(([id, s]) => [id, element(s)]));
const node = element(data.node);
HELPERS
process.stdout.write(JSON.stringify(describe(node, data.node.tag || 'input', 7)));
""".replace("CASE", json.dumps(case)).replace("HELPERS", HELPERS)
    result = subprocess.run([NODE, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("tag", ["input", "textarea"])
def test_filled_field_keeps_name_and_never_discloses_value(tag):
    node = {"tag": tag, "attrs": {"aria-label": "Search query"}}
    empty = describe_case({"node": node})
    node.update(value="PRIVATE_CURRENT_VALUE", children=["PRIVATE_DEFAULT_VALUE"])
    filled = describe_case({"node": node})
    assert empty == f"[7] {tag}{'()' if tag == 'input' else ''}: Search query"
    assert filled == empty + " [REDACTED]"
    assert "PRIVATE" not in filled


def test_aria_label_precedes_other_names():
    line = describe_case({"node": {
        "tag": "input", "value": "secret", "attrs": {
            "aria-label": "Account name", "aria-labelledby": "other",
            "placeholder": "Placeholder", "title": "Title"},
        "labels": [{"children": ["Associated label"]}]},
        "refs": {"other": {"children": ["Referenced label"]}}})
    assert line == "[7] input(): Account name [REDACTED]"


def test_multiple_id_references_use_control_root_and_skip_missing_ids():
    line = describe_case({"node": {"tag": "textarea", "attrs": {
        "aria-labelledby": "missing heading detail"}}, "refs": {
        "heading": {"children": ["  Delivery "]},
        "detail": {"children": [{"children": [" instructions\n"]}]}}})
    assert line == "[7] textarea: Delivery instructions"


def test_wrapped_and_explicit_labels_do_not_include_textarea_defaults():
    line = describe_case({"node": {"tag": "textarea", "value": "CURRENT_SECRET",
        "children": ["DEFAULT_SECRET"], "labels": [{"tag": "label", "children": [
            "Message", {"tag": "textarea", "children": ["DEFAULT_SECRET"]}]}]}})
    assert line == "[7] textarea: Message [REDACTED]"


def test_reference_to_form_control_cannot_reveal_default_contents():
    line = describe_case({"node": {"tag": "input", "attrs": {
        "aria-labelledby": "secret-field", "placeholder": "Reference"}},
        "refs": {"secret-field": {"tag": "textarea", "children": ["DEFAULT_SECRET"]}}})
    assert line == "[7] input(): Reference"


@pytest.mark.parametrize("attrs, expected", [
    ({"placeholder": "  Search\narticles "}, "Search articles"),
    ({"title": "Account"}, "Account"),
    ({}, "input"),
])
def test_unlabelled_fields_have_safe_fallbacks(attrs, expected):
    line = describe_case({"node": {"tag": "input", "attrs": attrs, "children": ["SECRET"]}})
    assert line == f"[7] input(): {expected}"


def test_password_type_remains_visible_for_sensitive_field_guard():
    line = describe_case({"node": {"tag": "input", "attrs": {
        "type": "password", "aria-label": "Password"}, "value": "SECRET"}})
    assert line == "[7] input(password): Password [REDACTED]"


def test_label_is_bounded_and_normalized():
    line = describe_case({"node": {"tag": "input", "attrs": {"aria-label": "x" * 200}}})
    assert line == "[7] input(): " + "x" * 100


def test_links_keep_existing_visible_text_and_url():
    line = describe_case({"node": {"tag": "a", "attrs": {"href": "/next"}, "children": ["Next"]}})
    assert line == "[7] link: Next (/next)"


@pytest.mark.parametrize("attrs", [{"disabled": ""}, {"aria-disabled": "true"}])
def test_disabled_next_control_exposes_state_for_pagination(attrs):
    line = describe_case({"node": {"tag": "button", "attrs": attrs, "children": ["Next page"]}})
    assert line == "[7] button: Next page [disabled]"


def test_aria_enabled_control_does_not_claim_disabled():
    line = describe_case({"node": {"tag": "button", "attrs": {"aria-disabled": "false"}, "children": ["Next page"]}})
    assert line == "[7] button: Next page"
