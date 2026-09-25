"""Логика клавиш диктовки и чтения (Dictation.on_key) без микрофона и модели."""

import threading
import time

import pytest

from shepot.dictation import KEY_DOWN, KEY_REPEAT, KEY_UP, Dictation
from shepot.keys import OTHER


class FakeTray:
    """Минимальный трей: запоминает статусы и иконки."""

    def __init__(self):
        self.enabled, self.tts_enabled = True, True
        self.dictate_key_name, self.read_key_name = "KEY_RIGHTCTRL", "KEY_RIGHTSHIFT"
        self.device_pref = "auto"
        self.jobs = None
        self.statuses, self.icons = [], []

    def set_status(self, t):
        self.statuses.append(t)

    def icon(self, s):
        self.icons.append(s)

    def set_last(self, t):
        pass

    def call_soon(self, fn, *a):
        pass


class FakeRecorder:
    """Вместо микрофона: считает start/pause, хвост записи — пустой."""

    def __init__(self):
        self.started = self.paused = 0

    def start(self):
        self.started += 1
        return self.started

    def pause(self):
        self.paused += 1

    def take_chunk(self, gen):
        return None

    def drain(self, gen):
        import numpy as np
        return np.zeros(0, dtype=np.float32)


class FakeReader:
    """Чтение: считает toggle/stop."""

    def __init__(self):
        self.reading = False
        self.toggles = self.stops = 0
        self.done = threading.Event()

    def toggle(self):
        self.toggles += 1
        self.done.set()

    def stop(self):
        self.stops += 1


@pytest.fixture
def d():
    tray, reader = FakeTray(), FakeReader()
    dic = Dictation(tray, {}, reader, typer=lambda t: None)
    dic.rec = FakeRecorder()
    dic.state["ready"] = True
    return dic


def test_hold_starts_and_release_stops_recording(d):
    d.on_key("KEY_RIGHTCTRL", KEY_DOWN)
    assert d.pressed and d.rec.started == 1 and d.tray.icons[-1] == "rec"
    d.on_key("KEY_RIGHTCTRL", KEY_REPEAT)          # автоповтор не начинает новую запись
    assert d.rec.started == 1
    d.on_key("KEY_RIGHTCTRL", KEY_UP)
    assert not d.pressed and d.rec.paused == 1 and not d.session.recording


def test_not_ready_model_does_not_record(d):
    d.state["ready"] = False
    d.on_key("KEY_RIGHTCTRL", KEY_DOWN)
    assert not d.pressed and "не готова" in d.tray.statuses[-1]


def test_disabled_ignores_key(d):
    d.tray.enabled = False
    d.on_key("KEY_RIGHTCTRL", KEY_DOWN)
    assert not d.pressed


def test_other_keys_ignored(d):
    d.on_key(OTHER, KEY_DOWN)
    d.on_key("KEY_RIGHTALT", KEY_DOWN)
    assert d.rec.started == 0


def test_read_key_solo_press_toggles_reading(d):
    d.on_key("KEY_RIGHTSHIFT", KEY_DOWN)
    d.on_key("KEY_RIGHTSHIFT", KEY_UP)
    assert d.reader.done.wait(1) and d.reader.toggles == 1


def test_read_key_with_other_key_does_not_read(d):
    # Shift+буква — обычный набор текста, не команда чтения
    d.on_key("KEY_RIGHTSHIFT", KEY_DOWN)
    d.on_key(OTHER, KEY_DOWN)
    d.on_key(OTHER, KEY_UP)
    d.on_key("KEY_RIGHTSHIFT", KEY_UP)
    time.sleep(0.1)
    assert d.reader.toggles == 0


def test_read_disabled_does_not_read(d):
    d.tray.tts_enabled = False
    d.on_key("KEY_RIGHTSHIFT", KEY_DOWN)
    d.on_key("KEY_RIGHTSHIFT", KEY_UP)
    time.sleep(0.1)
    assert d.reader.toggles == 0


def test_dictation_stops_reading(d):
    d.reader.reading = True
    d.on_key("KEY_RIGHTCTRL", KEY_DOWN)
    time.sleep(0.1)
    assert d.reader.stops == 1 and d.pressed


def test_key_change_applies_immediately(d):
    d.tray.dictate_key_name = "KEY_PAUSE"
    d.on_key("KEY_RIGHTCTRL", KEY_DOWN)
    assert not d.pressed
    d.on_key("KEY_PAUSE", KEY_DOWN)
    assert d.pressed
