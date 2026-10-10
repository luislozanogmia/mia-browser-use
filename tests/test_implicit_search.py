"""Implicit HTML text inputs keep the existing named-search-only exception."""
import pytest
from ghost_chat import needs_approval


@pytest.mark.parametrize('label',['input(): Search [REDACTED]','input(): Buscar','input(text): Search','input(search): Find company'])
def test_observed_named_search_does_not_create_a_write_approval(label):
    assert needs_approval('ghost_key',{'key':'Enter','choice':4},{4:label},'')==''


@pytest.mark.parametrize('label',['input(): Message','input(): Reference','input(password): Search','input(email): Search','textarea: Search',''])
def test_other_enter_targets_keep_the_submit_hold(label):
    assert needs_approval('ghost_key',{'key':'Enter','choice':4},{4:label},'')
