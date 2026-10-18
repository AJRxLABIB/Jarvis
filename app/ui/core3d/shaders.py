"""GLSL 3.30 shaders for the 3D core. All motion is computed here, on the GPU, from a few uniforms and per-vertex seeds.

Output is premultiplied colour with additive blending (blend func ONE, ONE), which Qt composites correctly over the
dark panel behind the widget.
"""

COMMON = """
#version 330 core
vec3 rot(vec3 p, vec3 k, float a) {
    float c = cos(a), s = sin(a);
    return p * c + cross(k, p) * s + k * dot(k, p) * (1.0 - c);
}
vec3 palette(float h, vec3 c0, vec3 c1, vec3 c2) {
    return h < 0.5 ? mix(c0, c1, h * 2.0) : mix(c1, c2, (h - 0.5) * 2.0);
}
"""

# ------------------------------------------------------------------ particles (nucleus and shell)
POINTS_VS = COMMON + """
in vec4 in_pos;      // xyz = unit direction, w = radius
in vec3 in_axis;     // the axis this particle orbits
in vec4 in_seed;     // x phase, y angular speed, z size, w hue
uniform mat4 u_mvp;
uniform mat4 u_view;
uniform float u_t, u_breath, u_converge, u_level, u_pscale, u_bright, u_cam, u_pulse, u_pulse_phase, u_shell;
uniform vec3 u_c0, u_c1, u_c2;
out vec3 v_col;
void main() {
    float r = in_pos.w;
    float a = in_seed.x + in_seed.y * u_t;
    vec3 p = rot(in_pos.xyz, normalize(in_axis), a) * r;
    p += 0.03 * vec3(sin(u_t * 0.9 + in_seed.x * 7.0), sin(u_t * 1.1 + in_seed.x * 5.0 + 1.0), sin(u_t * 0.7 + in_seed.x * 9.0 + 2.0));
    float breathe = 1.0 + u_breath * sin(u_t * 1.3 + r * 4.0 + in_seed.x) + 0.10 * u_level * (0.5 + in_seed.w);
    // an outward pulse travelling through the shell (speech / execution)
    float wave = exp(-pow((r - (0.4 + 0.9 * u_pulse_phase)) * 5.0, 2.0));
    breathe += 0.06 * u_pulse * wave * u_shell;
    p *= breathe * (1.0 - u_converge * 0.28 * min(u_shell, 1.0));
    vec4 vp = u_view * vec4(p, 1.0);
    gl_Position = u_mvp * vec4(p, 1.0);
    float z = clamp((-vp.z - u_cam) / 1.4 * 0.5 + 0.5, 0.0, 1.0);      // 0 = near, 1 = far
    float depth = mix(1.0, 0.30, z);
    float flash = pow(max(0.0, sin(u_t * 1.7 + in_seed.x * 41.0)), 60.0) * 1.8;
    vec3 base = palette(in_seed.w, u_c0, u_c1, u_c2);
    float band = u_shell > 1.5 ? 2.4 : 1.0;                               // energy ribbons glow hotter
    v_col = base * (0.80 + flash + 0.35 * wave * u_pulse) * u_bright * depth * mix(0.55, 1.0, min(u_shell, 1.0)) * band;
    gl_PointSize = max(1.6, in_seed.z * u_pscale * (u_cam / max(-vp.z, 0.5)) * (1.0 + 0.6 * flash));
}
"""
POINTS_FS = """
#version 330 core
in vec3 v_col;
out vec4 f;
void main() {
    vec2 d = gl_PointCoord * 2.0 - 1.0;
    float r2 = dot(d, d);
    if (r2 > 1.0) discard;
    float a = exp(-r2 * 3.2);
    f = vec4(v_col * a, a * 0.75);
}
"""

# ------------------------------------------------------------------ neural filaments (curved lines with travelling pulses)
FIL_VS = COMMON + """
in float in_t;       // 0..1 along the curve
in vec4 in_a;        // start: xyz direction, w radius
in vec4 in_b;        // end:   xyz direction, w radius
in vec4 in_s;        // x seed, y speed, z bulge, w hue
uniform mat4 u_mvp;
uniform mat4 u_view;
uniform float u_t, u_converge, u_bright, u_cam, u_level;
uniform vec3 u_c0, u_c1, u_c2;
out vec3 v_col;
void main() {
    vec3 p0 = in_a.xyz * in_a.w;
    vec3 p1 = in_b.xyz * in_b.w * (1.0 - 0.35 * u_converge);
    vec3 n = normalize(cross(p0, p1) + vec3(1e-3, 2e-3, 3e-3));
    float sway = sin(u_t * 0.6 * in_s.y + in_s.x * 13.0);
    vec3 ctrl = (p0 + p1) * 0.5 + n * in_s.z * (0.6 + 0.4 * sway)
              + normalize(p0 + p1 + vec3(1e-3)) * 0.18 * sin(u_t * 0.45 + in_s.x * 5.0);
    float t = in_t;
    vec3 p = (1.0 - t) * (1.0 - t) * p0 + 2.0 * (1.0 - t) * t * ctrl + t * t * p1;
    p = rot(p, normalize(vec3(sin(in_s.x * 3.0), 1.0, cos(in_s.x * 5.0))), u_t * 0.10 * in_s.y);
    vec4 vp = u_view * vec4(p, 1.0);
    gl_Position = u_mvp * vec4(p, 1.0);
    float z = clamp((-vp.z - u_cam) / 1.4 * 0.5 + 0.5, 0.0, 1.0);
    float head = fract(u_t * 0.20 * in_s.y + in_s.x);
    float pulse = exp(-pow((t - head) * 12.0, 2.0));
    float glow = 0.18 + 0.25 * u_level + 1.4 * pulse;
    v_col = palette(in_s.w, u_c0, u_c1, u_c2) * glow * u_bright * mix(1.0, 0.35, z);
}
"""
FIL_FS = """
#version 330 core
in vec3 v_col;
out vec4 f;
void main() { f = vec4(v_col, max(v_col.r, max(v_col.g, v_col.b)) * 0.8); }
"""

# ------------------------------------------------------------------ orbital rings, tick scales and orbit markers
RING_VS = COMMON + """
in float in_a;       // angle on the ring (radians)
in vec4 in_ring;     // radius, inclination, ascending node, angular speed
in vec4 in_cfg;      // kind (0 line, 1 tick start, 2 tick end, 3 marker), phase, hue, eccentricity
in vec2 in_dash;     // dash segments per turn (0 = solid), duty
uniform mat4 u_mvp;
uniform mat4 u_view;
uniform float u_t, u_bright, u_cam, u_pscale;
uniform vec3 u_c0, u_c1, u_c2, u_amber;
out vec3 v_col;
out float v_pat;
out float v_duty;
void main() {
    float spin = in_ring.w * u_t;
    float ang = in_a + in_cfg.y + spin;
    float r = in_ring.x + (in_cfg.x > 1.5 && in_cfg.x < 2.5 ? 0.035 : 0.0);
    vec3 local = vec3(cos(ang) * r, sin(ang) * r * in_cfg.w, 0.0);
    float node = in_ring.z + 0.05 * u_t * sign(in_ring.w);             // slow precession
    float ci = cos(in_ring.y), si = sin(in_ring.y), cn = cos(node), sn = sin(node);
    vec3 q = vec3(local.x, local.y * ci, local.y * si);                  // tilt about x
    vec3 p = vec3(q.x * cn - q.y * sn, q.x * sn + q.y * cn, q.z);        // turn about z
    vec4 vp = u_view * vec4(p, 1.0);
    gl_Position = u_mvp * vec4(p, 1.0);
    float z = clamp((-vp.z - u_cam) / 1.8 * 0.5 + 0.5, 0.0, 1.0);
    vec3 col = in_cfg.z > 1.5 ? u_amber : palette(in_cfg.z, u_c0, u_c1, u_c2);
    float k = in_cfg.x > 2.5 ? 2.0 : (in_cfg.x > 0.5 ? 1.1 : 1.15);
    v_col = col * k * u_bright * mix(1.0, 0.28, z);
    v_pat = (in_a + spin) / 6.2831853 * in_dash.x;
    v_duty = in_dash.y;
    gl_PointSize = in_cfg.x > 2.5 ? 7.0 * u_pscale * (u_cam / max(-vp.z, 0.5)) : 1.0;
}
"""
RING_FS = """
#version 330 core
in vec3 v_col;
in float v_pat;
in float v_duty;
uniform int u_points;
out vec4 f;
void main() {
    if (u_points == 1) {
        vec2 d = gl_PointCoord * 2.0 - 1.0;
        float r2 = dot(d, d);
        if (r2 > 1.0) discard;
        float a = exp(-r2 * 2.5);
        f = vec4(v_col * a, a);
        return;
    }
    if (v_duty > 0.0 && fract(v_pat) > v_duty) discard;
    f = vec4(v_col, max(v_col.r, max(v_col.g, v_col.b)) * 0.9);
}
"""

# ------------------------------------------------------------------ full-screen passes
FULLSCREEN_VS = """
#version 330 core
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);   // one big triangle
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
# the soft halo behind the core, the faint scan line and the scanning arc
GLOW_FS = """
#version 330 core
in vec2 v_uv;
uniform vec2 u_aspect;       // (w/h scale so the halo stays circular)
uniform vec3 u_tint;
uniform float u_t, u_bright, u_scan, u_sweep, u_glow, u_level, u_ring, u_limb;
out vec4 f;
void main() {
    vec2 c = (v_uv * 2.0 - 1.0) * u_aspect;
    float d = length(c);
    float halo = exp(-d * d * 3.2) * 0.55 + exp(-d * d * 14.0) * 0.70;
    float hot = exp(-d * d * 520.0) * (1.1 + 0.5 * u_level);               // the white-hot centre
    float heart = exp(-d * d * 40.0) * (0.75 + 0.35 * u_level) + exp(-d * d * 9.0) * 0.18;              // its magenta-violet glow
    vec2 sc = abs(c);                                                        // a soft 4-point star flare
    float flare = (exp(-sc.y * 160.0) * exp(-sc.x * 9.0) + exp(-sc.x * 160.0) * exp(-sc.y * 9.0)) * 0.55;
    float scan_y = (fract(u_t * 0.11) * 2.0 - 1.0) * 0.8;
    float scanline = smoothstep(0.035, 0.0, abs(c.y - scan_y)) * smoothstep(0.85, 0.2, d) * 0.35;
    float ang = atan(c.y, c.x);
    float behind = mod(u_sweep - ang, 6.2831853);
    float arc = pow(max(0.0, 1.0 - behind / 1.4), 3.0) * smoothstep(0.03, 0.0, abs(d - u_ring)) * 1.6;
    float fill = smoothstep(u_limb, 0.05, d) * 0.34;                          // the blue light filling the globe
    float limb = exp(-pow((d - u_limb) / 0.035, 2.0)) * 0.55 + exp(-pow((d - u_limb) / 0.11, 2.0)) * 0.22;
    float side = clamp(dot(normalize(c + vec2(1e-4)), normalize(vec2(-0.7, 0.7))), 0.0, 1.0);
    float side2 = clamp(dot(normalize(c + vec2(1e-4)), normalize(vec2(0.75, -0.6))), 0.0, 1.0);
    vec3 limb_col = mix(vec3(0.30, 0.90, 1.0), vec3(1.0, 0.30, 0.95), max(pow(side, 3.0), pow(side2, 4.0) * 0.8));
    float listen = smoothstep(0.02, 0.0, abs(d - (0.62 + 0.05 * sin(u_t * 3.0) + 0.1 * u_level))) * 0.4 * u_scan;
    float a = (halo * (0.20 + 0.22 * u_bright) * u_glow) + scanline * (0.4 + u_scan) + arc * u_scan + listen;
    vec3 rgb = u_tint * a * 0.7 + vec3(1.0, 0.97, 1.0) * (hot + flare) * u_bright
             + vec3(0.95, 0.25, 1.0) * heart * u_bright + vec3(0.14, 0.24, 1.0) * fill * u_bright
             + limb_col * limb * u_bright;
    f = vec4(rgb, min(1.0, a * 0.7 + hot + heart * 0.6 + flare + fill * 0.5 + limb * 0.6));
}
"""
BLUR_FS = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform vec2 u_dir;
uniform float u_threshold;
out vec4 f;
void main() {
    float w[5] = float[](0.2270270, 0.1945946, 0.1216216, 0.0540541, 0.0162162);
    vec4 sum = texture(u_tex, v_uv) * w[0];
    for (int i = 1; i < 5; i++) {
        sum += texture(u_tex, v_uv + u_dir * float(i)) * w[i];
        sum += texture(u_tex, v_uv - u_dir * float(i)) * w[i];
    }
    f = sum;
}
"""
BRIGHT_FS = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
uniform float u_threshold;
out vec4 f;
void main() {
    vec4 c = texture(u_tex, v_uv);
    float l = max(c.r, max(c.g, c.b));
    float k = smoothstep(u_threshold, u_threshold + 0.5, l);       // selective: only the bright parts glow
    f = vec4(c.rgb * k, c.a * k);
}
"""
COMBINE_FS = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_scene;
uniform sampler2D u_bloom;
uniform float u_strength;
uniform vec2 u_texel;        // > 0 when the scene was drawn larger than shown (supersampled): 4-tap down-filter
out vec4 f;
void main() {
    vec4 s = 0.25 * (texture(u_scene, v_uv + vec2(-u_texel.x, -u_texel.y)) + texture(u_scene, v_uv + vec2(u_texel.x, -u_texel.y))
                   + texture(u_scene, v_uv + vec2(-u_texel.x, u_texel.y)) + texture(u_scene, v_uv + vec2(u_texel.x, u_texel.y)));
    vec4 b = texture(u_bloom, v_uv) * u_strength;
    vec3 rgb = s.rgb + b.rgb;
    float peak = max(rgb.r, max(rgb.g, rgb.b));
    rgb = rgb / (1.0 + 0.30 * peak);                             // tone mapping on the brightest channel: keeps the hue
    rgb = min(rgb, vec3(1.0)) + vec3(max(0.0, max(rgb.r, max(rgb.g, rgb.b)) - 1.0)) * 0.35;   // only the hottest -> white
    f = vec4(rgb, clamp(max(s.a, max(rgb.r, max(rgb.g, rgb.b))), 0.0, 1.0));
}
"""

# ------------------------------------------------------------------ the reference look: dot-matrix globe, swirl sheets, orbits
# u_kind: 0 = globe (an even lattice of fine dots, bright rim), 1 = swirl sheet (twisted ribbons of dots inside the globe),
#         2 = orbit (a thick glowing ring of dense dots with light running along it), 3 = rim arc (bright cyan arcs)
ORB_VS = COMMON + """
in vec4 in_pos;      // xyz = unit direction, w = radius
in vec3 in_axis;     // the axis this point turns about (the same for a whole globe / sheet / ring: it moves as one)
in vec4 in_seed;     // x = angle along (orbit/arc/sheet), y = angular speed, z = strength (sheet: 1 at its bright edges), w = hue
uniform mat4 u_mvp;
uniform mat4 u_view;
uniform float u_t, u_level, u_pscale, u_bright, u_cam, u_pulse, u_pulse_phase, u_converge, u_breath;
uniform int u_kind;
uniform vec3 u_c0, u_c1, u_c2, u_amber;
out vec3 v_col;
out float v_sharp;
out float v_star;
const vec3 CYAN = vec3(0.30, 0.92, 1.0);
const vec3 BLUE = vec3(0.10, 0.36, 1.0);
const vec3 INDIGO = vec3(0.30, 0.20, 1.0);
const vec3 MAGENTA = vec3(1.0, 0.25, 0.95);
void main() {
    float a = in_seed.y * u_t;
    vec3 dir = rot(in_pos.xyz, normalize(in_axis), a);
    float r = in_pos.w * (1.0 + 0.012 * sin(u_t * 1.3) + 0.05 * u_level + 0.04 * u_pulse
                          * exp(-pow((in_pos.w - (0.3 + 1.0 * u_pulse_phase)) * 5.0, 2.0)));
    if (u_kind < 2) r *= 1.0 - 0.22 * u_converge;
    vec3 p = dir * r;
    vec4 vp = u_view * vec4(p, 1.0);
    gl_Position = u_mvp * vec4(p, 1.0);
    float z = clamp((-vp.z - u_cam) / 1.6 * 0.5 + 0.5, 0.0, 1.0);          // 0 = near, 1 = far
    vec3 n = normalize((u_view * vec4(dir, 0.0)).xyz);
    float rim = 1.0 - abs(n.z);                                            // 1 at the silhouette
    vec3 col; float size; float k;
    v_star = 0.0;
    vec3 tint = mix(CYAN, u_c0, 0.5);                                      // the state colour leans the cyan
    if (u_kind == 0) {                                                     // globe: vivid blue dots, cyan rim, magenta flare
        vec3 deep = mix(BLUE, INDIGO, in_seed.w * 0.8);
        col = mix(deep, tint, smoothstep(0.35, 1.0, rim));
        float side = smoothstep(0.2, 0.9, dot(normalize(n.xy + vec2(1e-4)), normalize(vec2(-0.75, 0.66))));
        col = mix(col, MAGENTA, side * pow(rim, 3.0) * 0.7);
        k = (0.95 + 1.6 * pow(rim, 3.0)) * mix(1.0, 0.62, z);
        float twinkle = pow(max(0.0, sin(u_t * 0.9 + in_seed.x * 37.0)), 90.0);
        k *= 1.0 + 1.8 * twinkle;
        size = 1.55 + 1.0 * pow(rim, 2.0) + 1.4 * twinkle;
        float flare = step(0.985, fract(in_seed.x * 13.7)) * step(0.7, rim) * smoothstep(0.4, 1.0, twinkle);
        if (flare > 0.0) { v_star = flare; size += 22.0 * flare; }
        v_sharp = 1.0;
    } else if (u_kind == 1) {                                              // swirl sheets: bright cyan edges, blue inside
        bool edge = in_seed.z > 0.95;
        col = edge ? mix(tint, vec3(0.85, 1.0, 1.0), 0.25) : mix(BLUE, palette(in_seed.w, u_c0, u_c1, u_c2), 0.35);
        float flow = 0.6 + 0.4 * sin(in_seed.x * 3.0 - u_t * 1.4 * sign(in_seed.y));
        k = (edge ? 1.5 : 1.9) * flow * mix(1.0, 0.55, z) * (1.0 + 0.6 * u_level);
        size = edge ? 2.2 : 1.8;
        v_sharp = 0.85;
    } else {                                                               // orbits and rim arcs
        bool gold = in_seed.w > 1.5;
        bool inner = in_seed.z < 0.6;                                      // the gold ring's white-hot inner line
        bool thin = in_seed.z > 0.6 && in_seed.z < 0.9;                     // finer orbits
        col = gold ? (inner ? vec3(1.0, 0.92, 0.62) : vec3(1.0, 0.66, 0.10))
                   : (in_seed.w > 0.9 ? MAGENTA : mix(tint, vec3(0.85, 1.0, 1.0), 0.3));
        float run = in_seed.x - u_t * (0.7 + 0.5 * abs(in_seed.y)) * (in_seed.y >= 0.0 ? 1.0 : -1.0);
        float head = pow(0.5 + 0.5 * cos(run), 18.0) + 0.7 * pow(0.5 + 0.5 * cos(run * 2.0 + 1.7), 40.0);
        float front = smoothstep(0.75, 0.2, z);                            // the half in front of the globe is brighter
        k = (u_kind == 3 ? 1.0 : 0.42 + 0.40 * front) * (0.8 + 1.3 * head) * (inner ? 1.45 : (thin ? 0.85 : 1.0));
        size = (u_kind == 3 ? (in_seed.w > 0.9 ? 4.5 : 2.3) : (inner ? 2.4 : (thin ? 2.6 : 6.6))) + 2.5 * head;
        if (in_seed.z > 1.5) {                                             // sparkles riding the orbits
            v_star = 0.75 + 0.25 * sin(u_t * 7.0 + in_seed.x * 5.0);
            size = 30.0;
            k = 1.2;
            col = gold ? vec3(1.0, 0.85, 0.45) : col;
        }
        v_sharp = inner ? 0.9 : 0.45;
    }
    v_col = col * k * u_bright;
    gl_PointSize = max(1.2, size * u_pscale * (u_cam / max(-vp.z, 0.5)));
}
"""
ORB_FS = """
#version 330 core
in vec3 v_col;
in float v_sharp;
in float v_star;
out vec4 f;
void main() {
    vec2 d = gl_PointCoord * 2.0 - 1.0;
    float r2 = dot(d, d);
    if (r2 > 1.0) discard;
    float a;
    if (v_star > 0.01) {                       // a four-point star sparkle with a hot centre
        vec2 q = abs(d);
        float spikes = exp(-q.y * 26.0) * (1.0 - q.x) + exp(-q.x * 26.0) * (1.0 - q.y);
        a = clamp(exp(-r2 * 30.0) * 1.6 + spikes * 0.9 + exp(-r2 * 4.0) * 0.25, 0.0, 2.0) * v_star;
        f = vec4(mix(v_col, vec3(1.0), 0.55) * a, min(1.0, a * 0.8));
        return;
    }
    // a crisp dot with a faint halo: sharp at HD sizes, still smooth (no hard pixel edge)
    float core = smoothstep(1.0, 0.15, sqrt(r2));
    a = mix(exp(-r2 * 3.0), core * core, v_sharp);
    f = vec4(v_col * a, a * 0.8);
}
"""
