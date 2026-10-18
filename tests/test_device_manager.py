"""Device manager: computers, phones and local network devices in one list, with check / add / remove / revoke."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx

from app.core import local_home
from app.core.local_home import LocalDevice, LocalHome
from app.memory.database import Database


def test_device_manager_lists_and_controls(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QApplication

    from app.config.settings import Settings
    from app.core.devices import DeviceRegistry
    from app.remote.mobile import MobileRegistry
    from app.ui.device_manager import DeviceManagerDialog

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "x.db")
    db.initialize()
    DeviceRegistry(db).register_self()
    DeviceRegistry(db).add("desktop", "aa:bb:cc:dd:ee:ff", "192.168.1.20")
    with db.transaction() as conn:
        conn.execute("INSERT INTO mobile_devices (id, name, public_key, fingerprint, trust, paired_at) "
                     "VALUES ('phone-0001', 'Pixel', 'k', 'fp', 'trusted', 'x')")
    LocalHome(tmp_path).add(LocalDevice("desk light", "wled", "192.168.1.60", confirm=False))
    registry = MobileRegistry(db)
    controller = SimpleNamespace(ctx=SimpleNamespace(database=db, paths=SimpleNamespace(config_dir=tmp_path),
                                                     settings=Settings(data_dir=tmp_path)),
                                 mobile_registry=lambda: registry)
    monkeypatch.setattr(local_home, "TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(200, json={"on": True, "bri": 255})))
    dialog = DeviceManagerDialog(controller)
    sections = [s for s, _ in dialog.rows]
    assert sections.count("computer") == 2 and sections.count("phone") == 1 and sections.count("local") == 1
    dialog.table.selectRow(sections.index("local"))
    assert "desk light: on, brightness 100%" in dialog.check_selected()
    assert dialog.add_local(LocalDevice("plug", "shelly", "192.168.1.61"))
    dialog.table.selectRow([s for s, _ in dialog.rows].index("phone"))
    assert dialog.revoke_selected(confirm=False)
    assert [s for s, _ in dialog.rows].count("phone") == 0                     # revoked phones leave the list
    dialog.table.selectRow(0)                                                   # this PC can't be removed
    assert dialog.remove_selected(confirm=False) is False
    local_index = [s for s, _ in dialog.rows].index("local")
    dialog.table.selectRow(local_index)
    assert dialog.remove_selected(confirm=False)
