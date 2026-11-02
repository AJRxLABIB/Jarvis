"""On-PC hardware validation: checks YOU run on the real machine, whose results are recorded honestly.

Everything the automated test suite cannot prove - Windows UI Automation, display scaling, several monitors, the microphone, the speakers,
the phone - is covered here by checks that run on the real hardware and say what they saw. A check that cannot run (not Windows, no
microphone, no paired phone) is recorded as PENDING with the reason; it is never reported as passed. Results are saved under
``data\\validation`` as JSON so the last validated state of each area can be shown (``python run.py --validate status``).

    python run.py --validate windows [--launch]   UI Automation, DPI / scaling, monitors, coordinates, focus (--launch opens Notepad)
    python run.py --validate voice                end-of-speech latency, VAD reaction, speaker-stop latency, Bangla accuracy by language
    python run.py --validate phone                paired phone: check-in age, version, real actions with latency, you confirm the effect
    python run.py --validate status               the last result of every area

The code of these checks is itself tested with fake hardware (tests/test_validation_*.py); those tests prove the checks make the right
decisions, NOT that your PC passes them.
"""
