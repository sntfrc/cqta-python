import numpy as np
import pytest

from cqta.core.afsk_modem import AFSKDemodulator, AFSKModulator, crc16, hdlc_bits, hdlc_decode
from cqta.core.afsk_port import AFSKPort, PortSettings
from cqta.core.ipv4 import build_udp

FS = 48000


def make_packet(n: int, seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    return build_udp(b"\x0a\x73\x01\x01", b"\x0a\x73\xff\xff", 1, 2, rng.bytes(n))


def modulate(frames, fs=FS, level=0.8):
    return AFSKModulator(fs).modulate(hdlc_bits(frames, 40, 8), level)


def demod(samples, fs=FS, chunk=1024):
    d = AFSKDemodulator(fs)
    got: list[bytes] = []
    d.on_frame = got.append
    lead = np.zeros(fs)  # lets the noise floor settle
    stream = np.concatenate((lead, samples, np.zeros(fs // 4))).astype(np.float32)
    for i in range(0, len(stream), chunk):
        d.process(stream[i:i + chunk])
    return got


def test_crc_known_value():
    assert crc16(b"123456789") == 0x906E


def test_hdlc_roundtrip_with_stuffing():
    payload = bytes([0xFF] * 40 + [0x7E, 0x00, 0x7E] * 5)
    bits = hdlc_bits([payload], 2, 1)
    body = bits[16:-8 - 8]
    assert hdlc_decode(body.tolist(), 20, 1600) == payload


@pytest.mark.parametrize("fs", [48000, 44100, 22050])
def test_clean_loopback(fs):
    frames = [make_packet(60, 1), make_packet(200, 2)]
    assert demod(modulate(frames, fs), fs) == frames


def test_noise_and_offsets():
    rng = np.random.default_rng(5)
    frame = make_packet(120, 3)
    s = modulate([frame])
    s = s * 0.05 + 0.02 + rng.normal(0, 0.01, len(s))
    assert demod(s) == [frame]


def test_clock_error():
    frame = make_packet(120, 4)
    s = modulate([frame])
    n = int(len(s) * 1.008)
    resampled = np.interp(np.linspace(0, len(s) - 1, n), np.arange(len(s)), s)
    assert demod(resampled) == [frame]


def test_no_false_frames_on_noise():
    rng = np.random.default_rng(9)
    assert demod(rng.normal(0, 0.1, FS * 10)) == []


def test_spectral_tilt():
    frame = make_packet(100, 6)
    s = modulate([frame])
    # crude pre-emphasis: boosts the 2200 Hz space tone relative to 1200 Hz mark
    tilted = np.concatenate(([0], np.diff(s) * 3)) + s * 0.1
    assert demod(tilted) == [frame]


def test_port_transmits_and_receives_between_two_ports():
    a = AFSKPort(FS, FS, PortSettings(csma=False))
    b = AFSKPort(FS, FS)
    heard = []
    b.on_frame = heard.append
    air: list[np.ndarray] = []
    a.on_transmit = air.append
    frame = make_packet(80, 7)
    a.send(frame)
    silence = np.zeros(1024, dtype=np.float32)
    for _ in range(60):
        a.receive(silence)
    assert air, "port never transmitted"
    stream = np.concatenate((np.zeros(FS), air[0], np.zeros(FS // 2))).astype(np.float32)
    for i in range(0, len(stream), 1024):
        b.receive(stream[i:i + 1024])
    assert heard == [frame]
    assert a.frames_sent == 1
