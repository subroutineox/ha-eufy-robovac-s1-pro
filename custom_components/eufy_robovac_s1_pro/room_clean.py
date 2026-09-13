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
    """Liest ein Varint. Wirft ValueError statt IndexError bei Abbruch."""
    r = s = 0
    while True:
        if i >= len(buf) or s > 63:
            raise ValueError("varint abgeschnitten")
        x = buf[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def _fields(buf: bytes):
    """Iteriert (feldnummer, wiretype, wert) auf einer Ebene.

    Bricht bei unvollstaendigen oder unbekannten Daten sauber ab, statt eine
    Ausnahme zu werfen - der Aufrufer bekommt dann eben weniger Felder.
    """
    i = 0
    while i < len(buf):
        try:
            key, i = _read_varint(buf, i)
        except ValueError:
            return
        f, w = key >> 3, key & 7
        if f == 0:
            return
        if w == 0:
            try:
                v, i = _read_varint(buf, i)
            except ValueError:
                return
            yield f, 0, v
        elif w == 2:
            try:
                ln, i = _read_varint(buf, i)
            except ValueError:
                return
            if ln < 0 or i + ln > len(buf):
                return
            yield f, 2, buf[i:i + ln]
            i += ln
        elif w == 1:
            if i + 8 > len(buf):
                return
            i += 8
        elif w == 5:
            if i + 4 > len(buf):
                return
            i += 4
        else:  # 3/4 sind veraltete Gruppen - hier nicht auswertbar
            return


def _wire(payload: bytes) -> str:
    return base64.b64encode(_varint(len(payload)) + payload).decode()


def _unwire(value: str) -> bytes:
    try:
        raw = base64.b64decode(value)
    except Exception:
        return b""
    if not raw:
        return b""
    try:
        n, i = _read_varint(raw, 0)
    except ValueError:
        return raw
    return raw[i:] if n == len(raw) - i else raw


# --- Nachrichten ------------------------------------------------------------

# Feldbelegung der Reinigungsparameter, durch Messung an der App belegt:
#   Feld 2 Saugstufe    fehlt = Leise, 1 = Standard, 2 = Turbo, 3 = Max
#   Feld 3 Wassermenge  fehlt = niedrig, 1 = mittel, 2 = hoch
#   Feld 4 Wischen      fehlt = nur saugen, 2 = wischen
#   Feld 6 Raumliste    {1: Raum-ID, 2: Reihenfolge}
#   Feld 17 Durchgaenge
# DPS 9 meldet gentle/normal/strong/max; die Klartextnamen aus DPS 158
# sind zusaetzlich aufgenommen, falls mal der andere Wert ankommt.
FAN = {
    "gentle": 0, "quiet": 0,
    "normal": 1, "standard": 1,
    "strong": 2, "turbo": 2,
    "max": 3, "maximum": 3,
}
WATER = {"low": 0, "middle": 1, "high": 2}


def build_timer(rooms: list[int], hour: int, minute: int,
                fan: int = 0, water: int = 0, mop: bool = False,
                repeats: int = 1, cycle: int = 0) -> bytes:
    """Baut eine TimerInfo nach dem Muster, das das Geraet selbst sendet.

    cycle = 0 erzeugt eine einmalige Aufgabe (Feld entfaellt), sonst eine
    Wochentagsmaske: Bit 0 = Sonntag ... Bit 6 = Samstag, 127 = taeglich.
    """
    params = _vf(1, 1)
    if fan:
        params += _lf(2, _vf(1, fan))
    if mop and water:
        params += _lf(3, _vf(1, water))
    if mop:
        params += _lf(4, _vf(1, 2))
    params += _lf(5, b"")
    for order, rid in enumerate(rooms):
        params += _lf(6, _vf(1, rid) + _vf(2, order))
    params += _vf(17, max(1, int(repeats)))

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


def params_from_dps(data: dict | None) -> dict:
    """Leitet Saugstufe, Wassermenge und Wischen aus dem Live-Zustand ab.

    So gilt eine Einstellung fuer manuelle wie geplante Reinigung:
    DPS 9 = Saugstufe, DPS 10 = Wassermenge, DPS 154 = wird gewischt.
    """
    data = data or {}
    fan = FAN.get(str(data.get("9", "")).lower(), 0)
    water = WATER.get(str(data.get("10", "")).lower(), 0)
    mop = _mopping(str(data.get("154", "")))
    return {"fan": fan, "water": water, "mop": bool(mop)}


def _mopping(dps154: str) -> bool | None:
    """Wischen aktiv? Feld 1.1 in DPS 154 gesetzt bedeutet ja."""
    body = _unwire(dps154)
    if not body:
        return None
    for f, w, d in _fields(body):
        if f == 1 and w == 2:
            for sf, sw, sv in _fields(d):
                if sf == 1:
                    return len(sv) > 0 if sw == 2 else bool(sv)
            return False
    return None


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
    try:
        body = _unwire(value)
    except Exception:
        return out
    for f, w, v in _fields(body):
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
