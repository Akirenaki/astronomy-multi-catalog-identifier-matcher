/**
 * Celestial coordinate 3D visualiser.
 *
 * This is a self-contained, lazily-initialised widget. Nothing in this file
 * runs at page load: three.js is only fetched from the CDN, and the scene is
 * only built, the first time `CelestialViz.mount()` is actually called (see
 * result.html, which calls it on first click of the side-panel toggle). The
 * page never waits on this file or on three.js.
 *
 * -- Physics note (why this differs from the original prototype) --------
 * The prototype this was built from (`celestial_coordinates.html`) oriented
 * the equatorial/ecliptic groups by mutating `.rotation.x` / `.rotation.y`
 * on Euler-ordered Object3Ds, sometimes twice, plus a dead no-op rotation.
 * That's not necessarily *wrong*, but it's unverifiable by inspection --
 * you can't tell whether it's physically correct just by reading it. It
 * also had one genuine bug: the vernal-equinox marker was placed along
 * local -Z while the star-placement formula puts RA=0 on local +Z, so the
 * marker didn't actually sit where RA=0 stars were being drawn.
 *
 * Here, each rotation is built as a named quaternion about a named physical
 * axis, and composed in an explicit, stated order:
 *
 *   qTilt  = rotation about local X (East-West axis) by (lat - 90)
 *            -> sends local +Y (pole) to world (0, sin(lat), -cos(lat)),
 *               i.e. altitude = lat above the North point. Verified by
 *               construction below, and cross-checked numerically against
 *               toHorizontal() for RA=0/Dec=0 at several (lat, LST) pairs.
 *   qSpin  = rotation about local Y by (LST * 15 degrees)
 *            -> applied *before* qTilt (i.e. qEquat = qTilt * qSpin, so
 *               qSpin acts first in local space), representing sidereal
 *               rotation about the (not-yet-tilted) polar axis. By the
 *               standard conjugation identity this is equivalent to
 *               spinning about the *already-tilted* pole after tilting --
 *               so which you do first doesn't matter, which is precisely
 *               what made the prototype's approach hard to verify (it
 *               mixed both without being explicit about either).
 *   qEquat = qTilt.multiply(qSpin)
 *   qObl   = rotation about local Z (the equatorial frame's own RA=0 axis,
 *            i.e. the line of nodes) by the obliquity epsilon
 *   qEclip = qEquat.multiply(qObl)
 *            -> tilts the ecliptic relative to the equator about the line
 *               of nodes, then carries that into world space via the
 *               equatorial frame's own orientation.
 *
 * Both qEquat and qEclip were checked by hand against toHorizontal()/
 * toEcliptic() for a handful of (RA, Dec, lat, LST, obliquity) combinations
 * before being wired up here.
 *
 * -- Azimuth mirror fix (2026-07) ----------------------------------------
 * The "checked by hand" claim above turned out to only have been exercised
 * at LST=0 / meridian-crossing cases, where the bug below is numerically
 * invisible. A later audit found the 3D scene placed stars at the mirror
 * image of their true azimuth (east/west flipped) versus toHorizontal(),
 * for any case with a non-degenerate LST. Root cause: raDecLocalDir()
 * negates RA (`sin(-a)`) so the sky reads correctly "from inside the
 * globe" -- this is a reflection (call it Mx, negating the local X
 * component) applied to the direction vector *before* qEquat rotates it.
 * A reflection composed with a rotation is not undone by flipping the
 * sign of that rotation's angle (Mx . Ry(th) = Ry(-th) . Mx algebraically,
 * and verified numerically against the real three.js build across a dozen
 * (ra, dec, lat, lst) cases, including polar and near-horizon ones): no
 * choice of qSpin's sign can cancel Mx, it only relocates which rotation
 * angle Mx ends up wrapping. The actual fix cancels Mx where it must be
 * cancelled: on the resulting *world* position, by negating world.x right
 * after `raDecLocalDir(...).applyQuaternion(qEquat)` (see updateStar()'s
 * star placement and layout()'s equinox-marker placement). This leaves
 * raDecLocalDir(), toHorizontal(), toEcliptic(), qTilt and qSpin exactly
 * as they were -- confirmed by direct comparison against toHorizontal()
 * for RA/Dec/lat/LST spanning due-north, due-south, LST 6h ahead and 6h
 * behind, and near-polar cases (see tests/test_celestial_viz_azimuth.py).
 *
 * One thing this does *not* fix, deliberately: LST is still applied by
 * spinning the equatorial frame rather than spinning the observer under a
 * fixed inertial frame. Physically, the equatorial grid should be fixed and
 * the *observer* should rotate under it. For a single-observer, single-star
 * snapshot with no independent fixed starfield to compare against, the two
 * are visually and numerically equivalent (it's a relative rotation), so
 * this is left as a harmless simplification rather than reworked into an
 * observer-centric animation loop.
 *
 * The Sun is fixed at RA=0/Dec=0 (the vernal equinox) regardless of the
 * obliquity slider. That is *not* a bug: at the equinox, Dec=0 and
 * ecliptic latitude beta=0 by definition, for any obliquity -- the
 * obliquity is what determines *where the equinoxes are*, not the Sun's
 * coordinates *at* an equinox. So no coupling is needed there.
 */
(function (global) {
  "use strict";

  var THREE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js";
  var threePromise = null;

  function loadThree() {
    if (global.THREE) return Promise.resolve(global.THREE);
    if (threePromise) return threePromise;
    threePromise = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = THREE_CDN;
      s.onload = function () {
        resolve(global.THREE);
      };
      s.onerror = function () {
        threePromise = null;
        reject(new Error("Failed to load three.js from CDN"));
      };
      document.head.appendChild(s);
    });
    return threePromise;
  }

  var D2R = Math.PI / 180,
    R2D = 180 / Math.PI;

  var REFERENCE_STARS = {
    sirius: { name: "Sirius", ra: 101.287, dec: -16.716, note: "The brightest star in the night sky, in Canis Major." },
    betelgeuse: { name: "Betelgeuse", ra: 88.793, dec: 7.407, note: "A red supergiant marking Orion's shoulder." },
    vega: {
      name: "Vega",
      ra: 279.235,
      dec: 38.784,
      note: "Comes closest to the north celestial pole around 13,700 CE, due to precession \u2014 it passes near the pole rather than sitting exactly on it.",
    },
    sun: {
      name: "The Sun",
      ra: 0.0,
      dec: 0.0,
      note: "Shown at the vernal equinox: Dec = 0 and ecliptic \u03bb = 0 there by definition, for any obliquity.",
    },
  };

  /* ---- astronomy math (unchanged from the reviewed prototype -- these
     conversions were independently checked and are correct) ---- */
  function toEcliptic(raDeg, decDeg, epsDeg) {
    var a = raDeg * D2R,
      d = decDeg * D2R,
      e = epsDeg * D2R;
    var sinB = Math.sin(d) * Math.cos(e) - Math.cos(d) * Math.sin(e) * Math.sin(a);
    var beta = Math.asin(Math.max(-1, Math.min(1, sinB)));
    var y = Math.sin(a) * Math.cos(e) + Math.tan(d) * Math.sin(e);
    var x = Math.cos(a);
    var lambda = Math.atan2(y, x);
    if (lambda < 0) lambda += 2 * Math.PI;
    return { lambda: lambda * R2D, beta: beta * R2D };
  }

  function toHorizontal(raDeg, decDeg, latDeg, lstHours) {
    var lstDeg = lstHours * 15;
    var ha = lstDeg - raDeg;
    ha = ((ha + 540) % 360) - 180;
    var H = ha * D2R,
      d = decDeg * D2R,
      phi = latDeg * D2R;
    var sinAlt = Math.sin(d) * Math.sin(phi) + Math.cos(d) * Math.cos(phi) * Math.cos(H);
    var alt = Math.asin(Math.max(-1, Math.min(1, sinAlt)));
    var cosAz = (Math.sin(d) - Math.sin(phi) * Math.sin(alt)) / (Math.cos(phi) * Math.cos(alt) || 1e-9);
    var sinAz = (-Math.sin(H) * Math.cos(d)) / (Math.cos(alt) || 1e-9);
    var az = Math.atan2(sinAz, cosAz) * R2D;
    if (az < 0) az += 360;
    return { az: az, alt: alt * R2D };
  }

  function degToDMS(deg, isLat) {
    var sign = deg < 0 ? -1 : 1,
      ad = Math.abs(deg);
    var totalSeconds = Math.round(ad * 3600);
    var d = Math.floor(totalSeconds / 3600),
      m = Math.floor((totalSeconds % 3600) / 60),
      s = totalSeconds % 60;
    var signChar = isLat ? (sign < 0 ? "-" : "+") : sign < 0 ? "-" : "";
    return signChar + d + "\u00b0" + String(m).padStart(2, "0") + "'" + String(s).padStart(2, "0") + '"';
  }
  function degToHMS(deg) {
    var totalSeconds = Math.round((deg / 15) * 3600);
    var hh = Math.floor(totalSeconds / 3600),
      mm = Math.floor((totalSeconds % 3600) / 60),
      ss = totalSeconds % 60;
    return String(hh).padStart(2, "0") + "h" + String(mm).padStart(2, "0") + "m" + String(ss).padStart(2, "0") + "s";
  }

  // Direction of (ra, dec) in the equatorial group's own LOCAL frame, before
  // qEquat is applied. RA=0/Dec=0 -> local +Z. This is the single source of
  // truth for "where RA=0 lives locally" -- the equinox marker below reuses
  // it instead of hardcoding a separate, possibly-inconsistent vector.
  function raDecLocalDir(THREE, raDeg, decDeg) {
    var a = raDeg * D2R,
      d = decDeg * D2R;
    return new THREE.Vector3(Math.cos(d) * Math.sin(-a), Math.sin(d), Math.cos(d) * Math.cos(-a));
  }

  var STYLE_ID = "celestial-viz-style";
  var CSS = "\n"
    + ".cviz{--ink-900:#070b14;--ink-800:#0d1424;--ink-700:#141d33;--ink-600:#1c2740;--paper:#e9edf5;--dim:#7c88a6;"
    + "--horiz:#5fd48a;--equat:#5b9dff;--eclip:#e8c04c;--star:#fff6de;"
    + "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--paper);background:var(--ink-900);"
    + "display:flex;flex-direction:column;height:100%;}\n"
    + ".cviz *{box-sizing:border-box;}\n"
    + ".cviz__head{padding:14px 16px 10px;border-bottom:1px solid var(--ink-600);}\n"
    + ".cviz__title{font-size:15px;font-weight:600;margin:0 0 8px;}\n"
    + ".cviz__select{width:100%;background:var(--ink-700);color:var(--paper);border:1px solid var(--ink-600);"
    + "padding:6px 8px;border-radius:4px;font-family:ui-monospace,Menlo,monospace;font-size:12px;margin-bottom:8px;}\n"
    + ".cviz__refcard{display:flex;font-family:ui-monospace,Menlo,monospace;font-size:11px;border:1px solid var(--ink-600);"
    + "border-radius:4px;overflow:hidden;}\n"
    + ".cviz__refcell{flex:1;padding:5px 8px;border-right:1px solid var(--ink-600);min-width:0;}\n"
    + ".cviz__refcell:last-child{border-right:none;}\n"
    + ".cviz__refcell span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}\n"
    + ".cviz__refcell .lbl{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);}\n"
    + ".cviz__viewer{position:relative;height:260px;flex:none;background:radial-gradient(ellipse at 50% 40%,#0f1a30 0%,var(--ink-900) 70%);}\n"
    + ".cviz__canvas-host{position:absolute;inset:0;}\n"
    + ".cviz__canvas-host canvas{display:block;width:100%;height:100%;cursor:grab;}\n"
    + ".cviz__canvas-host canvas:active{cursor:grabbing;}\n"
    + ".cviz__tabs{position:absolute;top:8px;left:8px;display:flex;gap:4px;z-index:3;background:rgba(7,11,20,.65);"
    + "backdrop-filter:blur(4px);padding:4px;border-radius:5px;border:1px solid var(--ink-600);}\n"
    + ".cviz__tabbtn{background:transparent;border:none;color:var(--dim);font-family:ui-monospace,Menlo,monospace;"
    + "font-size:9px;letter-spacing:.04em;text-transform:uppercase;padding:5px 7px;border-radius:3px;cursor:pointer;}\n"
    + ".cviz__tabbtn.active{color:var(--ink-900);font-weight:700;}\n"
    + ".cviz__tabbtn[data-sys=horizontal].active{background:var(--horiz);}\n"
    + ".cviz__tabbtn[data-sys=equatorial].active{background:var(--equat);}\n"
    + ".cviz__tabbtn[data-sys=ecliptic].active{background:var(--eclip);}\n"
    + ".cviz__tabbtn[data-sys=combined].active{background:var(--paper);}\n"
    + ".cviz__hint{position:absolute;bottom:6px;right:8px;z-index:3;font-family:ui-monospace,Menlo,monospace;"
    + "font-size:9px;color:var(--dim);}\n"
    + ".cviz__body{flex:1;overflow-y:auto;padding:14px 16px 20px;}\n"
    + ".cviz__eyebrow{font-family:ui-monospace,Menlo,monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;"
    + "color:var(--dim);margin-bottom:6px;}\n"
    + ".cviz__slider{margin-bottom:12px;}\n"
    + ".cviz__slider label{display:flex;justify-content:space-between;font-family:ui-monospace,Menlo,monospace;"
    + "font-size:10px;color:var(--dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em;}\n"
    + ".cviz__slider label .val{color:var(--paper);}\n"
    + ".cviz input[type=range]{width:100%;accent-color:#8aa0d4;}\n"
    + ".cviz table{width:100%;border-collapse:collapse;font-family:ui-monospace,Menlo,monospace;font-size:11px;margin-top:4px;}\n"
    + ".cviz table th{text-align:left;font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);"
    + "padding:3px 4px;border-bottom:1px solid var(--ink-600);}\n"
    + ".cviz table td{padding:5px 4px;border-bottom:1px solid var(--ink-700);}\n"
    + ".cviz__dot{width:8px;height:8px;border-radius:50%;display:inline-block;}\n"
    + ".cviz__note{font-size:10.5px;color:var(--dim);line-height:1.4;font-style:italic;margin-top:8px;}\n"
    + ".cviz__tip{position:absolute;pointer-events:none;z-index:8;background:var(--ink-800);border:1px solid var(--ink-600);"
    + "padding:6px 8px;border-radius:4px;font-size:11px;max-width:190px;line-height:1.35;display:none;"
    + "box-shadow:0 4px 14px rgba(0,0,0,.5);}\n"
    + ".cviz__tip b{display:block;font-family:ui-monospace,Menlo,monospace;font-size:9px;text-transform:uppercase;"
    + "letter-spacing:.06em;margin-bottom:2px;}\n";

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function makeLabelSprite(THREE, text, colorCss) {
    var c = document.createElement("canvas");
    c.width = 128;
    c.height = 64;
    var ctx = c.getContext("2d");
    ctx.font = "bold 40px monospace";
    ctx.fillStyle = colorCss;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, 64, 32);
    var tex = new THREE.CanvasTexture(c);
    var mat = new THREE.SpriteMaterial({ map: tex, transparent: true });
    var spr = new THREE.Sprite(mat);
    spr.scale.set(0.5, 0.25, 1);
    return spr;
  }

  function makeRing(THREE, radius, colorHex, opacity) {
    var curve = new THREE.EllipseCurve(0, 0, radius, radius, 0, Math.PI * 2, false, 0);
    var pts = curve.getPoints(128).map(function (p) {
      return new THREE.Vector3(p.x, 0, p.y);
    });
    var geo = new THREE.BufferGeometry().setFromPoints(pts);
    var mat = new THREE.LineBasicMaterial({ color: colorHex, transparent: true, opacity: opacity });
    return new THREE.LineLoop(geo, mat);
  }

  function makePoleAxis(THREE, colorHex) {
    var geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, -2.3, 0), new THREE.Vector3(0, 2.3, 0)]);
    var line = new THREE.Line(geo, new THREE.LineDashedMaterial({ color: colorHex, dashSize: 0.08, gapSize: 0.06 }));
    line.computeLineDistances(); // required for LineDashedMaterial to actually dash
    return line;
  }

  function buildViewer(container, THREE, opts) {
    ensureStyle();

    var currentObjectStar = null;
    if (typeof opts.ra === "number" && typeof opts.dec === "number" && !isNaN(opts.ra) && !isNaN(opts.dec)) {
      currentObjectStar = { name: opts.name || "This object", ra: opts.ra, dec: opts.dec, note: opts.note || "" };
    }

    var STARS = Object.assign({}, REFERENCE_STARS);
    var defaultKey = "sirius";
    if (currentObjectStar) {
      STARS = Object.assign({ _object: currentObjectStar }, STARS);
      defaultKey = "_object";
    }

    container.innerHTML =
      '<div class="cviz">' +
      '<div class="cviz__head">' +
      '<h4 class="cviz__title">Coordinate visualisation</h4>' +
      '<select class="cviz__select" data-role="star-select"></select>' +
      '<div class="cviz__refcard">' +
      '<div class="cviz__refcell"><span class="lbl">Equatorial</span><span data-role="rc-eq">\u2014</span></div>' +
      '<div class="cviz__refcell"><span class="lbl">Horizontal</span><span data-role="rc-hz">\u2014</span></div>' +
      '<div class="cviz__refcell"><span class="lbl">Ecliptic</span><span data-role="rc-ec">\u2014</span></div>' +
      "</div>" +
      "</div>" +
      '<div class="cviz__viewer">' +
      '<div class="cviz__canvas-host" data-role="canvas-host"></div>' +
      '<div class="cviz__tabs">' +
      '<button class="cviz__tabbtn" data-sys="horizontal">Horiz</button>' +
      '<button class="cviz__tabbtn" data-sys="equatorial">Equat</button>' +
      '<button class="cviz__tabbtn" data-sys="ecliptic">Eclip</button>' +
      '<button class="cviz__tabbtn" data-sys="combined">All</button>' +
      "</div>" +
      '<div class="cviz__hint">drag \u00b7 scroll</div>' +
      '<div class="cviz__tip" data-role="tip"></div>' +
      "</div>" +
      '<div class="cviz__body">' +
      '<div class="cviz__eyebrow">Observer &amp; sky settings</div>' +
      '<div class="cviz__slider"><label>Latitude (\u03c6) <span class="val" data-role="lat-val">30\u00b0N</span></label>' +
      '<input type="range" data-role="lat" min="-90" max="90" value="30" step="1"></div>' +
      '<div class="cviz__slider"><label>Local sidereal time <span class="val" data-role="lst-val">00:00</span></label>' +
      '<input type="range" data-role="lst" min="0" max="24" value="0" step="0.1"></div>' +
      '<div class="cviz__slider"><label>Obliquity (\u03b5) <span class="val" data-role="obl-val">23.44\u00b0</span></label>' +
      '<input type="range" data-role="obl" min="0" max="40" value="23.44" step="0.1"></div>' +
      '<div class="cviz__eyebrow" style="margin-top:14px;">Coordinates</div>' +
      '<table><thead><tr><th></th><th>System</th><th>Coord 1</th><th>Coord 2</th></tr></thead>' +
      '<tbody data-role="coord-body"></tbody></table>' +
      '<p class="cviz__note" data-role="star-note"></p>' +
      "</div>" +
      "</div>";

    var root = container.querySelector(".cviz");
    var host = root.querySelector('[data-role="canvas-host"]');
    var selectEl = root.querySelector('[data-role="star-select"]');
    Object.keys(STARS).forEach(function (key) {
      var opt = document.createElement("option");
      opt.value = key;
      opt.textContent = (key === "_object" ? "\u2605 " : key === "sun" ? "\u2609 " : "\u2605 ") + STARS[key].name;
      selectEl.appendChild(opt);
    });
    selectEl.value = defaultKey;

    var state = { system: "equatorial", combined: false, star: defaultKey, lat: 30, lst: 0, obl: 23.44 };

    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
    var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    host.appendChild(renderer.domElement);

    var rootGroup = new THREE.Group();
    scene.add(rootGroup);

    // subtle starfield background
    (function () {
      var g = new THREE.BufferGeometry();
      var N = 400,
        pos = new Float32Array(N * 3);
      for (var i = 0; i < N; i++) {
        var r = 20 + Math.random() * 10;
        var th = Math.random() * Math.PI * 2,
          ph = Math.acos(2 * Math.random() - 1);
        pos[i * 3] = r * Math.sin(ph) * Math.cos(th);
        pos[i * 3 + 1] = r * Math.cos(ph);
        pos[i * 3 + 2] = r * Math.sin(ph) * Math.sin(th);
      }
      g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      scene.add(new THREE.Points(g, new THREE.PointsMaterial({ color: 0x445070, size: 0.05 })));
    })();

    rootGroup.add(new THREE.Mesh(new THREE.SphereGeometry(0.045, 16, 16), new THREE.MeshBasicMaterial({ color: 0xffffff })));
    rootGroup.add(
      new THREE.Mesh(
        new THREE.SphereGeometry(2, 32, 24),
        new THREE.MeshBasicMaterial({ color: 0x2a3350, wireframe: true, transparent: true, opacity: 0.18 })
      )
    );

    // Horizontal frame: fixed, horizon = XZ plane, +Y = zenith, North = -Z, East = +X.
    var horizGroup = new THREE.Group();
    horizGroup.add(makeRing(THREE, 2, 0x5fd48a, 0.9));
    var zenithMark = new THREE.Mesh(new THREE.ConeGeometry(0.055, 0.14, 12), new THREE.MeshBasicMaterial({ color: 0x5fd48a }));
    zenithMark.position.set(0, 2.15, 0);
    var nadirMark = zenithMark.clone();
    nadirMark.rotation.z = Math.PI;
    nadirMark.position.set(0, -2.15, 0);
    horizGroup.add(zenithMark, nadirMark);
    [
      ["N", 0, -2.2],
      ["S", 0, 2.2],
      ["E", 2.2, 0],
      ["W", -2.2, 0],
    ].forEach(function (d) {
      var s = makeLabelSprite(THREE, d[0], "#5fd48a");
      s.position.set(d[1], 0, d[2]);
      horizGroup.add(s);
    });
    rootGroup.add(horizGroup);

    // Equatorial frame: oriented entirely via qEquat (see file header).
    var equatGroup = new THREE.Group();
    equatGroup.add(makeRing(THREE, 1.9, 0x5b9dff, 0.9));
    equatGroup.add(makePoleAxis(THREE, 0x5b9dff));
    var npLabel = makeLabelSprite(THREE, "NCP", "#5b9dff");
    npLabel.position.set(0, 2.35, 0);
    var spLabel = makeLabelSprite(THREE, "SCP", "#5b9dff");
    spLabel.position.set(0, -2.35, 0);
    equatGroup.add(npLabel, spLabel);
    rootGroup.add(equatGroup);

    // Ecliptic frame: oriented via qEclip = qEquat * qObl(local Z).
    var eclipGroup = new THREE.Group();
    eclipGroup.add(makeRing(THREE, 1.8, 0xe8c04c, 0.9));
    eclipGroup.add(makePoleAxis(THREE, 0xe8c04c));
    rootGroup.add(eclipGroup);

    var equinoxMark = new THREE.Mesh(new THREE.SphereGeometry(0.04, 12, 12), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    var equinoxLabel = makeLabelSprite(THREE, "\u2648", "#ffffff");
    rootGroup.add(equinoxMark, equinoxLabel);

    var starMesh = new THREE.Mesh(new THREE.SphereGeometry(0.065, 16, 16), new THREE.MeshBasicMaterial({ color: 0xfff6de }));
    var starGlow = new THREE.Sprite(
      new THREE.SpriteMaterial({
        map: (function () {
          var c = document.createElement("canvas");
          c.width = 64;
          c.height = 64;
          var ctx = c.getContext("2d");
          var grd = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
          grd.addColorStop(0, "rgba(255,246,222,0.9)");
          grd.addColorStop(1, "rgba(255,246,222,0)");
          ctx.fillStyle = grd;
          ctx.fillRect(0, 0, 64, 64);
          return new THREE.CanvasTexture(c);
        })(),
        transparent: true,
        depthWrite: false,
      })
    );
    starGlow.scale.set(0.45, 0.45, 1);
    rootGroup.add(starMesh, starGlow);
    var starLabel = makeLabelSprite(THREE, "", "#fff6de");
    rootGroup.add(starLabel);
    var starLine = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
      new THREE.LineBasicMaterial({ color: 0xfff6de, transparent: true, opacity: 0.6 })
    );
    rootGroup.add(starLine);

    var X_AXIS = new THREE.Vector3(1, 0, 0);
    var Y_AXIS = new THREE.Vector3(0, 1, 0);
    var Z_AXIS = new THREE.Vector3(0, 0, 1);

    function layout() {
      var lat = state.lat,
        obl = state.obl,
        lst = state.lst;

      var qTilt = new THREE.Quaternion().setFromAxisAngle(X_AXIS, (lat - 90) * D2R);
      var qSpin = new THREE.Quaternion().setFromAxisAngle(Y_AXIS, lst * 15 * D2R);
      var qEquat = qTilt.clone().multiply(qSpin); // qSpin acts first (local), then qTilt
      equatGroup.quaternion.copy(qEquat);

      var qObl = new THREE.Quaternion().setFromAxisAngle(Z_AXIS, obl * D2R);
      var qEclip = qEquat.clone().multiply(qObl); // qObl acts first (about the line of nodes), then qEquat
      eclipGroup.quaternion.copy(qEclip);

      var eqDir = raDecLocalDir(THREE, 0, 0).multiplyScalar(1.9); // RA=0/Dec=0, same convention as star placement
      var v = eqDir.clone().applyQuaternion(qEquat);
      v.x = -v.x; // undo raDecLocalDir()'s RA mirror in world space (see header comment, "Azimuth mirror fix")
      equinoxMark.position.copy(v);
      equinoxLabel.position.copy(v.clone().multiplyScalar(1.18));

      updateStar();
    }

    function updateStar() {
      var s = STARS[state.star];
      var ecl = toEcliptic(s.ra, s.dec, state.obl);
      var hz = toHorizontal(s.ra, s.dec, state.lat, state.lst);

      var qEquat = equatGroup.quaternion;
      var dir = raDecLocalDir(THREE, s.ra, s.dec).multiplyScalar(1.95);
      var world = dir.clone().applyQuaternion(qEquat);
      world.x = -world.x; // undo raDecLocalDir()'s RA mirror in world space (see header comment, "Azimuth mirror fix")

      starMesh.position.copy(world);
      starGlow.position.copy(world);
      starLabel.position.copy(world.clone().multiplyScalar(1.15));
      starLabel.material.map.dispose();
      starLabel.material.map = makeLabelSprite(THREE, s.name, "#fff6de").material.map;
      starLine.geometry.setFromPoints([new THREE.Vector3(0, 0, 0), world]);

      root.querySelector('[data-role="rc-eq"]').textContent = degToHMS(s.ra) + " / " + degToDMS(s.dec, true);
      root.querySelector('[data-role="rc-hz"]').textContent = "Az " + hz.az.toFixed(1) + "\u00b0 / Alt " + hz.alt.toFixed(1) + "\u00b0";
      root.querySelector('[data-role="rc-ec"]').textContent = "\u03bb " + ecl.lambda.toFixed(1) + "\u00b0 / \u03b2 " + ecl.beta.toFixed(1) + "\u00b0";

      root.querySelector('[data-role="coord-body"]').innerHTML =
        '<tr><td><span class="cviz__dot" style="background:#5b9dff"></span></td><td>Equatorial</td><td>RA ' +
        degToHMS(s.ra) +
        "</td><td>Dec " +
        degToDMS(s.dec, true) +
        "</td></tr>" +
        '<tr><td><span class="cviz__dot" style="background:#5fd48a"></span></td><td>Horizontal</td><td>Az ' +
        hz.az.toFixed(1) +
        "\u00b0</td><td>Alt " +
        hz.alt.toFixed(1) +
        "\u00b0" +
        (hz.alt < 0 ? " (below horizon)" : "") +
        "</td></tr>" +
        '<tr><td><span class="cviz__dot" style="background:#e8c04c"></span></td><td>Ecliptic</td><td>\u03bb ' +
        ecl.lambda.toFixed(1) +
        "\u00b0</td><td>\u03b2 " +
        ecl.beta.toFixed(1) +
        "\u00b0</td></tr>";
      root.querySelector('[data-role="star-note"]').textContent = s.note || "";
    }

    function applyVisibility() {
      var sys = state.system;
      if (state.combined || sys === "combined") {
        horizGroup.visible = equatGroup.visible = eclipGroup.visible = true;
      } else {
        horizGroup.visible = sys === "horizontal";
        equatGroup.visible = sys === "equatorial";
        eclipGroup.visible = sys === "ecliptic";
      }
    }

    root.querySelectorAll(".cviz__tabbtn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var sys = btn.getAttribute("data-sys");
        state.combined = sys === "combined";
        if (!state.combined) state.system = sys;
        root.querySelectorAll(".cviz__tabbtn").forEach(function (b) {
          b.classList.remove("active");
        });
        btn.classList.add("active");
        applyVisibility();
      });
    });
    root.querySelector('.cviz__tabbtn[data-sys="equatorial"]').classList.add("active");

    selectEl.addEventListener("change", function (e) {
      state.star = e.target.value;
      updateStar();
    });

    var latSlider = root.querySelector('[data-role="lat"]');
    var lstSlider = root.querySelector('[data-role="lst"]');
    var oblSlider = root.querySelector('[data-role="obl"]');
    latSlider.addEventListener("input", function (e) {
      state.lat = +e.target.value;
      root.querySelector('[data-role="lat-val"]').textContent = Math.abs(state.lat) + "\u00b0" + (state.lat >= 0 ? "N" : "S");
      layout();
    });
    lstSlider.addEventListener("input", function (e) {
      state.lst = +e.target.value;
      var hh = Math.floor(state.lst),
        mm = Math.round((state.lst - hh) * 60);
      root.querySelector('[data-role="lst-val"]').textContent = String(hh).padStart(2, "0") + ":" + String(mm).padStart(2, "0");
      layout();
    });
    oblSlider.addEventListener("input", function (e) {
      state.obl = +e.target.value;
      root.querySelector('[data-role="obl-val"]').textContent = state.obl.toFixed(2) + "\u00b0";
      layout();
    });

    function resize() {
      var w = host.clientWidth,
        h = host.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h);
      renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
    var resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(host);

    var dragging = false,
      lastX = 0,
      lastY = 0,
      rotX = 0.35,
      rotY = -0.5,
      camDist = 5.6;
    function updateCamera() {
      camera.position.set(
        camDist * Math.cos(rotX) * Math.sin(rotY),
        camDist * Math.sin(rotX),
        camDist * Math.cos(rotX) * Math.cos(rotY)
      );
      camera.lookAt(0, 0, 0);
    }
    host.addEventListener("mousedown", function (e) {
      dragging = true;
      lastX = e.clientX;
      lastY = e.clientY;
    });
    window.addEventListener("mouseup", function () {
      dragging = false;
    });
    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var dx = e.clientX - lastX,
        dy = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      rotY += dx * 0.006;
      rotX += dy * 0.006;
      rotX = Math.max(-1.5, Math.min(1.5, rotX));
      updateCamera();
    });
    host.addEventListener(
      "wheel",
      function (e) {
        e.preventDefault();
        camDist += e.deltaY * 0.003;
        camDist = Math.max(2.6, Math.min(12, camDist));
        updateCamera();
      },
      { passive: false }
    );

    // tooltips
    var tipEl = root.querySelector('[data-role="tip"]');
    var raycaster = new THREE.Raycaster();
    raycaster.params.Line.threshold = 0.06;
    var mouse = new THREE.Vector2();
    var tooltipDefs = new Map();
    function registerTip(obj, title, body) {
      tooltipDefs.set(obj, { title: title, body: body });
    }
    registerTip(horizGroup.children[0], "Horizon", "The reference plane of the horizontal system for this observer.");
    registerTip(equatGroup.children[0], "Celestial equator", "Earth's equator projected onto the sky.");
    registerTip(eclipGroup.children[0], "Ecliptic", "The plane of Earth's orbit, projected onto the sky.");
    registerTip(starMesh, "Selected object", "Drag to orbit and see this object from other angles.");

    host.addEventListener("mousemove", function (e) {
      var rect = host.getBoundingClientRect();
      mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(mouse, camera);
      var targets = Array.from(tooltipDefs.keys()).filter(function (o) {
        return o.visible !== false && o.parent && o.parent.visible !== false;
      });
      var hits = raycaster.intersectObjects(targets, false);
      if (hits.length) {
        var def = tooltipDefs.get(hits[0].object);
        tipEl.style.display = "block";
        tipEl.style.left = e.clientX - rect.left + 12 + "px";
        tipEl.style.top = e.clientY - rect.top + 12 + "px";
        tipEl.innerHTML = "<b>" + def.title + "</b>" + def.body;
      } else {
        tipEl.style.display = "none";
      }
    });
    host.addEventListener("mouseleave", function () {
      tipEl.style.display = "none";
    });

    var running = true;
    function animate() {
      if (!running) return;
      requestAnimationFrame(animate);
      renderer.render(scene, camera);
    }

    resize();
    updateCamera();
    applyVisibility();
    layout();
    animate();
    // resize once more after layout settles (panel slide-in transition,
    // web fonts, etc. can all change host.clientWidth/Height after mount)
    setTimeout(resize, 300);

    return {
      destroy: function () {
        running = false;
        resizeObserver.disconnect();
        renderer.dispose();
      },
    };
  }

  global.CelestialViz = {
    mount: function (container, opts) {
      return loadThree().then(function (THREE) {
        return buildViewer(container, THREE, opts || {});
      });
    },
  };
})(window);
