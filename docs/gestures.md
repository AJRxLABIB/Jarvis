# Hand-gesture control (round 56)

Opt-in, local, off by default. Webcam -> 21 hand landmarks -> gestures -> safety controller -> mouse. Frames are never saved or sent; CAMERA ACTIVE stays on while the camera is open.

## Turn it on
1. `pip install -r requirements-gesture.txt` (optional; mediapipe 1.x has one Windows wheel that works on Python 3.14, confirmed by `pip download` for 3.12-3.14; it pulls opencv-contrib-python) and `python run.py --setup-hand-model` (explicit one-time download of `hand_landmarker.task`, ~7.5 MB; nothing is downloaded silently, detection never uses the network).
2. Settings > Privacy > **Gesture control** (`GESTURE_CONTROL=true`) - only permission; the camera still opens only when you enable it.
3. "Enable gesture control" (tool `gesture_control`, asks first). `practice` shows gestures without clicking. `diagnostic` says what is missing.

## Gestures
| Gesture | Action |
|---|---|
| INDEX_POINT | cursor follows the fingertip (mirrored, smoothed, dead zone, speed cap, clamped to the monitor) |
| PINCH | click on release (edge-triggered: held pinch = one click; start < 0.28 hand-sizes, release > 0.42) |
| PINCH held > 0.45 s | drag, only if `drag` is on; releases on pinch release, hand lost, disable, camera loss, emergency stop, shutdown or an input error |
| TWO_FINGER_SCROLL | index + middle up, move hand up/down to scroll (capped per frame) |
| OPEN_PALM held ~1 s | pause (resume by tool/Resume) |
| FIST | optional drag mode (`fist_drag`, off) |
Double click is optional (`double_click`). Right click: thumb + middle finger pinch, off until `right_click` is turned on. Mappings: `map` binds a gesture to a registered tool (never a shell command, cooldown >= 0.5 s, confirmation on by default, runs through the normal ToolExecutor).

## Safety
States DISABLED / INITIALIZING / READY / ACTIVE / PAUSED / TRACKING_LOST / ERROR / SHUTTING_DOWN; only ACTIVE sends input; any failure fails closed and releases buttons *we* pressed. Emergency stop (Ctrl+Shift+F12, Menu, voice "emergency stop") disables gestures without involving recognition. Jarvis lock blocks enabling.

## Layout
`app/gesture/`: `camera.py` (shared ref-counted broker; presence detection now uses it too, one device, newest-frame slot, no backlog), `landmarks.py`, `engine.py`, `cursor.py` (calibration), `safety.py` (controller, fake/Windows input, mapping registry), `detector.py` (MediaPipe Tasks adapter + setup), `service.py` (worker, practice mode, measured stats). Tool: `app/tools/gesture_tool.py`.

## Two hands, zoom, swipe, dwell (round 56e)
Research notes (open-source air-pointer / virtual-mouse projects, [air_pointer changelog](https://pub.dev/packages/air_pointer/versions/0.1.0/changelog), [Leap Motion mouse devlog](https://stardance.hackclub.com/projects/36378/devlogs/25386)): separate press/release thresholds,
a longer pre-drag hold, a "Midas-touch" guard, swipe direction dominance + cooldown, and dwell-click with a still-radius were the recurring practical lessons; no source covered two-hand zoom, so that design is mine.
* **Two hands** (default `max_hands` 2): one hand drives the pointer and is kept by continuity, so a second hand entering the view cannot steal it. The preview draws the pointer hand green, the second amber.
* **Zoom**: pinch with BOTH hands and move them apart (zoom in) or together (zoom out) = Ctrl + mouse wheel (browsers, Office, maps, photos). Ctrl is always released. Clicks, drags and pointer moves are suppressed while zooming and for 0.5 s after. `zoom` setting.
* **Swipe**: a quick sideways sweep of an open palm (needs travel >= 1.6 hand sizes within 0.6 s, mostly sideways, 1 s cooldown; a still palm still pauses, a swipe never does). `swipe` setting: off (default) | browser (Alt+Left/Right) | desktops (Ctrl+Win+Left/Right) | slides (arrow keys). Direction is as YOU see it (mirrored).
* **Dwell click** (`dwell_click`, off): hold the fingertip still for ~1.2 s to click once; moving away re-arms it.
* **Midas-touch guard**: a hand that appears (or returns after a loss) already pinched cannot click or drag until it has opened once.
* Tutorial now also teaches the swipe and the two-hand zoom. Left/right-hand preference is NOT implemented (MediaPipe's handedness label is mirrored for un-mirrored video and I could not verify it here).
* Not verified on hardware: real two-hand detection speed (two hands cost more than one; check `--validate gesture`), swipe thresholds and zoom feel with your camera frame rate.

## Dashboard, calibration, tutorial (Gestures page in the command center)
Six live panels (camera, hand tracking, virtual mouse, mappings, performance, safety; only measured numbers, otherwise "not measured yet"), a skeleton preview (landmarks only - the camera
image is never drawn or kept), **Calibrate** (hold the fingertip at the screen's top-left, then bottom-right, 1 s each; saved to the cursor area), **Tutorial** (practice mode, advances only
when the real gesture is seen) and a red **STOP**. Voice/text: "enable / stop / pause / resume / practice gesture control", "gesture status", "what are the gestures".
The cursor now covers the whole monitor (taskbar reachable).

## Smoothness and ease of use (round 56c)
* Hand position goes through a One Euro filter (steady at rest, quick when moving), and the pointer glides toward it at 60 Hz, so motion stays smooth even when the camera only delivers 10-15 frames/s.
* Click accuracy: when a pinch starts the pointer snaps back to where you aimed ~0.18 s earlier and stays frozen until you release (fingers drift as they close).
* Presets, applied live without restarting: Hand movement small / medium / large (default medium = middle 60% of the camera view), Smoothness smoother / balanced / quicker. Calibrate saves a custom area and applies at once.
* Page: one status line, a plain-language hint for the current state, a live gesture badge, Start / Pause-Resume / Stop, options on one row, panels hidden under "Show details".
* Voice/tool: `settings` accepts `reach` and `smooth`.

## Validate on your PC
`python run.py --validate gesture` : Python/mediapipe wheel, model file, DPI awareness, the pointer landing where the mapper says on every monitor (moves the mouse, no click), taskbar
reach, webcam frames/s, and real detection (you hold a hand up: hand-seen rate, fps, detection median/p95 ms). "Real click lands on the right control" stays PENDING by design.

## Honest status
* Unit-tested (54 tests): synthetic landmarks, fake mouse/camera/detector, dashboard model, calibration wizard, tutorial, validation decisions with a fake environment.
* NOT run on real hardware by me: MediaPipe detection accuracy/speed, real mouse injection under scaling/multi-monitor, mediapipe wheel availability on your Python, and the Qt Gestures page
  (needs a display; only compile/wiring is tested). `--validate gesture` is the tool that settles the first three.
* Not built: Android remote gestures (design only: would reuse the signed PhoneLink queue).
