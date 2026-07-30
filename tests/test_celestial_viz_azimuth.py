"""Regression test for F1/TICKET-1: the 3D scene's star and equinox-marker
placement in celestial-viz.js must land at the same azimuth as the
independently-computed toHorizontal() text, for every (ra, dec, lat, lst)
combination -- not just the ones where the previous mirror bug happened to
be numerically invisible (az == 0 or 180, or LST == 0).

Implementation note: the architectural review that flagged this bug
recommended fixing it by flipping the sign of the LST angle used to build
`qSpin`. That recommendation was checked against the real shipped
`raDecLocalDir()`/quaternion pipeline (run through the actual three.js
package this app depends on, not a reimplementation) and found not to
fix the mismatch -- `raDecLocalDir()`'s RA negation is a reflection, and
a reflection composed with a rotation is not undone by negating the
rotation's own angle (Mx . Ry(theta) == Ry(-theta) . Mx algebraically: the
reflection just ends up wrapping the other angle, it never cancels). This
was verified numerically for a dozen (ra, dec, lat, lst) combinations,
including the review's own LST=0 reproduction case, for which negating
qSpin's sign provably cannot change the output at all since qSpin is the
identity rotation when lst=0.

The actual fix implemented negates the resulting *world* X coordinate at
the two points a rotated `raDecLocalDir()` vector becomes a scene position
(star placement in updateStar(), equinox marker in layout()), leaving
raDecLocalDir(), toHorizontal(), toEcliptic(), qTilt, and qSpin untouched.
This test encodes that fix's correctness directly against toHorizontal(),
using the same JS-source-extraction-via-Node-subprocess technique as
tests/test_celestial_viz_dms_rounding.py, so it exercises the real shipped
code rather than a parallel reimplementation that could drift from it.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

_JS_FILE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "celestial-viz.js"
_NODE_MODULES = _JS_FILE.parent.parent.parent.parent / "node_modules"

_NODE_AVAILABLE = subprocess.run(["which", "node"], capture_output=True).returncode == 0
_THREE_AVAILABLE = (_NODE_MODULES / "three").exists()

# (ra_deg, dec_deg, lat_deg, lst_hours) -- spans due-north/due-south, LST=0
# (the review's own reproduction case), LST offsets both ahead of and behind
# the object's RA, and near-polar declinations.
_CASES = [
    (90.0, 0.0, 0.0, 0.0),  # review's exact reproduction case
    (0.0, 30.0, 40.0, 0.0),  # due north
    (180.0, -30.0, 40.0, 12.0),  # due south
    (200.0, 5.0, 60.0, 20.0),  # LST well ahead of RA/15
    (30.0, 5.0, 60.0, 8.0),  # LST well behind RA/15
    (123.4, 89.0, 51.0, 7.0),  # near north celestial pole
    (50.0, -89.5, -10.0, 4.0),  # near south celestial pole
    (101.287, -16.716, 52.0, 6.0),  # Sirius-like
    (279.235, 38.784, 10.0, 18.0),  # Vega-like
]


def _extract_function(name: str, src: str) -> str:
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


def _extract_lines(start_anchor: str, end_anchor: str, src: str) -> str:
    """Pull the literal source text from the line containing start_anchor
    through the line containing end_anchor (inclusive), so this test runs
    the real shipped qTilt/qSpin/qEquat construction rather than a
    hand-retyped copy that could silently drift from it."""
    start = src.index(start_anchor)
    end = src.index(end_anchor, start)
    end_of_line = src.index("\n", end)
    return src[start:end_of_line]


def _run_node(js_snippet: str) -> str:
    result = subprocess.run(["node", "-e", js_snippet], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return result.stdout.strip()


@pytest.mark.skipif(not _NODE_AVAILABLE, reason="node is not available in this environment")
@pytest.mark.skipif(
    not _THREE_AVAILABLE,
    reason="three.js is not installed in node_modules (npm install)",
)
def test_3d_star_and_equinox_placement_azimuth_matches_toHorizontal():
    src = _JS_FILE.read_text()
    to_horizontal_src = _extract_function("toHorizontal", src)
    ra_dec_local_dir_src = _extract_function("raDecLocalDir", src)
    q_construction_src = _extract_lines(
        "var qTilt = new THREE.Quaternion()", "var qEquat = qTilt.clone().multiply(qSpin);", src
    )

    # Confirm the fix (world.x negation) is present at both placement sites;
    # the qTilt/qSpin/qEquat construction and raDecLocalDir() above are
    # extracted directly from the file, so only this one-line negation
    # still needs a literal-presence check rather than extraction, since it
    # isn't inside a named, brace-matchable function.
    assert "world.x = -world.x;" in src, "expected fix (world.x negation) not found in updateStar()"
    assert "v.x = -v.x;" in src, "expected fix (v.x negation) not found in layout()"

    script = f"""
const THREE = require({json.dumps(str(_NODE_MODULES / "three"))});
const D2R = Math.PI / 180, R2D = 180 / Math.PI;

{to_horizontal_src}
{ra_dec_local_dir_src}

function starWorldPosition(raDeg, decDeg, lat, lst) {{
  var X_AXIS = new THREE.Vector3(1, 0, 0), Y_AXIS = new THREE.Vector3(0, 1, 0);
  {q_construction_src}
  var dir = raDecLocalDir(THREE, raDeg, decDeg);
  var world = dir.clone().applyQuaternion(qEquat);
  world.x = -world.x; // the shipped fix
  return world;
}}

var cases = {json.dumps(_CASES)};
var results = cases.map(function (c) {{
  var h = toHorizontal(c[0], c[1], c[2], c[3]);
  var world = starWorldPosition(c[0], c[1], c[2], c[3]);
  // File's own stated convention: North = -Z, East = +X.
  var az3d = Math.atan2(world.x, -world.z) * R2D;
  if (az3d < 0) az3d += 360;
  var alt3d = Math.asin(Math.max(-1, Math.min(1, world.y))) * R2D;
  return [h.az, h.alt, az3d, alt3d];
}});
console.log(JSON.stringify(results));
"""
    output = _run_node(script)
    results = json.loads(output)
    assert len(results) == len(_CASES)
    for case, (true_az, true_alt, az3d, alt3d) in zip(_CASES, results):
        assert abs(az3d - true_az) < 0.01, f"azimuth mismatch for {case}: 3D={az3d} vs toHorizontal={true_az}"
        assert abs(alt3d - true_alt) < 0.01, f"altitude mismatch for {case}: 3D={alt3d} vs toHorizontal={true_alt}"
