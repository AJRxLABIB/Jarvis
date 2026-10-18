"""The 3D core scene: geometry, shader programs, bloom and the render sequence. Independent of Qt: it draws into any
moderngl framebuffer, so it can be tested off-screen and reused.

Layers (see the shaders for the motion): a dot-matrix globe (an even lattice of fine dots with a bright cyan rim),
twisted swirl sheets of dots turning inside it, a magenta-white heart, a thick glowing gold orbit and a magenta orbit
that cross the globe with light running along them, bright cyan rim arcs, fine orbit lines and a soft halo.
"""

from __future__ import annotations

import math

import numpy as np

from app.ui.core3d import shaders
from app.ui.core3d.dynamics import AMBER, CoreDynamics
from app.ui.core3d.quality import Quality

CAMERA_DISTANCE = 5.0
FOV_DEGREES = 38.0
PALETTE = ((0.29, 0.91, 1.0), (0.22, 0.60, 1.0), (0.95, 0.43, 1.0))     # cyan, electric blue, magenta (violet mixed in)
VIOLET = (0.65, 0.42, 1.0)

# radius, inclination, ascending node, angular speed, hue (2 = amber), eccentricity, dash segments, duty, ticks
RING_SPECS = (
    (1.35, 0.35, 0.2, 0.30, 0.00, 1.00, 0, 0.0, 72),
    (1.50, 1.15, 1.0, -0.22, 0.25, 1.00, 24, 0.62, 0),
    (1.20, 0.75, 2.4, 0.42, 0.75, 0.82, 0, 0.0, 0),
    (1.60, 1.40, 0.6, 0.15, 1.00, 1.00, 8, 0.80, 0),
    (1.10, 0.20, 4.0, -0.50, 0.00, 1.00, 40, 0.5, 120),
    (1.62, 0.90, 3.3, 0.10, 2.00, 0.90, 6, 0.35, 0),
    (0.95, 1.55, 1.7, 0.60, 0.30, 1.00, 0, 0.0, 0),
    (1.45, 0.50, 5.2, -0.35, 0.80, 0.90, 16, 0.70, 36),
    (1.25, 1.00, 2.9, 0.25, 0.90, 1.00, 0, 0.0, 0),
)


# ------------------------------------------------------------------ matrices
def perspective(fov_deg: float, aspect: float, near: float = 0.1, far: float = 50.0) -> np.ndarray:
    f = 1.0 / math.tan(math.radians(fov_deg) / 2.0)
    m = np.zeros((4, 4))
    m[0, 0], m[1, 1] = f / aspect, f
    m[2, 2], m[2, 3] = (far + near) / (near - far), 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


def rotation(axis: str, a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    if axis == "x":
        m[1, 1], m[1, 2], m[2, 1], m[2, 2] = c, -s, s, c
    elif axis == "y":
        m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    else:
        m[0, 0], m[0, 1], m[1, 0], m[1, 1] = c, -s, s, c
    return m


def rotate_about(axis, a: float) -> np.ndarray:
    """4x4 rotation by ``a`` radians about any axis (Rodrigues)."""
    k = np.asarray(axis, dtype=float)
    k = k / np.linalg.norm(k)
    x, y, z = k
    c, s, C = math.cos(a), math.sin(a), 1 - math.cos(a)
    m = np.eye(4)
    m[:3, :3] = [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                 [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                 [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]
    return m


def translation(x: float, y: float, z: float) -> np.ndarray:
    m = np.eye(4)
    m[0, 3], m[1, 3], m[2, 3] = x, y, z
    return m


def _bytes(m: np.ndarray) -> bytes:
    return np.asarray(m, dtype="f4").T.tobytes()      # GLSL is column-major


# ------------------------------------------------------------------ geometry (numpy, built once per quality level)
def _unit_vectors(rng: np.random.Generator, n: int) -> np.ndarray:
    v = rng.normal(size=(n, 3))
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def particle_data(rng: np.random.Generator, count: int, shell: bool) -> np.ndarray:
    """Interleaved (pos.xyzw, axis.xyz, seed.xyzw) float32 rows."""
    direction = _unit_vectors(rng, count)
    if shell:
        radius = np.where(rng.random(count) < 0.78, 1.0 + rng.normal(0, 0.025, count), rng.uniform(0.70, 0.95, count))
        hue = rng.uniform(0.0, 0.95, count)
        size = rng.uniform(1.1, 2.8, count)
        speed = rng.uniform(0.04, 0.22, count) * rng.choice([-1.0, 1.0], count)
    else:
        radius = 0.30 * rng.random(count) ** (1.0 / 1.6)                  # a compact, hot heart ...
        hue = np.clip(rng.normal(0.88, 0.12, count), 0.0, 1.0)             # ... magenta-violet like the reference
        size = rng.uniform(1.0, 2.6, count) * (1.3 - radius / 0.30 * 0.5)
        speed = rng.uniform(0.15, 0.9, count) * rng.choice([-1.0, 1.0], count)
    axis = _unit_vectors(rng, count)
    seed = np.stack([rng.uniform(0, 2 * math.pi, count), speed, size, hue], axis=1)
    return np.concatenate([direction, radius[:, None], axis, seed], axis=1).astype("f4")


def band_data(rng: np.random.Generator, bands: int, per_band: int) -> np.ndarray:
    """Glowing energy ribbons: points spread around tilted great circles; every point of a band turns about the band's
    own normal at the band's speed, so the whole ribbon flows around the sphere (same row layout as particle_data)."""
    hues = (0.0, 0.9, 0.55, 1.0, 0.25, 0.75, 0.1, 0.95)
    rows = []
    for k in range(bands):
        normal = _unit_vectors(rng, 1)[0]
        u = np.cross(normal, [0.0, 0.0, 1.0] if abs(normal[2]) < 0.9 else [1.0, 0.0, 0.0])
        u /= np.linalg.norm(u)
        n = per_band
        direction = np.repeat(u[None, :] + rng.normal(0, 0.035, (1, 3)), n, axis=0)
        direction += rng.normal(0, 0.03, (n, 3))                       # a little width
        direction /= np.linalg.norm(direction, axis=1, keepdims=True)
        radius = rng.uniform(0.98, 1.18) + rng.normal(0, 0.015, n)
        axis = np.repeat(normal[None, :], n, axis=0)
        speed = rng.uniform(0.25, 0.55) * (1 if k % 2 else -1)
        seed = np.stack([np.linspace(0, 2 * math.pi, n, endpoint=False) + rng.normal(0, 0.01, n),
                         np.full(n, speed), rng.uniform(2.0, 4.4, n), np.full(n, hues[k % len(hues)])], axis=1)
        rows.append(np.concatenate([direction, radius[:, None], axis, seed], axis=1))
    return np.concatenate(rows).astype("f4") if rows else np.zeros((0, 11), dtype="f4")


def globe_data(count: int) -> np.ndarray:
    """An even dot lattice on the globe (Fibonacci sphere): the fine 'dot-matrix' surface of the reference look.
    The whole lattice turns as one about the vertical axis."""
    i = np.arange(count) + 0.5
    y = 1.0 - 2.0 * i / count
    ring = np.sqrt(np.maximum(0.0, 1.0 - y * y))
    theta = math.pi * (3.0 - math.sqrt(5.0)) * i
    direction = np.stack([np.cos(theta) * ring, y, np.sin(theta) * ring], axis=1)
    rng = np.random.default_rng(7)
    radius = np.ones(count)
    axis = np.tile([0.0, 1.0, 0.0], (count, 1))
    seed = np.stack([rng.uniform(0, 2 * math.pi, count), np.full(count, 0.16), np.ones(count),
                     rng.uniform(0.0, 1.0, count)], axis=1)
    return np.concatenate([direction, radius[:, None], axis, seed], axis=1).astype("f4")


def _basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    u = np.cross(normal, [0.0, 0.0, 1.0] if abs(normal[2]) < 0.9 else [1.0, 0.0, 0.0])
    u /= np.linalg.norm(u)
    return u, np.cross(normal, u)


def swirl_data(rng: np.random.Generator, sheets: int, per_sheet: int) -> np.ndarray:
    """The big luminous 'petals' inside the globe (the reference's swirling shells): a few WIDE smooth bands around
    tilted circles, densely dotted inside with a bright line along both edges, each turning about its own axis."""
    hues = (0.15, 0.40, 0.05, 0.30, 0.55)
    count = max(2, min(sheets, 4))
    across = 19
    along = max(80, (per_sheet * max(sheets, 1) // count) // across)
    normals = [np.array(v, dtype=float) for v in ((0.35, 0.90, 0.25), (-0.80, 0.30, 0.52), (0.55, -0.35, 0.76),
                                                  (0.10, 0.45, -0.89), (-0.40, -0.85, 0.34))]
    rows = []
    for k in range(count):
        normal = normals[k] / np.linalg.norm(normals[k])
        u, v = _basis(normal)
        width = (0.34, 0.28, 0.38, 0.30, 0.30)[k]
        edge_n = along * 3                                  # the two bright edge lines: dense, evenly spaced
        fill_n = along * (across - 2)                       # the fill: scattered dots (no visible lanes / moire)
        theta = np.concatenate([np.tile(np.linspace(0, 2 * math.pi, edge_n, endpoint=False), 2),
                                rng.uniform(0, 2 * math.pi, fill_n)])
        lanes = np.concatenate([np.full(edge_n, -1.0), np.full(edge_n, 1.0),
                                rng.choice([-1.0, 1.0], fill_n) * np.clip(1.0 - rng.exponential(0.45, fill_n), 0.0, 0.97)])
        phi = width * lanes * (0.75 + 0.25 * np.cos(2.0 * theta + k))           # petal-shaped: wider, then narrower
        direction = (np.cos(phi)[:, None] * (np.cos(theta)[:, None] * u + np.sin(theta)[:, None] * v)
                     + np.sin(phi)[:, None] * normal)
        radius = (0.93, 0.86, 0.80, 0.90, 0.74)[k] + 0.02 * np.sin(3 * theta)
        strength = np.where(np.abs(lanes) > 0.99, 1.0, 0.25 + 0.55 * np.abs(lanes))   # 1.0 = edges; fill brightest beside them
        speed = (0.20 + 0.06 * k) * (1 if k % 2 else -1)
        seed = np.stack([theta, np.full(theta.size, speed), strength, np.full(theta.size, hues[k % len(hues)])], axis=1)
        axis = np.repeat(normal[None, :], theta.size, axis=0)
        rows.append(np.concatenate([direction, radius[:, None], axis, seed], axis=1))
    return np.concatenate(rows).astype("f4") if rows else np.zeros((0, 11), dtype="f4")


def spiral_data(points: int) -> np.ndarray:
    """The vortex round the heart: three spiral arms of dots in a tilted plane, turning (the 'eye' of the reference)."""
    normal = np.array([0.15, 0.30, 0.94])
    normal /= np.linalg.norm(normal)
    u, v = _basis(normal)
    arms, per = 3, max(60, points // 3)
    rows = []
    for arm in range(arms):
        r = np.linspace(0.05, 0.58, per)
        ang = arm * 2 * math.pi / arms + np.log(r / 0.05) * 2.2
        for shift, strength in ((0.0, 1.0), (0.10, 0.6), (-0.10, 0.6)):
            a = ang + shift
            direction = np.cos(a)[:, None] * u + np.sin(a)[:, None] * v
            seed = np.stack([a, np.full(per, 0.55), np.full(per, strength * (1.0 - r / 0.7)), np.full(per, 0.35)], axis=1)
            rows.append(np.concatenate([direction, r[:, None], np.repeat(normal[None, :], per, axis=0), seed], axis=1))
    return np.concatenate(rows).astype("f4")


# radius, tilt about x, turn about z, speed, hue (2 = gold, 1 = magenta, 0 = cyan), points, arc fraction (1 = whole)
ORBIT_SPECS = (
    (1.33, 1.22, -0.30, 0.10, 2.0, 2200, 1.0),     # the big bold gold orbit crossing the globe
    (1.33, 1.22, -0.30, 0.10, 2.0, 1400, 1.0),     # ... its white-hot inner line
    (1.16, 1.05, 0.55, -0.14, 1.0, 1600, 1.0),     # the bold magenta orbit
    (1.03, 0.20, 0.30, 0.20, 0.0, 700, 0.30),      # bright cyan arcs on the rim
    (1.03, 0.20, 3.40, 0.20, 0.0, 600, 0.22),
    (1.02, 0.35, 2.30, -0.16, 1.0, 600, 0.26),     # magenta glow arcs on the rim (upper left / lower right)
    (1.02, 0.35, 5.50, -0.16, 1.0, 500, 0.18),
)


def orbit_data(specs) -> np.ndarray:
    rows = []
    for index, (radius, tilt, turn, speed, hue, count, arc) in enumerate(specs):
        normal = np.array([0.0, -math.sin(tilt), math.cos(tilt)])
        cz, sz = math.cos(turn), math.sin(turn)
        normal = np.array([normal[0] * cz - normal[1] * sz, normal[0] * sz + normal[1] * cz, normal[2]])
        u, v = _basis(normal)
        theta = np.linspace(0, 2 * math.pi * arc, count, endpoint=arc >= 1.0)
        direction = np.cos(theta)[:, None] * u + np.sin(theta)[:, None] * v
        inner = index == 1
        strength = np.full(count, 0.55 if inner else 1.0)
        if arc < 1.0:
            strength = np.sin(np.linspace(0, math.pi, count)) ** 0.6       # arcs fade at both ends
        seed = np.stack([theta, np.full(count, speed), strength, np.full(count, hue)], axis=1)
        r = np.full(count, radius * (0.998 if inner else 1.0))
        rows.append(np.concatenate([direction, r[:, None], np.repeat(normal[None, :], count, axis=0), seed], axis=1))
        if arc >= 1.0 and not inner:                       # two star sparkles that travel around this orbit
            angles = np.array([0.6, 0.6 + math.pi * 0.9])
            spark_dir = np.cos(angles)[:, None] * u + np.sin(angles)[:, None] * v
            spark_seed = np.stack([angles, np.full(2, 0.55 if speed >= 0 else -0.55), np.full(2, 2.0), np.full(2, hue)], axis=1)
            rows.append(np.concatenate([spark_dir, np.full((2, 1), radius), np.repeat(normal[None, :], 2, axis=0),
                                        spark_seed], axis=1))
    return np.concatenate(rows).astype("f4")


def filament_data(rng: np.random.Generator, count: int, segments: int) -> tuple[np.ndarray, np.ndarray]:
    t = np.linspace(0.0, 1.0, segments + 1)
    start = _unit_vectors(rng, count)
    end = start + rng.normal(0, 0.9, (count, 3))
    end /= np.linalg.norm(end, axis=1, keepdims=True)
    a = np.concatenate([start, rng.uniform(0.28, 0.46, (count, 1))], axis=1)
    b = np.concatenate([end, rng.uniform(0.90, 1.04, (count, 1))], axis=1)
    s = np.stack([rng.uniform(0, 6.0, count), rng.uniform(0.5, 1.6, count), rng.uniform(0.12, 0.55, count),
                  rng.uniform(0.0, 1.0, count)], axis=1)
    per = np.concatenate([a, b, s], axis=1)                              # (count, 12)
    rows = np.concatenate([np.tile(t, count)[:, None], np.repeat(per, segments + 1, axis=0)], axis=1).astype("f4")
    base = np.arange(count)[:, None] * (segments + 1) + np.arange(segments)[None, :]
    index = np.stack([base, base + 1], axis=2).reshape(-1).astype("u4")
    return rows, index


def ring_data(rng: np.random.Generator, specs) -> tuple[np.ndarray, list[tuple[int, int]], np.ndarray, np.ndarray]:
    """(line vertices, [(first, count) per ring], tick vertices, marker vertices); rows are (a, ring4, cfg4, dash2)."""
    lines, slices, ticks, markers = [], [], [], []
    first = 0
    for radius, incl, node, speed, hue, ecc, dash, duty, tick_count in specs:
        ring = (radius, incl, node, speed)
        angles = np.linspace(0, 2 * math.pi, 181)[:-1]
        rows = np.zeros((angles.size, 11), dtype="f4")
        rows[:, 0] = angles
        rows[:, 1:5] = ring
        rows[:, 5:9] = (0.0, 0.0, hue, ecc)
        rows[:, 9:11] = (dash, duty)
        lines.append(rows)
        slices.append((first, angles.size))
        first += angles.size
        if tick_count:
            tick_angles = np.linspace(0, 2 * math.pi, tick_count, endpoint=False)
            for kind in (1.0, 2.0):
                t = np.zeros((tick_count, 11), dtype="f4")
                t[:, 0] = tick_angles
                t[:, 1:5] = (radius, incl, node, speed * 0.6)
                t[:, 5:9] = np.stack([np.full(tick_count, kind), np.zeros(tick_count), np.full(tick_count, hue),
                                      np.full(tick_count, ecc)], axis=1)
                t[:, 9:11] = (0.0, 0.0)
                ticks.append(t)
        for k in range(3):
            m = np.zeros((1, 11), dtype="f4")
            m[0, 0] = 0.0
            m[0, 1:5] = (radius, incl, node, speed * 1.7)
            m[0, 5:9] = (3.0, k * 2.0944 + rng.uniform(0, 1), hue, ecc)
            markers.append(m)
    # tick rows interleave start/end so GL_LINES pairs them: sort by angle index
    tick_rows = np.zeros((0, 11), dtype="f4")
    if ticks:
        pairs = []
        for i in range(0, len(ticks), 2):
            start, end = ticks[i], ticks[i + 1]
            pairs.append(np.stack([start, end], axis=1).reshape(-1, 11))
        tick_rows = np.concatenate(pairs)
    return (np.concatenate(lines), slices, tick_rows, np.concatenate(markers))


class CoreScene:
    def __init__(self, ctx, quality: Quality) -> None:
        import moderngl

        self.mgl = moderngl
        self.ctx = ctx
        self.quality = quality
        self._geometry: list = []
        self.programs = {
            "points": ctx.program(vertex_shader=shaders.POINTS_VS, fragment_shader=shaders.POINTS_FS),
            "orb": ctx.program(vertex_shader=shaders.ORB_VS, fragment_shader=shaders.ORB_FS),
            "fil": ctx.program(vertex_shader=shaders.FIL_VS, fragment_shader=shaders.FIL_FS),
            "ring": ctx.program(vertex_shader=shaders.RING_VS, fragment_shader=shaders.RING_FS),
            "glow": ctx.program(vertex_shader=shaders.FULLSCREEN_VS, fragment_shader=shaders.GLOW_FS),
            "bright": ctx.program(vertex_shader=shaders.FULLSCREEN_VS, fragment_shader=shaders.BRIGHT_FS),
            "blur": ctx.program(vertex_shader=shaders.FULLSCREEN_VS, fragment_shader=shaders.BLUR_FS),
            "combine": ctx.program(vertex_shader=shaders.FULLSCREEN_VS, fragment_shader=shaders.COMBINE_FS),
        }
        self._fullscreen = {name: ctx.vertex_array(self.programs[name], []) for name in ("glow", "bright", "blur", "combine")}
        self._targets: dict | None = None
        self._target_size = (0, 0)
        self.build(quality)

    # ---- geometry
    GEOMETRY_ATTRS = ("nucleus", "globe", "swirls", "gold_orbit", "magenta_orbit", "rim_arcs", "ring_lines", "ring_slices", "ring_ticks", "tick_count",
                      "ring_markers", "marker_count")

    def _release_geometry(self) -> None:
        for objects, _attrs in getattr(self, "_levels", {}).values():
            for obj in objects:
                try:
                    obj.release()
                except Exception:
                    pass
        self._levels = {}
        self._geometry = []

    def build(self, quality: Quality) -> None:
        """Make (or reuse) the geometry for this quality level. Each level is built once and kept on the GPU, so the
        automatic quality switching back and forth never stalls the animation with a rebuild."""
        if not hasattr(self, "_levels"):
            self._levels = {}
        if quality.samples != getattr(self.quality, "samples", None):
            self._release_targets()           # the multisample buffers depend on the level
        self.quality = quality
        cached = self._levels.get(quality.name)
        if cached is not None:
            self._geometry = cached[0]
            for name, value in cached[1].items():
                setattr(self, name, value)
            return
        ctx, rng, p = self.ctx, np.random.default_rng(20261017), self.programs
        keep = self._geometry = []

        def buffer(data) -> object:
            b = ctx.buffer(np.ascontiguousarray(data).tobytes())
            keep.append(b)
            return b

        def vao(prog, content, index=None):
            v = ctx.vertex_array(prog, content, index_buffer=index)
            keep.append(v)
            return v

        self.nucleus = vao(p["points"], [(buffer(particle_data(rng, quality.nucleus, False)), "4f 3f 4f", "in_pos", "in_axis", "in_seed")])
        layout11 = ("4f 3f 4f", "in_pos", "in_axis", "in_seed")
        self.globe = vao(p["orb"], [(buffer(globe_data(quality.shell)), *layout11)])
        swirl = np.concatenate([swirl_data(rng, quality.bands, quality.band_points * 8),
                                spiral_data(quality.band_points * 3)])
        self.swirls = vao(p["orb"], [(buffer(swirl), *layout11)])
        orbits = orbit_data(ORBIT_SPECS)
        arcs = sum(spec[5] for spec in ORBIT_SPECS if spec[6] < 1.0)              # (the arcs are listed last)
        gold = orbit_data(ORBIT_SPECS[:2])                                         # the bold gold ring (+ inner line)
        magenta = orbit_data(ORBIT_SPECS[2:3])                                     # the bold magenta ring
        self.gold_orbit = vao(p["orb"], [(buffer(gold), *layout11)])
        self.magenta_orbit = vao(p["orb"], [(buffer(magenta), *layout11)])
        self.rim_arcs = vao(p["orb"], [(buffer(orbits[-arcs:]), *layout11)])    # ... and the cyan rim arcs
        lines, self.ring_slices, ticks, markers = ring_data(rng, RING_SPECS[:quality.rings])
        layout = ("1f 4f 4f 2f", "in_a", "in_ring", "in_cfg", "in_dash")
        self.ring_lines = vao(p["ring"], [(buffer(lines), *layout)])
        self.ring_ticks = vao(p["ring"], [(buffer(ticks), *layout)]) if len(ticks) else None
        self.tick_count = len(ticks)
        self.ring_markers = vao(p["ring"], [(buffer(markers), *layout)])
        self.marker_count = len(markers)
        self._levels[quality.name] = (keep, {name: getattr(self, name) for name in self.GEOMETRY_ATTRS
                                             if hasattr(self, name)})

    # ---- uniforms
    @staticmethod
    def _set(prog, name: str, value) -> None:
        try:
            prog[name].value = value
        except KeyError:
            pass                              # the compiler dropped an unused uniform

    def _common(self, prog, dyn: CoreDynamics, view_model: np.ndarray, mvp: np.ndarray, pscale: float) -> None:
        s = self._set
        for name, value in (("u_t", dyn.phase), ("u_breath", dyn.breath), ("u_converge", dyn.converge), ("u_level", dyn.level),
                            ("u_pscale", pscale), ("u_bright", dyn.bright * dyn.glow_scale ** 0.5), ("u_cam", CAMERA_DISTANCE),
                            ("u_pulse", dyn.pulse), ("u_pulse_phase", dyn.pulse_phase)):
            s(prog, name, float(value))
        # the core's own palette: its state tint takes the place of the first colour
        s(prog, "u_c0", tuple(dyn.tint))
        s(prog, "u_c1", PALETTE[1])
        s(prog, "u_c2", PALETTE[2])
        s(prog, "u_amber", AMBER)
        for name, m in (("u_mvp", mvp), ("u_view", view_model)):
            try:
                prog[name].write(_bytes(m))
            except KeyError:
                pass

    # ---- render targets for bloom
    def _ensure_targets(self, size: tuple[int, int]) -> dict:
        if self._targets is not None and self._target_size == size:
            return self._targets
        self._release_targets()
        ctx = self.ctx
        w, h = size
        half = (max(2, w // 2), max(2, h // 2))

        def tex(sz):
            t = ctx.texture(sz, 4, dtype="f1")
            t.filter = (self.mgl.LINEAR, self.mgl.LINEAR)
            t.repeat_x = t.repeat_y = False
            return t

        scene, a, b = tex(size), tex(half), tex(half)
        self._targets = {"scene": scene, "a": a, "b": b, "half": half,
                         "scene_fbo": ctx.framebuffer([scene]), "a_fbo": ctx.framebuffer([a]), "b_fbo": ctx.framebuffer([b])}
        samples = min(self.quality.samples, getattr(ctx, "max_samples", 0) or 0)
        if samples > 1:                       # multisampled drawing, resolved into the scene texture: smooth edges
            try:
                ms = ctx.renderbuffer(size, 4, samples=samples)
                self._targets["ms"] = ms
                self._targets["ms_fbo"] = ctx.framebuffer([ms])
            except Exception:
                pass
        self._target_size = size
        return self._targets

    def _release_targets(self) -> None:
        if self._targets is not None:
            for key, obj in self._targets.items():
                if key != "half":
                    try:
                        obj.release()
                    except Exception:
                        pass
        self._targets = None

    # ---- the frame
    def render(self, target, size: tuple[int, int], dyn: CoreDynamics, yaw: float, pitch: float,
               sphere_pixel_scale: float = 1.0, viewport: tuple[int, int, int, int] | None = None,
               over: bool = False) -> None:
        """Draw one frame of pixel ``size`` into ``target`` (a moderngl framebuffer). With ``viewport`` the core goes
        into that rectangle of a bigger target; ``over`` blends it over what is already there instead of clearing."""
        ctx, mgl = self.ctx, self.mgl
        w, h = max(2, size[0]), max(2, size[1])
        projection = perspective(FOV_DEGREES, w / h)
        model = rotation("y", dyn.clock * 0.05 * dyn.speed_scale) @ rotation("x", 0.35)
        view = translation(0, 0, -CAMERA_DISTANCE) @ rotation("x", pitch) @ rotation("y", yaw)
        view_model = view @ model
        mvp = projection @ view_model
        out_size = (w, h)
        ss = max(1.0, float(getattr(self.quality, "supersample", 1.0)))
        if max(w, h) * ss > 4096:                       # stay within every GPU's texture limit
            ss = max(1.0, 4096 / max(w, h))
        w, h = int(w * ss), int(h * ss)                 # drawn larger, filtered down at the end: crisper at any size
        pscale = max(0.6, h / ss / 520.0) * sphere_pixel_scale * ss
        use_bloom = self.quality.bloom
        # always drawn off-screen first: the final pass makes the colours valid premultiplied alpha for Qt's compositor
        # (additive blending alone leaves colour brighter than alpha, which shows as coloured specks on screen)
        targets = self._ensure_targets((w, h))
        draw_into = targets.get("ms_fbo") or targets["scene_fbo"]
        draw_into.use()
        draw_into.viewport = (0, 0, w, h)
        draw_into.clear(0.0, 0.0, 0.0, 0.0)
        ctx.disable(mgl.DEPTH_TEST)
        ctx.enable(mgl.BLEND)
        ctx.blend_func = mgl.ONE, mgl.ONE
        ctx.enable(mgl.PROGRAM_POINT_SIZE)

        glow = self.programs["glow"]
        aspect = (w / h if w > h else 1.0, 1.0 if w > h else h / w)
        self._set(glow, "u_aspect", (aspect[0] * 1.0, aspect[1] * 1.0))
        for name, value in (("u_t", dyn.clock), ("u_bright", dyn.bright), ("u_scan", dyn.scan), ("u_sweep", dyn.sweep),
                            ("u_glow", dyn.glow_scale), ("u_level", dyn.level), ("u_ring", 0.74),
                            ("u_limb", 1.0 / (math.sqrt(CAMERA_DISTANCE ** 2 - 1.0) * math.tan(math.radians(FOV_DEGREES) / 2)))):
            self._set(glow, name, float(value))
        self._set(glow, "u_tint", tuple(dyn.tint))
        self._fullscreen["glow"].render(mgl.TRIANGLES, vertices=3)

        orb = self.programs["orb"]
        self._common(orb, dyn, view_model, mvp, pscale)
        orb["u_kind"].value = 0
        self.globe.render(mgl.POINTS)                # the dot-matrix globe
        orb["u_kind"].value = 1
        self.swirls.render(mgl.POINTS)               # swirling sheets inside it

        points = self.programs["points"]
        self._common(points, dyn, view_model, mvp, pscale)
        self._set(points, "u_shell", 0.0)
        self._set(points, "u_bright", float(dyn.bright * dyn.glow_scale ** 0.5 * 0.35))
        self.nucleus.render(mgl.POINTS)              # the hot heart

        # the two bold rings circle the core: each ring's plane turns steadily round its own tilted axis (gold one
        # way, magenta the other), so the rings sweep around the globe, passing in front of it and behind it
        orb["u_kind"].value = 2
        for vao_, axis, rate, phase in ((self.gold_orbit, (0.25, 1.0, 0.15), 0.42, 0.0),
                                        (self.magenta_orbit, (-0.35, 1.0, -0.20), -0.55, 1.9)):
            turn = rotate_about(axis, phase + dyn.clock * rate * dyn.speed_scale)
            orbit_vm = view @ turn
            self._common(orb, dyn, orbit_vm, projection @ orbit_vm, pscale)
            vao_.render(mgl.POINTS)
        self._common(orb, dyn, view_model, mvp, pscale)
        orb["u_kind"].value = 3
        self.rim_arcs.render(mgl.POINTS)

        ring = self.programs["ring"]
        self._common(ring, dyn, view_model, mvp, pscale)
        self._set(ring, "u_bright", float(dyn.bright * 0.12))          # fine orbit lines: a quiet accent now
        self._set(ring, "u_points", 0)
        for first, count in self.ring_slices:
            self.ring_lines.render(mgl.LINE_LOOP, vertices=count, first=first)
        self._set(ring, "u_points", 1)               # (the tick scales are the flat HUD's job now: no doubled ticks)
        self.ring_markers.render(mgl.POINTS, vertices=self.marker_count)
        self._set(ring, "u_points", 0)

        if "ms_fbo" in targets:
            ctx.copy_framebuffer(targets["scene_fbo"], targets["ms_fbo"])     # resolve the multisampled frame
        self._finish(target, (w, h), dyn, use_bloom, viewport, over, out_size=out_size)

    def _output(self, target, size, viewport, over) -> None:
        """Select the final target: the whole of it (cleared), or a rectangle blended over the backdrop."""
        mgl, ctx = self.mgl, self.ctx
        target.use()
        target.viewport = viewport or (0, 0, *size)
        if over:
            ctx.enable(mgl.BLEND)
            ctx.blend_func = mgl.ONE, mgl.ONE_MINUS_SRC_ALPHA        # premultiplied "over"
        else:
            ctx.disable(mgl.BLEND)
            target.clear(0.0, 0.0, 0.0, 0.0)

    def _finish(self, target, size: tuple[int, int], dyn: CoreDynamics, bloom: bool,
                viewport: tuple[int, int, int, int] | None = None, over: bool = False,
                out_size: tuple[int, int] | None = None) -> None:
        mgl, ctx, t = self.mgl, self.ctx, self._ensure_targets(size)
        half = t["half"]
        out = out_size or size
        # the down-filter: 4 taps half a texel apart when the frame was drawn larger than it is shown
        self._set(self.programs["combine"], "u_texel", (0.5 / size[0], 0.5 / size[1]) if out != size else (0.0, 0.0))
        ctx.disable(mgl.BLEND)
        if not bloom:
            self._output(target, out, viewport, over)
            t["scene"].use(0)
            t["a"].use(1)
            self._set(self.programs["combine"], "u_scene", 0)
            self._set(self.programs["combine"], "u_bloom", 1)
            self._set(self.programs["combine"], "u_strength", 0.0)
            self._fullscreen["combine"].render(mgl.TRIANGLES, vertices=3)
            return
        t["a_fbo"].use()
        t["a_fbo"].viewport = (0, 0, *half)
        t["scene"].use(0)
        self._set(self.programs["bright"], "u_tex", 0)
        self._set(self.programs["bright"], "u_threshold", 0.7)
        self._fullscreen["bright"].render(mgl.TRIANGLES, vertices=3)
        for _ in range(2):
            for src, dst, direction in (("a", "b_fbo", (1.0 / half[0], 0.0)), ("b", "a_fbo", (0.0, 1.0 / half[1]))):
                t[dst].use()
                t[dst].viewport = (0, 0, *half)
                t[src].use(0)
                self._set(self.programs["blur"], "u_tex", 0)
                self._set(self.programs["blur"], "u_dir", direction)
                self._fullscreen["blur"].render(mgl.TRIANGLES, vertices=3)
        self._output(target, out, viewport, over)
        t["scene"].use(0)
        t["a"].use(1)
        self._set(self.programs["combine"], "u_scene", 0)
        self._set(self.programs["combine"], "u_bloom", 1)
        self._set(self.programs["combine"], "u_strength", float(0.55 * dyn.glow_scale))
        self._fullscreen["combine"].render(mgl.TRIANGLES, vertices=3)

    def release(self) -> None:
        self._release_geometry()
        self._release_targets()
        for obj in (*self.programs.values(), *self._fullscreen.values()):
            try:
                obj.release()
            except Exception:
                pass
