"""Doctor reports missing Tcl scripts without needing a desktop or real Tk."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sys
from types import ModuleType
from unittest.mock import Mock, patch

import pytest

from tools.dependency_manager import cli


class FakeTclError(Exception):
    pass


def toolkit():
    module = ModuleType("tkinter")
    for name, value in {"TclError": FakeTclError, "TkVersion": 8.6, "Tcl": Mock()}.items():
        setattr(module, name, value)
    return module


def doctor(module):
    output, error = io.StringIO(), io.StringIO()
    with patch.dict(sys.modules, {"tkinter": module}), redirect_stdout(output), redirect_stderr(error):
        code = cli.main(["doctor", "--json"])
    assert error.getvalue() == ""
    assert "Traceback" not in output.getvalue()
    return code, json.loads(output.getvalue())


@pytest.mark.parametrize("phase", ["construct", "patchlevel"])
def test_missing_tcl_scripts_are_structured_failure(phase):
    module = toolkit()
    failure = FakeTclError("Can't find a usable init.tcl; Tcl wasn't installed properly")
    if phase == "construct":
        module.Tcl.side_effect = failure
    else:
        module.Tcl.return_value.eval.side_effect = failure
    code, result = doctor(module)
    assert code == 2
    assert result["schema_version"] == 1
    assert result["operation"] == "doctor"
    assert result["status"] == "failed"
    assert result["tcl_initialized"] is False
    assert result["gui_capability"] == "unavailable"
    assert result["gui_rendering_verified"] is False
    assert "init.tcl" in result["errors"][0]
    assert "GUI unavailable" in result["errors"][0]


def test_tkinter_import_failure_still_returns_json():
    code, result = doctor(None)
    assert code == 2
    assert result["status"] == "failed"
    assert result["gui_capability"] == "unavailable"
    assert result["errors"]


def test_successful_tcl_probe_does_not_claim_gui_rendering():
    module = toolkit()
    module.Tcl.return_value.eval.return_value = "8.6.15"
    code, result = doctor(module)
    assert code == 0
    assert result["status"] == "success"
    assert result["tcl_patchlevel"] == "8.6.15"
    assert result["tcl_initialized"] is True
    assert result["gui_capability"] == "unverified"
    assert result["gui_rendering_verified"] is False
    assert result["warnings"]
    assert result["errors"] == []
