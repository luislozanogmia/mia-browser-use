"""Unit builders must not write their journals into the operator's live goals."""

import pytest

from automation_build import BuildJournal


@pytest.fixture(autouse=True)
def isolate_default_build_journals(monkeypatch, tmp_path):
    original = BuildJournal.__init__

    def initialize(self, request="", plan=None, build_id=None, root=None, checkpoint=None):
        return original(self, request=request, plan=plan, build_id=build_id,
                        root=tmp_path / "default-builds" if root is None else root,
                        checkpoint=checkpoint)

    monkeypatch.setattr(BuildJournal, "__init__", initialize)
