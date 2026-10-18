"""The real-time 3D AI core: GLSL shaders drawn through OpenGL (moderngl) inside a QOpenGLWidget.

* `dynamics`  - the shared animation state machine (pure Python: eased parameters per Jarvis state)
* `quality`   - the three quality levels and the automatic governor
* `scene`     - geometry + shaders + bloom, independent of Qt (rendered into any moderngl framebuffer)
* `widget`    - `GlOrbWidget`, the drop-in replacement for the painter-based `OrbWidget`

If OpenGL or moderngl is unavailable, or a shader fails to compile, the widget falls back to the painter-drawn core.
"""
