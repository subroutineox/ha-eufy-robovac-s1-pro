"""Raumreinigung fuer den eufy S1 Pro ueber den Zeitplan-Kanal (DPS 164).

Der direkte Reinigungsbefehl (DPS 152, START_SELECT_ROOMS_CLEAN) wird von der
Firmware zwar angenommen, die enthaltene Raumliste aber nicht ausgewertet -
der Roboter faengt dann einfach beim ersten Raum der Karte an.

Zeitplaene wertet er dagegen korrekt aus. Dieses Modul legt deshalb eine
einmalige Aufgabe fuer "jetzt + delay" an. Der Roboter startet sie selbst.

Drahtformat: <Laenge als Varint><Protobuf>, base64-kodiert.
DPS 164 traegt TimerResponse (Geraet -> uns) und TimerRequest (uns -> Geraet).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

DPS_TIMER = "164"

ROOMS = {
    0: "Wohnzimmer",
    1: "Schlafzimmer",
    2: "Flur",
    3: "Badezimmer",
    4: "Kueche",
}

# TimerRequest.Method
M_ADD = 1
M_DELETE = 2
M_MOTIFY = 3
M_OPEN = 4
M_CLOSE = 5
M_INQUIRY = 6


# --- Protobuf-Primitive (bewusst ohne externe Abhaengigkeit) -----------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _vf(field: int, value: int) -> bytes:
    """Varint-Feld. Nullwerte werden wie in proto3 weggelassen."""
    if not value:
        return b""
    return _varint(field << 3) + _varint(value)


def _lf(field: int, payload: bytes) -> bytes:
    """Laengenbegrenztes Feld."""
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = buf[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def _fields(buf: bytes):
    """Iteriert (feldnummer, wiretype, wert) auf einer Ebene."""
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _read_varint(buf, i)
            yield f, 0, v
        elif w == 2:
            ln, i = _read_varint(buf, i)
            yield f, 2, buf[i:i + ln]
            i += ln
        else:  # 1 = 64bit, 5 = 32bit - kommt bei diesem Geraet nicht vor
            i += 8 if w == 1 else 4


def _wire(payload: bytes) -> str:
    return base64.b64encode(_varint(len(payload)) + payload).decode()


def _unwire(value: str) -> bytes:
    raw = base64.b64decode(value)
    n, i = _read_varint(raw, 0)
    return raw[i:] if n == len(raw) - i else raw


# --- Nachrichten ------------------------------------------------------------

def build_timer(rooms: list[int], hour: int, minute: int,
                fan: int = 1, mop: int = 1, clean_type: int = 1,
                extent: int = 2, cycle: int = 0) -> bytes:
    """Baut eine TimerInfo nach dem Muster, das das Geraet selbst sendet.

    cycle = 0 erzeugt eine einmalige Aufgabe (Feld entfaellt), sonst eine
    Wochentagsmaske: Bit 0 = Sonntag ... Bit 6 = Samstag, 127 = taeglich.
    """
    # ScheduleRoomsClean, wie in DPS 164 beobachtet: rooms liegen auf Feld 6
    params = (
        _vf(1, fan)
        + _lf(2, _vf(1, mop))
        + _lf(3, _vf(1, clean_type))
        + _lf(4, _vf(1, extent))
        + _lf(5, b"")
    )
    for order, rid in enumerate(rooms):
        params += _lf(6, _vf(1, rid) + _vf(2, order))
    params += _vf(17, 1)

    timing = _vf(2, 1) + _vf(3, hour) + _vf(4, minute)
    desc = _vf(1, 1) + _lf(2, timing)
    if cycle:
        desc += _lf(3, _vf(1, cycle))

    return (
        _lf(1, _vf(1, 1))                    # id
        + _lf(2, _vf(1, 1) + _vf(2, 1))      # status: valid, opened
        + _lf(3, desc)                       # desc
        + _lf(4, _vf(1, 1))                  # addition
        + _lf(5, _vf(1, 1) + _lf(4, _lf(2, params)))   # action
    )


def request(method: int, timer: bytes | None = None) -> str:
    payload = _vf(1, method)
    if timer is not None:
        payload += _lf(3, timer)
    return _wire(payload)


def clean_rooms_value(rooms: list[int], delay: int = 120,
                      now: datetime | None = None, **kw) -> tuple[str, datetime]:
    """Fertiger DPS-164-Wert plus der Zeitpunkt, zu dem er starten wird."""
    now = now or datetime.now()
    # Abrunden auf die volle Minute kann bis zu 59 s verschlucken - ohne
    # Mindestvorlauf laege die Startzeit sonst in der Vergangenheit.
    delay = max(int(delay), 60)
    start = (now + timedelta(seconds=delay)).replace(second=0, microsecond=0)
    if start <= now:
        start += timedelta(minutes=1)
    timer = build_timer(rooms, start.hour, start.minute, **kw)
    return request(M_ADD, timer), start


# --- Auswerten der Geraeteantwort -------------------------------------------

def parse_timers(value: str) -> list[dict]:
    """Liest TimerResponse (DPS 164) und liefert die Aufgaben als dicts."""
    out: list[dict] = []
    for f, w, v in _fields(_unwire(value)):
        if f != 4 or w != 2:
            continue
        info: dict = {"id": None, "hour": None, "minute": None,
                      "cycle": 0, "rooms": [], "opened": True}
        for tf, tw, tv in _fields(v):
            if tf == 1:
                for a, _, b in _fields(tv):
                    if a == 1:
                        info["id"] = b
            elif tf == 2:
                for a, _, b in _fields(tv):
                    if a == 2:
                        info["opened"] = bool(b)
            elif tf == 3:
                for a, aw, b in _fields(tv):
                    if a == 2 and aw == 2:
                        for c, _, d in _fields(b):
                            if c == 3:
                                info["hour"] = d
                            elif c == 4:
                                info["minute"] = d
                    elif a == 3 and aw == 2:
                        for c, _, d in _fields(b):
                            if c == 1:
                                info["cycle"] = d
            elif tf == 5:
                for a, aw, b in _fields(tv):
                    if a == 4 and aw == 2:
                        for c, cw, d in _fields(b):
                            if c == 2 and cw == 2:
                                for e, ew, g in _fields(d):
                                    if e == 6 and ew == 2:
                                        rid = 0
                                        for h, _, k in _fields(g):
                                            if h == 1:
                                                rid = k
                                        info["rooms"].append(rid)
        out.append(info)
    return out


def find_timer_id(value: str, hour: int, minute: int) -> int | None:
    """Sucht die Aufgaben-ID zu einer Uhrzeit - fuers Abbrechen."""
    for t in parse_timers(value):
        if t["hour"] is None or t["id"] is None or t["cycle"]:
            continue
        if t["hour"] == hour and t["minute"] == minute:
            return t["id"]
    return None


def delete_value(timer_id: int) -> str:
    return request(M_DELETE, _lf(1, _vf(1, timer_id)))
