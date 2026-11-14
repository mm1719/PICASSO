import io

from plankton_control.client import _read_response


def test_progress_events_precede_the_final_usb_response() -> None:
    observed: list[tuple[int, int]] = []
    response = _read_response(
        io.BytesIO(
            b'{"event":"progress","processed":1000,"total":2500}\n'
            b'{"event":"progress","processed":2000,"total":2500}\n'
            b'{"ok":true,"result":{"entry_count":2500}}\n'
        ),
        lambda processed, total: observed.append((processed, total)),
    )

    assert observed == [(1000, 2500), (2000, 2500)]
    assert response == {"ok": True, "result": {"entry_count": 2500}}


def test_scan_import_progress_validates_bytes_and_heartbeat() -> None:
    observed: list[tuple[object, ...]] = []
    response = _read_response(
        io.BytesIO(
            b'{"event":"scan_import_progress","stage":"scanning","processed":1,"total":2,"processed_bytes":4,"total_bytes":8,"heartbeat":false}\n'
            b'{"event":"scan_import_progress","stage":"scanning","processed":1,"total":2,"processed_bytes":4,"total_bytes":8,"heartbeat":true}\n'
            b'{"ok":true,"result":{"entry_count":1}}\n'
        ),
        lambda *values: observed.append(values),
    )
    assert observed == [("scanning", 1, 2, 4, 8, False), ("scanning", 1, 2, 4, 8, True)]
    assert response == {"ok": True, "result": {"entry_count": 1}}
