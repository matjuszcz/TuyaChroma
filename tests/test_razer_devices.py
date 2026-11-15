from tuyachroma.razer_devices import categorize, group_devices

ENTRIES = [
    # USB keyboard: parent node with the product name + generic HID children
    (r"USB\VID_1532&PID_0287\00000000001A", "Razer BlackWidow V4"),
    (r"HID\VID_1532&PID_0287&MI_00\7&1", "HID Keyboard Device"),
    (r"HID\VID_1532&PID_0287&MI_01\7&2", "HID-compliant consumer control device"),
    # wireless mouse through its HyperSpeed dongle
    (r"USB\VID_1532&PID_00B7\5&1", "Razer DeathAdder V3 Pro HyperSpeed Dongle"),
    (r"HID\VID_1532&PID_00B7&MI_00\8&1", "HID-compliant mouse"),
    # headset, controller, unknown device without a name, Bluetooth headset
    (r"USB\VID_1532&PID_0527\6&1", "Razer Kraken V3"),
    (r"USB\VID_1532&PID_0A29\6&2", "Razer Wolverine V2 Chroma"),
    (r"USB\VID_1532&PID_0F99\6&3", "USB Composite Device"),
    (r"BTHLE\DEV_C0FFEE\7&3", "Razer Hammerhead True Wireless"),
    # not Razer
    (r"USB\VID_046D&PID_C52B\5&9", "Logitech USB Receiver"),
    (r"HID\VID_046D&PID_C52B\5&9", "HID Keyboard Device"),
]


def test_group_devices() -> None:
    devices = {d.name: d for d in group_devices(ENTRIES)}
    assert set(devices) == {
        "Razer BlackWidow V4", "Razer DeathAdder V3 Pro HyperSpeed Dongle", "Razer Kraken V3",
        "Razer Wolverine V2 Chroma", "Urządzenie Razer (PID 0F99)", "Razer Hammerhead True Wireless",
    }
    kb = devices["Razer BlackWidow V4"]
    assert (kb.category, kb.connection, kb.sdk_category, kb.note) == ("keyboard", "USB", "keyboard", "")
    mouse = devices["Razer DeathAdder V3 Pro HyperSpeed Dongle"]
    assert mouse.category == "mouse" and mouse.connection == "odbiornik bezprzew."
    assert devices["Razer Hammerhead True Wireless"].connection == "Bluetooth"
    assert devices["Razer Hammerhead True Wireless"].category == "headset"
    pad = devices["Razer Wolverine V2 Chroma"]
    assert pad.category == "controller" and "Controller Setup" in pad.note
    assert devices["Urządzenie Razer (PID 0F99)"].category == "other"
    # sorted by category: keyboard first
    assert group_devices(ENTRIES)[0].name == "Razer BlackWidow V4"


def test_categorize() -> None:
    cases = {
        "Razer Huntsman V3 Pro": "keyboard", "Razer Blade 16": "keyboard",
        "Razer Basilisk V3": "mouse", "Razer Naga V2 Pro": "mouse",
        "Razer Firefly V2 Pro": "mousepad", "Razer Base Station V2 Chroma": "mousepad",
        "Razer Mouse Dock Pro": "mousepad", "Razer BlackShark V2 Pro": "headset",
        "Razer Tartarus Pro": "keypad", "Razer Nommo V2 Pro": "chromalink",
        "Razer Leviathan V2": "chromalink", "Razer Seiren V3 Chroma": "chromalink",
        "Razer Chroma Addressable RGB Controller": "chromalink", "Razer Kitsune": "controller",
        "Razer Something New": "other",
    }
    for name, expected in cases.items():
        assert categorize(name) == expected, name


def test_translated_generic_names_never_win() -> None:
    """From a real (Polish Windows) log: long generic HID names replaced the products."""
    from tuyachroma.razer_devices import keyboard_layout

    generic = "Razer Urządzenie sterujące użytkownika zgodne z HID"
    entries = [
        (r"USB\VID_1532&PID_0257\6&1", "Razer Huntsman Mini"),
        (r"HID\VID_1532&PID_0257&MI_01\7&1", "Urządzenie sterujące użytkownika zgodne z HID"),
        (r"HID\VID_1532&PID_0257&MI_02\7&2", generic),
        (r"HID\VID_1532&PID_0099&MI_00\7&3", "Mysz zgodna z HID"),
        (r"HID\VID_1532&PID_0099&MI_01\7&4", "Klawiatura HID"),
        (r"USB\VID_1532&PID_0F1D\6&2", "Złożone urządzenie USB"),
        (r"HID\VID_1532&PID_0257&MI_03\7&9", "Razer Control Device"),  # also seen (1.6.3 log)
        (r"HID\VID_1532&PID_0F1F&MI_00\7&5", "Urządzenie wejściowe USB"),
    ]
    devices = group_devices(entries)
    by_pid = {d.pid: d for d in devices}
    assert by_pid["0257"].name == "Razer Huntsman Mini" and by_pid["0257"].category == "keyboard"
    assert by_pid["0099"].name == "Razer Basilisk V3" and by_pid["0099"].category == "mouse"
    assert "Bungee" in by_pid["0F1D"].name
    assert by_pid["0F1F"].category == "chromalink"
    assert all("zgodne z HID" not in d.name for d in devices)
    assert keyboard_layout(devices) == "mini"


def test_generic_razer_name_uses_database() -> None:
    """1.6.3 log: Windows named the Huntsman Mini only "Razer Control Device"."""
    from tuyachroma.razer_devices import keyboard_layout

    entries = [
        (r"HID\VID_1532&PID_0099&MI_00\7&3", "Mysz zgodna z HID"),
        (r"USB\VID_1532&PID_0F1D\6&2", "Złożone urządzenie USB"),
        (r"HID\VID_1532&PID_0257&MI_01\7&1", "Razer Control Device"),
        (r"HID\VID_1532&PID_0F1F&MI_00\7&5", "Razer Control Device"),
    ]
    by_pid = {d.pid: d for d in group_devices(entries)}
    assert by_pid["0257"].name == "Razer Huntsman Mini" and by_pid["0257"].category == "keyboard"
    assert by_pid["0F1F"].category == "chromalink"
    assert keyboard_layout(list(by_pid.values())) == "mini"
