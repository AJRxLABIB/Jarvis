"""The window backdrop drawn by the GPU when the 3D core is the window's background: the hexagon backdrop image, a slow
scan beam, and the holographic energy streaks + audio waves that run from the core out to both window edges.

Doing this on the GPU (instead of repainting a wide strip of the window with QPainter every frame) is what keeps the
main thread free, so the animation no longer stutters or pauses while Jarvis is busy.
"""

from __future__ import annotations


import numpy as np

BLIT_VS = """
#version 330 core
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
BLIT_FS = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform float u_beam;        // 0..1 from the top: the slow scan beam
uniform vec3 u_tint;
out vec4 f;
void main() {
    vec2 uv = vec2(v_uv.x, 1.0 - v_uv.y);                 // the image is stored top row first
    vec3 c = texture(u_tex, uv).rgb;
    float d = abs(uv.y - u_beam);
    c += u_tint * (exp(-d * d * 900.0) * 0.06 + smoothstep(0.0012, 0.0, d) * 0.10);
    f = vec4(c, 1.0);
}
"""
LINE_VS = """
#version 330 core
in vec2 in_pos;      // pixels, origin top-left
in vec4 in_col;      // premultiplied colour
uniform vec2 u_size;
out vec4 v_col;
void main() {
    v_col = in_col;
    gl_Position = vec4(in_pos.x / u_size.x * 2.0 - 1.0, 1.0 - in_pos.y / u_size.y * 2.0, 0.0, 1.0);
}
"""
LINE_FS = """
#version 330 core
in vec4 v_col;
uniform vec3 u_mask;         // x, y (window pixels, GL origin), radius; radius 0 = draw everywhere
out vec4 f;
void main() {
    if (u_mask.z <= 0.0) { f = v_col; return; }
    // second pass, over the globe: only inside it, so the streams visibly start in the core's heart
    float d = distance(gl_FragCoord.xy, u_mask.xy) / u_mask.z;
    float k = smoothstep(1.0, 0.80, d) * smoothstep(0.04, 0.38, d) * 0.85;   // soft at the rim AND the very centre
    if (k <= 0.0) discard;
    f = v_col * k;
}
"""

VIOLET = (0.655, 0.545, 0.98)
GOLD = (0.96, 0.71, 0.26)


def ribbon(points: np.ndarray, width: float, rgba: tuple[float, float, float, float]) -> np.ndarray:
    """A polyline (n, 2) in pixels -> triangles (6 * (n-1), 6): x, y, r, g, b, a (premultiplied)."""
    if len(points) < 2:
        return np.zeros((0, 6), dtype="f4")
    d = np.gradient(points, axis=0)
    n = np.stack([-d[:, 1], d[:, 0]], axis=1)
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-6)
    left, right = points + n * width / 2, points - n * width / 2
    a0, a1, b0, b1 = left[:-1], left[1:], right[:-1], right[1:]
    tri = np.stack([a0, b0, a1, a1, b0, b1], axis=1).reshape(-1, 2)
    r, g, b, a = rgba
    col = np.tile(np.array([r * a, g * a, b * a, a], dtype="f4"), (len(tri), 1))
    return np.concatenate([tri, col], axis=1).astype("f4")


def ribbons(curves: np.ndarray, widths: np.ndarray, colors: np.ndarray) -> np.ndarray:
    """Many polylines at once: curves (m, n, 2) px; widths (m,) or (m, n) - a line may taper; colors (m, 4) or
    (m, n, 4) straight rgba - a line may change colour / fade along its length -> triangles (m*6*(n-1), 6)."""
    m, n = curves.shape[0], curves.shape[1]
    d = np.gradient(curves, axis=1)
    normal = np.stack([-d[..., 1], d[..., 0]], axis=-1)
    normal /= np.maximum(np.linalg.norm(normal, axis=-1, keepdims=True), 1e-6)
    widths = np.asarray(widths, dtype=float)
    half = (np.broadcast_to(widths[:, None], (m, n)) if widths.ndim == 1 else widths)[..., None] / 2
    left, right = curves + normal * half, curves - normal * half
    tri = np.stack([left[:, :-1], right[:, :-1], left[:, 1:], left[:, 1:], right[:, :-1], right[:, 1:]], axis=2)
    colors = np.asarray(colors, dtype=float)
    if colors.ndim == 2:
        colors = np.broadcast_to(colors[:, None, :], (m, n, 4))
    premult = np.concatenate([colors[..., :3] * colors[..., 3:4], colors[..., 3:4]], axis=-1)        # (m, n, 4)
    a0, a1 = premult[:, :-1], premult[:, 1:]
    col = np.stack([a0, a0, a1, a1, a0, a1], axis=2)                    # each vertex takes its end's colour
    segments = n - 1
    return np.concatenate([tri.reshape(m, segments * 6, 2), col.reshape(m, segments * 6, 4)], axis=2).reshape(-1, 6) \
        .astype("f4")


def _ribbon_band(top: np.ndarray, bottom: np.ndarray, colors: np.ndarray) -> np.ndarray:
    """A filled band between two polylines (n, 2) with per-point colours (n, 4) straight rgba (the wave's glow)."""
    premult = np.concatenate([colors[:, :3] * colors[:, 3:4], colors[:, 3:4]], axis=1)
    a, b, c, d = top[:-1], bottom[:-1], top[1:], bottom[1:]
    ca, cb = premult[:-1], premult[1:]
    tri = np.stack([a, b, c, c, b, d], axis=1).reshape(-1, 2)
    col = np.stack([ca, ca * 0.25, cb, cb, ca * 0.25, cb * 0.25], axis=1).reshape(-1, 4)   # bright at the wave, soft below
    return np.concatenate([tri, col], axis=1).astype("f4")


def holo_geometry(width: float, height: float, cx: float, cy: float, R: float, tint, wave: list[float], t: float,
                  samples: int = 96) -> np.ndarray:
    """The energy streams and the voice waves, as GPU triangles (pixel coordinates), built in one vectorised pass.

    Streams: a fan of curves born in the heart of the core, swaying gently. Each one tapers from a bright, thicker
    root to a hair-thin tip, shifts colour along its length, carries a pulse of light that runs outwards, and has
    comet-like beads (a bright head with a fading tail) travelling to the window edge.
    Waves: three layered voice waves per side (cyan, violet, deep blue) with a soft glowing fill under the main one;
    they swell with your voice, breathe gently when it's quiet, and taper to nothing at the window edge.
    """
    tones = np.array([tuple(tint), VIOLET, GOLD], dtype=float)
    s = np.linspace(0.0, 1.0, samples)
    S = s[None, :, None]
    parts: list[np.ndarray] = []
    curves, widths, colors = [], [], []
    n_lines = 12
    k = np.arange(n_lines)
    fan = (k - (n_lines - 1) / 2) / ((n_lines - 1) / 2)                             # -1 .. 1
    beads = []
    edge_fade = np.clip((1.0 - s) / 0.12, 0.0, 1.0)                                  # dissolve at the window edge
    for side in (-1, 1):
        far = cx if side < 0 else width - cx
        p0 = np.stack([np.full(n_lines, cx + side * R * 0.02), cy + fan * R * 0.015], axis=1)[:, None, :]
        c1 = np.stack([np.full(n_lines, cx + side * R * (0.85 + 0.15 * np.abs(fan))),
                       cy + fan * R * 0.22 + np.sin(t * 0.55 + k * 1.3 + side) * R * 0.14], axis=1)[:, None, :]
        p2 = np.stack([np.full(n_lines, cx + side * far),
                       cy - R * 0.30 + fan * R * 0.95 + np.sin(t * 0.37 + k * 0.8) * R * 0.08], axis=1)[:, None, :]
        curve = (1 - S) ** 2 * p0 + 2 * (1 - S) * S * c1 + S ** 2 * p2              # (n, samples, 2)
        # a gentle shimmer across the stream (it is never a dead-straight wire)
        curve[..., 1] += np.sin(s[None, :] * 9.0 - t * 1.6 + k[:, None] * 0.7) * R * 0.012 * s[None, :]
        base = tones[k % 3]                                                          # (n, 3)
        shifted = tones[(k + 1) % 3]
        mix = (s ** 1.4)[None, :, None]
        rgb = base[:, None, :] * (1 - mix * 0.55) + shifted[:, None, :] * (mix * 0.55)  # colour drifts along the line
        strength = 0.55 + 0.45 * np.cos(k * 2.1) ** 2                                 # some streams brighter
        head = (t * (0.11 + 0.025 * (k % 4)) + k * 0.31) % 1.25                       # a pulse running outwards
        pulse = np.exp(-((s[None, :] - head[:, None]) / 0.05) ** 2)
        along = (1.0 - 0.40 * s[None, :]) * edge_fade[None, :]                        # bright root, fading tip
        taper = 1.0 - 0.75 * s[None, :]
        for width_px, alpha in ((7.0, 0.06), (3.2, 0.14), (1.2, 0.72)):              # wide haze, glow, bright core line
            a = alpha * strength[:, None] * along * (1.0 + 2.2 * pulse)
            curves.append(curve)
            widths.append(width_px * taper * (1.0 + 0.6 * pulse))
            colors.append(np.concatenate([np.clip(rgb + 0.35 * pulse[..., None], 0, 1.0), np.clip(a, 0, 1)[..., None]],
                                         axis=-1))
        # comet beads: a bright head and a fading tail, flowing outwards at different speeds
        for j in range(3):
            u0 = (t * (0.045 + 0.012 * (k % 4)) + j / 3.0 + k * 0.137) % 1.0
            for tail in range(4):
                u = np.clip(u0 - tail * 0.012, 0.0, 1.0)
                q = ((1 - u) ** 2)[:, None] * p0[:, 0] + (2 * (1 - u) * u)[:, None] * c1[:, 0] + (u ** 2)[:, None] * p2[:, 0]
                life = np.sin(np.pi * u) ** 0.5 * (1.0 - tail * 0.24) * strength
                beads.append((q, base, life, 1.0 - tail * 0.2))
    parts.append(ribbons(np.concatenate(curves), np.concatenate(widths), np.concatenate(colors)))
    reach = min(cx, width - cx) - R * 0.02
    if reach > 20:
        history = np.asarray(wave or [0.0], dtype=float)
        level = history[len(history) - 1 - (s * (len(history) - 1)).astype(int)]
        level = np.maximum(level, 0.20 + 0.07 * np.sin(t * 1.3 + s * 4.0))          # a quiet breath when it's silent
        grow = np.clip(s * reach / (R * 0.55), 0.0, 1.0) ** 1.5                      # thin at the heart
        envelope = level * R * 0.60 * (1 - s * 0.45) * grow * np.clip((1.0 - s) / 0.15, 0.0, 1.0)
        wave_lines, wave_w, wave_c = [], [], []
        for side in (-1, 1):
            x = cx + side * (R * 0.02 + s * reach)
            px = s * reach / (R * 0.09)                       # wavelength tied to the core's size, not the window
            for layer, (tone, lw, freq, speed, amp) in enumerate(((tones[0], 1.8, 1.0, 5.0, 1.0),
                                                                  (tones[1], 1.3, 0.75, -3.6, 0.8),
                                                                  (np.array([0.25, 0.45, 1.0]), 1.0, 1.4, 6.5, 0.55))):
                phase = px * freq - t * speed + layer * 1.7
                y = cy + np.sin(phase) * (0.65 + 0.35 * np.cos(s * 3.0 + t * 0.8 + layer)) * envelope * amp
                line = np.stack([x, y], axis=1)
                fade = np.clip((1.0 - s) / 0.15, 0.0, 1.0) * (0.35 + 0.65 * grow)
                for width_px, alpha in ((lw * 4.0, 0.10), (lw, 0.85 - 0.22 * layer)):
                    wave_lines.append(line)
                    wave_w.append(np.full(samples, width_px))
                    c = np.zeros((samples, 4))
                    c[:, :3] = tone
                    c[:, 3] = alpha * fade
                    wave_c.append(c)
                if layer == 0:                                       # the soft glow filling under the main wave
                    fill = np.zeros((samples, 4))
                    fill[:, :3] = tone
                    fill[:, 3] = 0.16 * fade * np.clip(level * 2.0, 0.3, 1.0)
                    parts.append(_ribbon_band(line, np.stack([x, np.full(samples, cy)], axis=1), fill))
        parts.append(ribbons(np.stack(wave_lines), np.stack(wave_w), np.stack(wave_c)))
    if beads:
        parts.append(bead_quads(beads))
    return np.concatenate(parts).astype("f4")


def bead_quads(beads, sides: int = 10) -> np.ndarray:
    """Round glowing beads: a soft halo and a bright core per bead, each a small polygon (premultiplied colour)."""
    angles = np.linspace(0, 2 * np.pi, sides + 1)
    ring = np.stack([np.cos(angles), np.sin(angles)], axis=1)                       # (sides+1, 2)
    rows = []
    for item in beads:
        centres, tone, strength = item[0], item[1], item[2]
        scale = item[3] if len(item) > 3 else 1.0
        for radius, alpha, white in ((5.5, 0.14, 0.0), (1.9, 0.95, 0.5)):
            r = radius * scale
            a = np.clip(alpha * strength, 0.0, 1.0)[:, None]                          # (m, 1)
            centre_col = np.concatenate([(tone * (1 - white) + white) * a, a], axis=1)
            edge_col = centre_col * (0.0 if radius > 3 else 0.6)
            p0 = np.repeat(centres[:, None, :], sides, axis=1)                         # (m, sides, 2)
            p1 = centres[:, None, :] + ring[None, :-1] * r
            p2 = centres[:, None, :] + ring[None, 1:] * r
            tri = np.stack([p0, p1, p2], axis=2).reshape(-1, 2)
            col = np.stack([np.repeat(centre_col[:, None], sides, 1), np.repeat(edge_col[:, None], sides, 1),
                            np.repeat(edge_col[:, None], sides, 1)], axis=2).reshape(-1, 4)
            rows.append(np.concatenate([tri, col], axis=1))
    return np.concatenate(rows).astype("f4")


class Backdrop:
    def __init__(self, ctx) -> None:
        import moderngl

        self.mgl, self.ctx = moderngl, ctx
        self.blit = ctx.program(vertex_shader=BLIT_VS, fragment_shader=BLIT_FS)
        self.blit_vao = ctx.vertex_array(self.blit, [])
        self.line = ctx.program(vertex_shader=LINE_VS, fragment_shader=LINE_FS)
        self.texture = None
        self.texture_size = (0, 0)
        self._vbo = ctx.buffer(reserve=6 * 4 * 90000, dynamic=True)
        self._vao = ctx.vertex_array(self.line, [(self._vbo, "2f 4f", "in_pos", "in_col")])

    def set_image(self, rgba: bytes, size: tuple[int, int]) -> None:
        if self.texture is not None:
            self.texture.release()
        self.texture = self.ctx.texture(size, 4, rgba)
        self.texture.filter = (self.mgl.LINEAR, self.mgl.LINEAR)
        self.texture_size = size

    def draw_background(self, beam: float, tint) -> None:
        if self.texture is None:
            return
        self.ctx.disable(self.mgl.BLEND)
        self.texture.use(0)
        self.blit["u_tex"].value = 0
        self.blit["u_beam"].value = float(beam)
        self.blit["u_tint"].value = tuple(float(c) for c in tint)
        self.blit_vao.render(self.mgl.TRIANGLES, vertices=3)

    def draw_holo(self, size: tuple[int, int], geometry: np.ndarray) -> None:
        if not len(geometry):
            return
        data = geometry.tobytes()
        if len(data) > self._vbo.size:
            self._vbo.orphan(len(data))
        self._vbo.write(data)
        self.ctx.enable(self.mgl.BLEND)
        self.ctx.blend_func = self.mgl.ONE, self.mgl.ONE
        self.line["u_size"].value = (float(size[0]), float(size[1]))
        self.line["u_mask"].value = (0.0, 0.0, 0.0)
        self._vao.render(self.mgl.TRIANGLES, vertices=len(geometry))
        self._last_count = len(geometry)

    def draw_holo_inside(self, size: tuple[int, int], centre: tuple[float, float], radius: float) -> None:
        """The same streams again, over the globe and only inside it (after the core was drawn)."""
        count = getattr(self, "_last_count", 0)
        if not count or radius <= 1:
            return
        self.ctx.enable(self.mgl.BLEND)
        self.ctx.blend_func = self.mgl.ONE, self.mgl.ONE
        self.line["u_size"].value = (float(size[0]), float(size[1]))
        self.line["u_mask"].value = (float(centre[0]), float(size[1] - centre[1]), float(radius))
        self._vao.render(self.mgl.TRIANGLES, vertices=count)

    def release(self) -> None:
        for obj in (self.texture, self.blit_vao, self.blit, self._vao, self._vbo, self.line):
            try:
                if obj is not None:
                    obj.release()
            except Exception:
                pass
