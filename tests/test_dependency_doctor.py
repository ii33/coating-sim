"""Terminal doctor requires standard-library tooling, never Tcl/Tk or a display."""

from contextlib import redirect_stdout
import io
import json
import sys
from unittest.mock import patch

from tools.dependency_manager import cli


def doctor(modules):
    output = io.StringIO()
    with patch.dict(sys.modules, modules), redirect_stdout(output):
        code = cli.main(["doctor", "--json"])
    assert "Traceback" not in output.getvalue()
    return code, json.loads(output.getvalue())


def test_no_tkinter_is_required():
    code, result = doctor({"tkinter": None})
    assert code == 0
    assert result["status"] == "success"
    assert result["interface"] == "terminal"
    assert result["third_party_dependencies"] == []
    assert result["venv"] is True


def test_missing_ensurepip_is_structured_failure():
    code, result = doctor({"ensurepip": None})
    assert code == 2
    assert result["status"] == "failed"
    assert result["errors"]
