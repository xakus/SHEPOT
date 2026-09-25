"""Общая логика трея, конфиг, нарезка аудио и текста, иконки."""

import numpy as np
import pytest

from shepot import config as config_mod
from shepot.audio import find_split
from shepot.keys import key_label
from shepot.reader import BLOCK_MAX, split_blocks
from shepot.ui_base import ICON_STATES, TrayBase


class DummyTray(TrayBase):
    """TrayBase без отрисовки: считает перестройки меню."""

    def __init__(self, cfg):
        super().__init__(cfg, has_reader=True)
        self.rebuilds, self.statuses = 0, []

    def rebuild_key_menus(self):
        self.rebuilds += 1

    def set_status(self, t):
        self.statuses.append(t)

    def icon(self, s):
        pass


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    """Конфиг — во временной папке, настоящий не трогаем."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(tmp_path / "config.json"))


def test_config_roundtrip():
    config_mod.save_config({"model": "tiny", "текст": "да"})
    assert config_mod.load_config() == {"model": "tiny", "текст": "да"}


def test_same_key_for_both_functions_is_refused():
    t = DummyTray({"hotkey": "KEY_RIGHTCTRL", "tts_key": "KEY_RIGHTSHIFT"})
    t.set_dictate_key("KEY_RIGHTSHIFT")
    assert t.dictate_key_name == "KEY_RIGHTCTRL" and "занята" in t.statuses[-1]
    t.set_read_key("KEY_PAUSE")
    assert t.read_key_name == "KEY_PAUSE"
    assert config_mod.load_config()["tts_key"] == "KEY_PAUSE"


def test_bad_device_in_config_falls_back_to_auto():
    assert DummyTray({"device": "tpu"}).device_pref == "auto"


def test_device_title():
    assert TrayBase.device_title("auto", "cpu") == "Устройство: Авто → CPU"
    assert TrayBase.device_title("cuda", "cpu") == "Устройство: GPU (CUDA) → CPU"
    assert TrayBase.device_title("cpu", "cpu") == "Устройство: CPU"


def test_key_label():
    assert key_label("KEY_RIGHTSHIFT") == "Правый Shift"
    assert key_label("KEY_F13") == "F13"


def test_find_split_hits_silence():
    sr = 16000
    audio = np.random.default_rng(0).normal(0, 0.3, sr * 30).astype(np.float32)
    audio[sr * 25: sr * 25 + sr // 2] = 0          # пауза на 25-й секунде
    cut = find_split(audio, sr, 20, 30)
    assert sr * 25 <= cut <= sr * 25 + sr // 2


def test_split_blocks_keeps_words():
    text = " ".join(f"Предложение номер {i}." for i in range(80))
    blocks = split_blocks(text)
    assert all(len(b) <= BLOCK_MAX for b in blocks)
    assert " ".join(blocks).split() == text.split()


def test_icons_draw_all_states():
    pytest.importorskip("PIL")
    from shepot.icons import draw_icon
    for s in ICON_STATES:
        img = draw_icon(s, 32)
        assert img.size == (32, 32)
