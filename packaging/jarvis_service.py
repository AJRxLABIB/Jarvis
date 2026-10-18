"""Windows service "JarvisBackground": keeps `run.py --background` running without anyone signed in.

Needs pywin32 (already in requirements.txt) and an ADMINISTRATOR PowerShell. Use packaging\\service.ps1, which asks for
the account and password; or by hand:

    python packaging\\jarvis_service.py --username .\\YourName --password ... --startup auto install
    python packaging\\jarvis_service.py start | stop | remove

IMPORTANT: run the service as YOUR account, not LocalSystem. Jarvis's settings, secrets (sealed with Windows DPAPI for
your account) and database live under your profile; LocalSystem could not open them. The account needs the "Log on as
a service" right. The service only supervises the same background host the logon task runs (app/automation/supervisor.py);
nothing here widens what Jarvis may do, and background mode refuses every step that needs an approval.

NOT run on a real Windows PC by its author: read it, run `service.ps1 -Status` first, and expect to debug the first install
(pywin32 services are sensitive to how Python and pywin32 were installed; `python Scripts\\pywin32_postinstall.py -install`).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import servicemanager  # noqa: E402
import win32event  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402

from app.automation.supervisor import Supervisor, spawn_child  # noqa: E402


class JarvisService(win32serviceutil.ServiceFramework):
    _svc_name_ = "JarvisBackground"
    _svc_display_name_ = "Jarvis background automations"
    _svc_description_ = "Runs Jarvis's scheduled automations without the window (same engine as run.py --background)."

    def __init__(self, args) -> None:
        super().__init__(args)
        self._stop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=45000)
        win32event.SetEvent(self._stop)

    def SvcDoRun(self) -> None:
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ""))

        def wait(seconds: float) -> bool:
            return win32event.WaitForSingleObject(self._stop, int(seconds * 1000)) == win32event.WAIT_OBJECT_0

        def log(message: str) -> None:
            servicemanager.LogInfoMsg(f"Jarvis: {message}")

        Supervisor(lambda: spawn_child(ROOT), wait, data_dir=ROOT / "data", log=log).run()
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STOPPED,
                              (self._svc_name_, ""))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(JarvisService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(JarvisService)
