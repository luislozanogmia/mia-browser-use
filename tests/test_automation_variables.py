"""Variable ordering must match what Play has actually copied at each step."""

import pytest

from automations import clean, validate_variables


OPEN = {"do": "open", "url": "https://example.com/"}
COPY = {"do": "copy", "css": "h1", "as": "name"}
TYPE = {"do": "type", "text": "Message", "value": "Hi {{name}}"}


@pytest.mark.parametrize("consumer", [
    TYPE,
    {"do": "click", "text": "{{name}}"},
    {"do": "copy", "text": "{{name}}", "as": "heading"},
    {"do": "key", "text": "{{name}}", "key": "Enter"},
    {"do": "open", "url": "{{name}}"},
    {"do": "append", "sheet": "https://docs.google.com/spreadsheets/d/test",
     "tab": "People", "row": ["{{name}}"]},
    {"do": "append", "sheet": "{{name}}", "tab": "People", "row": ["literal"]},
    {"do": "append", "sheet": "https://docs.google.com/spreadsheets/d/test",
     "tab": "{{name}}", "row": ["literal"]},
    {"do": "append", "sheet": "https://docs.google.com/spreadsheets/d/test",
     "tab": "People", "unique": "{{name}}", "row": ["literal"]},
])
def test_copy_must_precede_each_kind_of_variable_consumer(consumer):
    with pytest.raises(ValueError, match=r"step 2 uses \{\{name\}\} without copying it first"):
        clean({"name": "Ordering", "steps": [OPEN, consumer, COPY]})
    assert clean({"name": "Ordering", "steps": [OPEN, COPY, consumer]})["steps"][-1] == consumer


def test_declared_input_is_available_before_a_copy_with_the_same_name():
    item = clean({"name": "Input then copy", "inputs": [{"name": "name"}],
                  "steps": [OPEN, TYPE, COPY, TYPE]})
    validate_variables(item, {"name": "Luis"})


def test_loop_link_and_page_url_remain_available():
    item = clean({"name": "Loop", "each": {"links": "/person/"}, "steps": [OPEN,
                  {"do": "open", "url": "{{link}}"},
                  {"do": "append", "sheet": "https://docs.google.com/spreadsheets/d/test",
                   "tab": "People", "row": ["{{page_url}}", "{{link}}"]}]})
    validate_variables(item)


def template_script(steps):
    return clean({"name": "Template", "inputs": [{"name": "template"}], "steps": [OPEN, *steps]})


TEMPLATE_TYPE = {"do": "type", "text": "Message", "value": "{{template}}"}


def test_input_template_dependency_is_checked_when_the_template_is_used():
    item = template_script([TEMPLATE_TYPE, COPY])
    with pytest.raises(ValueError, match=r"step 2 uses \{\{name\}\}"):
        validate_variables(item, {"template": "Hi {{name}}"})
    # Literal values are usable even though the script contains a later copy.
    validate_variables(item, {"template": "Hi Luis"})


def test_input_template_can_use_a_value_copied_before_its_first_use():
    validate_variables(template_script([COPY, TEMPLATE_TYPE]), {"template": "Hi {{ name }}"})


def test_unknown_template_variables_are_rejected_at_the_consumer():
    with pytest.raises(ValueError, match=r"step 3 uses \{\{unknown\}\}"):
        validate_variables(template_script([COPY, TEMPLATE_TYPE]), {"template": "Hi {{unknown}}"})


def test_unused_input_templates_do_not_need_copied_values():
    validate_variables(template_script([{"do": "click", "text": "Next"}]),
                       {"template": "Hi {{not_copied}}"})


def test_copied_value_overrides_an_input_template_with_the_same_name():
    item = template_script([{"do": "copy", "css": "h1", "as": "template"}, TEMPLATE_TYPE])
    validate_variables(item, {"template": "Hi {{not_copied}}"})


def test_append_keeps_input_contents_literal_like_the_single_expansion_runtime():
    item = template_script([{"do": "append", "sheet": "https://docs.google.com/spreadsheets/d/test",
                             "tab": "People", "row": ["{{template}}"]}])
    validate_variables(item, {"template": "Hi {{not_copied}}"})


def test_target_text_expands_once_even_when_type_values_expand_twice():
    item = template_script([{"do": "type", "text": "{{template}}", "value": "literal"}])
    validate_variables(item, {"template": "Field {{literal_label}}"})
