"""Hand-gesture control (opt-in, off by default): webcam -> 21 hand landmarks -> gestures -> a safety controller -> mouse.

Everything is local. Frames are never saved or sent anywhere. Pure logic (landmarks, engine, cursor, safety) is separate from hardware
(camera, detector, OS input), so it is unit-tested with synthetic landmarks and a fake input device."""
