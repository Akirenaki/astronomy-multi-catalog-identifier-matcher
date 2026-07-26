"""Regression test for F7/TICKET-F: degToDMS/degToHMS in celestial-viz.js must
never emit an invalid "60" seconds/minutes component when a value rounds to
exactly a minute (or hour) boundary.

celestial-viz.js is browser-only code with no existing JS test runner/framework
in this repository (package.json, jest, etc. are all absent), so rather than
adding new JS test infrastructure just for this one fix, this test shells out to
Node (already present in the environment) to execute the *actual* shipped
degToDMS/degToHMS source extracted from the real file -- exercising the real
production code, not a re-implementation that could drift from it.
"""

import re
import subprocess
from pathlib import Path

import pytest

_JS_FILE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "celestial-viz.js"


def _extract_function(name: str) -> str:
    """Pull a top-level `function name(...) { ... }` block out of the JS file
    by brace-matching, so the test runs the real shipped source verbatim."""
    src = _JS_FILE.read_text()
    match = re.search(rf"function {name}\(", src)
    assert match is not None, f"{name} not found in {_JS_FILE}"
    start = match.start()
    brace_start = src.index("{", match.end())
    depth = 0
    for i in range(brace_start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"Could not find matching closing brace for {name}")


def _run_node(js_snippet: str) -> str:
    result = subprocess.run(["node", "-e", js_snippet], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return result.stdout.strip()


@pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node is not available in this environment",
)
def test_deg_to_dms_carries_seconds_into_minutes_and_degrees_at_boundary():
    deg_to_dms_src = _extract_function("degToDMS")
    output = _run_node(f"{deg_to_dms_src}\nconsole.log(degToDMS(10.999999722222222, true));")
    assert output == "+11\u00b000'00\""
    assert "60" not in output.split("\u00b0")[1]  # neither the minutes nor seconds component is "60"


@pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node is not available in this environment",
)
def test_deg_to_hms_carries_seconds_into_minutes_and_hours_at_boundary():
    deg_to_hms_src = _extract_function("degToHMS")
    # RA equivalent of the same near-boundary value used for degToDMS above.
    boundary_ra_deg = 10.999999722222222 * 15
    output = _run_node(f"{deg_to_hms_src}\nconsole.log(degToHMS({boundary_ra_deg}));")
    assert output == "11h00m00s"
    assert "60" not in output
