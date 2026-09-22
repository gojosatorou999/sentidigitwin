"""Execute the map layer specs in Node, so JS bugs fail the build.

``twin-layers.js`` is pure data: layer specs and geometry helpers, no DOM, no
MapLibre. That makes it the one piece of frontend here that can be executed in
a test -- and it is also the piece where a mistake is hardest to notice by
eye, because MapLibre rejects a malformed paint expression by *silently
skipping the layer*. No error, no warning, just a layer that never appears.

Two classes of bug are pinned here:

* **Expression validity** -- a `step` expression whose stops do not strictly
  ascend is rejected at runtime, and the AQI ramp is generated from a table,
  so an edit to that table can break it without touching the expression.
* **Cone geometry** -- the direction maths, and the two cases that must return
  nothing: a camera with no mapped bearing (drawing a north-facing cone would
  claim knowledge nobody has) and a 360-degree dome.

Skipped when Node is absent, so it never blocks a Python-only environment.
"""

import os
import shutil
import subprocess
import textwrap

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LAYERS_JS = os.path.join(REPO_ROOT, "static", "js", "twin-layers.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="Node is not installed")


def run_js(body, tmp_path):
    """Run `body` with twin-layers.js loaded and `L` bound to TwinLayers."""
    script = tmp_path / "check.mjs"
    script.write_text(textwrap.dedent("""
        import fs from "node:fs";
        import vm from "node:vm";
        const sandbox = {{ window: {{}}, console }};
        sandbox.window.window = sandbox.window;
        vm.createContext(sandbox);
        vm.runInContext(fs.readFileSync({source!r}, "utf8"), sandbox);
        const L = sandbox.window.TwinLayers;
        {body}
    """).format(source=LAYERS_JS.replace("\\", "/"), body=body), encoding="utf-8")

    result = subprocess.run(["node", str(script)], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestLayerSpecs:
    def test_every_new_layer_builds(self, tmp_path):
        output = run_js("""
            const names = ["alertZoneLayer", "alertZoneOutlineLayer", "airStationLayer",
              "airStationLabelLayer", "transitVehicleLayer", "transitStalledHaloLayer",
              "cctvConeLayer", "cctvStreamLayer", "flagAreaLayer", "flagGlowLayer"];
            const bad = names.filter((n) => {
              const spec = L[n]("src");
              return !spec || !spec.id || !spec.type || spec.source !== "src";
            });
            console.log(bad.length === 0 ? "OK" : "BAD:" + bad.join(","));
        """, tmp_path)
        assert output == "OK"

    def test_the_aqi_ramp_stops_strictly_ascend(self, tmp_path):
        # MapLibre rejects a step expression whose stops are out of order by
        # skipping the layer entirely, with no console error.
        output = run_js("""
            const expr = L.airStationLayer("src").paint["circle-color"];
            const stops = expr.slice(3).filter((_, i) => i % 2 === 0);
            console.log(stops.every((v, i) => i === 0 || v > stops[i - 1]) ? "OK" : "BAD");
        """, tmp_path)
        assert output == "OK"

    def test_the_alert_layer_uses_cap_severity_not_risk_bands(self, tmp_path):
        # An alert is an authority's statement and keeps the authority's own
        # vocabulary; folding it into the twin's bands would misreport it.
        output = run_js("""
            const expr = JSON.stringify(L.alertZoneLayer("src").paint["fill-color"]);
            console.log(expr.includes("severity") ? "OK" : "BAD");
        """, tmp_path)
        assert output == "OK"


class TestViewCones:
    def test_a_cone_projects_towards_its_bearing(self, tmp_path):
        output = run_js("""
            const cone = L.viewCone({lat: 12.97, lon: 77.59, direction: 90,
                                     camera_type: "fixed"});
            const ring = cone.geometry.coordinates[0];
            console.log(ring.length === 15 && ring[7][0] > 77.59 ? "OK" : "BAD");
        """, tmp_path)
        assert output == "OK"

    def test_a_camera_without_a_bearing_gets_no_cone(self, tmp_path):
        output = run_js("""
            console.log(L.viewCone({lat: 1, lon: 1, camera_type: "fixed"}) === null
                        ? "OK" : "BAD");
        """, tmp_path)
        assert output == "OK"

    def test_a_dome_gets_no_cone(self, tmp_path):
        output = run_js("""
            console.log(L.viewCone({lat: 1, lon: 1, direction: 0, camera_type: "dome"})
                        === null ? "OK" : "BAD");
        """, tmp_path)
        assert output == "OK"

    def test_cones_are_derived_only_for_cameras_that_have_a_bearing(self, tmp_path):
        output = run_js("""
            const cones = L.conesFrom({features: [
              {geometry: {coordinates: [77.59, 12.97]},
               properties: {direction: 45, camera_type: "fixed"}},
              {geometry: {coordinates: [77.60, 12.98]},
               properties: {camera_type: "fixed"}},
            ]});
            console.log(cones.features.length === 1 ? "OK" : "BAD");
        """, tmp_path)
        assert output == "OK"

    def test_optics_match_the_python_side(self, tmp_path):
        """The map and the API must agree about what a camera can see."""
        from twin.cameras import CAMERA_OPTICS

        output = run_js("""
            console.log(JSON.stringify(L.CAMERA_OPTICS));
        """, tmp_path)

        import json

        js_optics = json.loads(output)
        for kind, optics in CAMERA_OPTICS.items():
            assert js_optics[kind]["fov"] == optics["fov"], kind
            assert js_optics[kind]["range"] == optics["range_m"], kind
