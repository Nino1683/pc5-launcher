"""Trailer als Datenstrom für die Seite (E132).

Der Kern holt die Stücke eines Steam-Trailers (HLS-Fassung, fMP4) nacheinander aus dem Video-CDN und reicht sie als
EINE fortlaufende MP4-Datei an WebKit weiter, über einen kleinen HTTP-Dienst nur auf 127.0.0.1. Im Arbeitsspeicher
liegt dabei höchstens ein Stück je Spur (rund 1 MB) plus die Puffer der Verbindung; auf die Platte kommt nichts.

Warum so (gemessen 2026-10-27, WebKitGTK 2.54.1, GStreamer 1.28.8, docs/ARCHITECTURE.md E132):
  * Blob (E123): GStreamer liest einen Blob über queue2 im Ziehbetrieb. Ein voller Trailer (23 MB) legte den
    Web-Prozess binnen Sekunden still (kein Herzschlag) bzw. stürzte in gst_queue2_create_read (memcpy) ab, ebenso
    ein Blob, der zum zweiten Mal spielte. Das waren die Hänger am Fernseher.
  * Das eigene Schema couch:// geht für <video> nicht: GStreamer hat dafür keinen URI-Handler („Es ist kein
    URI-Handler für »couch« implementiert“), auch mit WEBKIT_GST_ALLOWED_URI_PROTOCOLS. Darum HTTP auf 127.0.0.1,
    je Trailer ein Zufallsschlüssel in der Adresse.
  * Ohne Länge und ohne Range spielt WebKit den Strom nur vorwärts (Schiebebetrieb, „Live stream detected“, kein
    Puffer auf der Platte) mit dem Hardware-Decoder (vah264dec); ein voller Trailer (77 s) lief in 4K durch.
  * Steams Stücke (styp, sidx, moof, mdat) aneinandergehängt ergeben eine Datei, deren Dauer GStreamer aus dem
    ersten Stück nimmt: Sie endete nach 3 s („die Trailer sind nur sehr kurz“). Darum fallen styp/sidx weg, und im
    Anfangsstück steht die Gesamtdauer (mehd).

Bild und Ton liefert Steam getrennt. Arten je Schlüssel:
    video.mp4   nur Bild (stummer Hintergrund)
    audio.m4a   nur Ton (Spielmusik ohne Bild)
    av.mp4      Bild und Ton in einer Datei (zwei Spuren, Stücke nach Zeit verzahnt): Ton zum Bild ohne Abgleich
Spiele, deren Ordner der Konsole gehört (Marker, installer/trash.open_own_dir), bekommen den Trailer zugleich als
Datei in <Spielordner>/.pc5-trailer/ (Größengrenze; geht beim Deinstallieren mit dem Ordner in den Papierkorb);
danach spielt die Datei. Alles andere wird nur gestreamt (2026-10-27).

Zeitgrenzen: je Abruf FETCH_TIMEOUT_S, je Strom STREAM_LIMIT_S über der Länge des Trailers; höchstens STREAMS
Ströme zugleich. Die Seite verwirft ihrerseits einen Trailer, der nicht anläuft oder hängt (components/trailer.js).
"""
from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import struct
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

from . import log

_log = log.get("trailerfeed")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) PC5-Launcher"
CHUNK = 256 * 1024
SEGMENT_MAX = 8 * 1024 * 1024           # größtes Stück, das geholt wird
STREAM_MAX = 160 * 1024 * 1024          # so viel geht höchstens durch einen Strom
FILE_MAX = {"video": 56 * 1024 * 1024, "audio": 8 * 1024 * 1024}    # Datei im eigenen Spielordner je Spur
FETCH_TIMEOUT_S = 15.0
STREAM_LIMIT_S = 120.0                  # Strom endet spätestens Länge + so viel
STREAMS = 4                             # Ströme zugleich (Hintergrund, Store, alte, die gerade enden)
PLAY_SLACK_S = 30.0                     # Hänger-Schutz: so lange nach dem geschätzten Ende gilt ein Trailer noch als laufend
TOKENS = 64
FOLDER = ".pc5-trailer"
FILES = {"video": "video.mp4", "audio": "audio.m4a", "av": "av.mp4"}
CTYPES = {"video": "video/mp4", "audio": "audio/mp4", "av": "video/mp4"}
KEY_RE = re.compile(r"^[0-9]{1,10}-[0-9a-f]{12}-[0-9]{2,4}$")
OWN_FILE_RE = re.compile(r"^[0-9]{1,10}-[0-9a-f]{12}-[0-9]{2,4}-(video\.mp4|audio\.m4a)(\.part)?$")
PATH_RE = re.compile(r"^/t/([A-Za-z0-9_-]{16,64})/(video\.mp4|audio\.m4a|av\.mp4)$")


class FeedError(Exception):
    pass


# ====================================================================== fMP4
def boxes(data: bytes, start: int = 0, end: int | None = None) -> list[tuple[bytes, bytes]]:
    """Kästen einer Ebene: [(Typ, ganzer Kasten)]. Wirft FeedError bei kaputten Größen."""
    end = len(data) if end is None else end
    out, off = [], start
    while off < end:
        if end - off < 8:
            raise FeedError("abgeschnittener Kasten")
        size, typ = struct.unpack(">I4s", data[off:off + 8])
        if size == 1:
            if end - off < 16:
                raise FeedError("abgeschnittener Kasten")
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
        elif size == 0:
            size = end - off
        if size < 8 or off + size > end:
            raise FeedError("Kasten %r mit falscher Größe" % typ)
        out.append((typ, data[off:off + size]))
        off += size
    return out


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def _children(box: bytes) -> list[tuple[bytes, bytes]]:
    return boxes(box, 8)


def _find(box_list: list[tuple[bytes, bytes]], typ: bytes) -> bytes | None:
    return next((b for t, b in box_list if t == typ), None)


def strip_segment(data: bytes) -> bytes:
    """Ein Stück ohne styp/sidx/prft (nur moof und mdat, in der Reihenfolge). Wirft FeedError ohne moof+mdat."""
    kept = [(t, b) for t, b in boxes(data) if t in (b"moof", b"mdat")]
    if not any(t == b"moof" for t, _b in kept) or not any(t == b"mdat" for t, _b in kept):
        raise FeedError("Stück ohne moof/mdat")
    return b"".join(b for _t, b in kept)


def _mvhd_timescale(mvhd: bytes) -> int:
    v = mvhd[8]
    return struct.unpack(">I", mvhd[20:24] if v == 0 else mvhd[28:32])[0]


def _mvhd_with(mvhd: bytes, duration: int, next_track: int | None = None) -> bytes:
    v = mvhd[8]
    if v == 0:
        out = mvhd[:24] + struct.pack(">I", min(duration, 0xFFFFFFFF)) + mvhd[28:]
    else:
        out = mvhd[:32] + struct.pack(">Q", duration) + mvhd[40:]
    if next_track is not None:
        out = out[:-4] + struct.pack(">I", next_track)
    return out


def _mehd(duration: int) -> bytes:
    if duration > 0xFFFFFFFF:
        return _box(b"mehd", struct.pack(">IQ", 1 << 24, duration))
    return _box(b"mehd", struct.pack(">II", 0, duration))


def _split_init(init: bytes) -> tuple[bytes, list[tuple[bytes, bytes]]]:
    """(ftyp, Kinder von moov). Wirft FeedError."""
    top = boxes(init)
    ftyp, moov = _find(top, b"ftyp"), _find(top, b"moov")
    if moov is None:
        raise FeedError("Anfangsstück ohne moov")
    kids = _children(moov)
    if _find(kids, b"mvhd") is None or _find(kids, b"trak") is None:
        raise FeedError("Anfangsstück ohne mvhd/trak")
    return ftyp or b"", kids


def with_duration(init: bytes, seconds: float) -> bytes:
    """Anfangsstück mit der Gesamtdauer (mehd in mvex, mvhd): Ohne sie nimmt GStreamer die Dauer des ersten Stücks."""
    ftyp, kids = _split_init(init)
    mvhd = _find(kids, b"mvhd")
    duration = max(1, int(round(float(seconds) * _mvhd_timescale(mvhd))))
    out = []
    for typ, box in kids:
        if typ == b"mvhd":
            box = _mvhd_with(box, duration)
        elif typ == b"mvex":
            box = _box(b"mvex", _mehd(duration) + b"".join(b for t, b in _children(box) if t != b"mehd"))
        out.append(box)
    return ftyp + _box(b"moov", b"".join(out))


def _set_u32(box: bytes, offset: int, value: int) -> bytes:
    return box[:offset] + struct.pack(">I", value) + box[offset + 4:]


def _retrack_trak(trak: bytes, track_id: int) -> bytes:
    out = []
    for typ, box in _children(trak):
        if typ == b"tkhd":
            box = _set_u32(box, 20 if box[8] == 0 else 28, track_id)
        out.append(box)
    return _box(b"trak", b"".join(out))


def track_timescale(init: bytes) -> int:
    """Zeitmaß (mdhd) der ersten Spur eines Anfangsstücks."""
    _ftyp, kids = _split_init(init)
    trak = _find(kids, b"trak")
    mdia = _find(_children(trak), b"mdia")
    mdhd = _find(_children(mdia), b"mdhd") if mdia else None
    if mdhd is None:
        raise FeedError("Spur ohne mdhd")
    return struct.unpack(">I", mdhd[20:24] if mdhd[8] == 0 else mdhd[28:32])[0] or 1


def mux_init(video: bytes, audio: bytes, seconds: float) -> bytes:
    """Ein Anfangsstück mit beiden Spuren: Bild als Spur 1, Ton als Spur 2, Gesamtdauer in mehd."""
    ftyp, vkids = _split_init(video)
    _aftyp, akids = _split_init(audio)
    vmvhd, amvhd = _find(vkids, b"mvhd"), _find(akids, b"mvhd")
    scale = _mvhd_timescale(vmvhd)
    if _mvhd_timescale(amvhd) != scale:
        raise FeedError("Bild und Ton mit verschiedenem Zeitmaß")
    duration = max(1, int(round(float(seconds) * scale)))
    vtrex = _find(_children(_find(vkids, b"mvex") or _box(b"mvex", b"")), b"trex")
    atrex = _find(_children(_find(akids, b"mvex") or _box(b"mvex", b"")), b"trex")
    if vtrex is None or atrex is None:
        raise FeedError("Anfangsstück ohne trex")
    mvex = _box(b"mvex", _mehd(duration) + _set_u32(vtrex, 12, 1) + _set_u32(atrex, 12, 2))
    moov = _box(b"moov", _mvhd_with(vmvhd, duration, next_track=3) + _retrack_trak(_find(vkids, b"trak"), 1)
                + _retrack_trak(_find(akids, b"trak"), 2) + mvex)
    return ftyp + moov


def retrack(segment: bytes, track_id: int, seq: int) -> tuple[bytes, int]:
    """Stück (moof+mdat …) mit neuer Spurnummer (tfhd) und laufender Folgenummer (mfhd). → (Stück, nächste Nummer).
    Die Datenzeiger der trun zählen ab moof (default-base-is-moof), Umordnen der Stücke ändert sie nicht."""
    out = []
    for typ, box in boxes(segment):
        if typ == b"moof":
            kids = []
            for t2, b2 in _children(box):
                if t2 == b"mfhd":
                    b2 = _set_u32(b2, 12, seq)
                    seq += 1
                elif t2 == b"traf":
                    b2 = _box(b"traf", b"".join(_set_u32(b3, 12, track_id) if t3 == b"tfhd" else b3
                                                for t3, b3 in _children(b2)))
                kids.append(b2)
            box = _box(b"moof", b"".join(kids))
        out.append(box)
    return b"".join(out), seq


def segment_time(segment: bytes, timescale: int) -> float:
    """Beginn eines Stücks in Sekunden (tfdt der ersten traf)."""
    for typ, box in boxes(segment):
        if typ != b"moof":
            continue
        for t2, b2 in _children(box):
            if t2 != b"traf":
                continue
            tfdt = _find(_children(b2), b"tfdt")
            if tfdt is not None:
                base = struct.unpack(">I", tfdt[12:16])[0] if tfdt[8] == 0 else struct.unpack(">Q", tfdt[12:20])[0]
                return base / float(timescale or 1)
    return 0.0


# ====================================================================== Abruf
def fetch(url: str, timeout: float = FETCH_TIMEOUT_S, limit: int = SEGMENT_MAX) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 (Adressen geprüft, siehe Feed)
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise FeedError("Stück zu groß")
    return data


# ====================================================================== eigener Spielordner
class OwnDir:
    """<Spielordner>/.pc5-trailer eines eigenen Spiels, über Deskriptoren (nie über einen Pfad hinweg).
    open_dir() liefert den Deskriptor des geprüften Spielordners (installer/trash.open_own_dir)."""

    def __init__(self, open_dir: Callable[[], int], label: str = ""):
        self.open_dir = open_dir
        self.label = label

    def open(self) -> int:
        from .installer import trash
        game_fd = self.open_dir()
        try:
            return trash.open_own_subdir(game_fd, FOLDER)
        finally:
            os.close(game_fd)


_writing: set = set()                    # (Gerät, Inode des Ordners, Name): eine Teildatei hat genau einen Schreiber
_writing_lock = threading.Lock()


def _is_file(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def _unlink_own(dir_fd: int, name: str) -> None:
    """Eine eigene Datei (Name nach OWN_FILE_RE, gewöhnliche Datei) einzeln entfernen; alles andere bleibt."""
    if OWN_FILE_RE.match(name) and _is_file(dir_fd, name) is not None:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


class Tee:
    """Schreibt die verarbeiteten Bytes einer Spur in <key>-<spur>.part und benennt die fertige Datei um."""

    def __init__(self, dir_fd: int, name: str, limit: int):
        self.dir_fd, self.name, self.limit = dir_fd, name, limit
        self.part = name + ".part"
        st = os.fstat(dir_fd)
        self.slot = (st.st_dev, st.st_ino, name)
        with _writing_lock:
            if self.slot in _writing:
                raise FeedError("schreibt schon")
            _writing.add(self.slot)
        self.fd = -1
        try:
            _unlink_own(dir_fd, self.part)               # Rest eines abgebrochenen Laufs
            self.fd = os.open(self.part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                              dir_fd=dir_fd)
        except BaseException:
            self._release()
            raise
        self.size = 0
        self.failed = False

    def _release(self) -> None:
        with _writing_lock:
            _writing.discard(self.slot)

    def write(self, data: bytes) -> None:
        if self.failed:
            return
        if self.size + len(data) > self.limit:
            self.abort("zu groß")
            return
        try:
            view = memoryview(data)
            while view:
                n = os.write(self.fd, view)
                view = view[n:]
            self.size += len(data)
        except OSError as e:
            self.abort(e.strerror or "Schreibfehler")

    def abort(self, why: str = "") -> None:
        if self.failed:
            return
        self.failed = True
        if why:
            _log.info("Trailer-Datei %s nicht abgelegt: %s", self.name, why)
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        _unlink_own(self.dir_fd, self.part)
        self._release()

    def commit(self) -> bool:
        if self.failed:
            return False
        try:
            os.fsync(self.fd)
            os.close(self.fd)
            self.fd = -1
            from .installer import trash
            trash.rename_noreplace(self.part, self.name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        except (OSError, Exception) as e:  # noqa: BLE001 (Refuse aus trash)
            self.abort(str(e))
            return False
        self.failed = True                               # fertig: nichts mehr schreiben
        self._release()
        return True


# ====================================================================== Quellen einer Spur
class Track:
    """Eine Spur: Anfangsstück und Stücke aus dem CDN oder aus der abgelegten Datei; schreibt bei Bedarf mit."""

    def __init__(self, feed: "Feed", kind: str, spec: dict, own_fd: int | None, key: str, cancel: threading.Event,
                 estimate: float = 0.0):
        self.feed, self.kind, self.spec, self.key, self.cancel = feed, kind, spec, key, cancel
        self.own_fd = own_fd
        self.file = None
        self.tee: Tee | None = None
        self.next = 0                                    # nächstes Stück aus dem CDN
        name = "%s-%s" % (key, FILES[kind])
        self.name = name
        if own_fd is not None:
            st = _is_file(own_fd, name)
            if st is not None and 0 < st.st_size <= FILE_MAX[kind]:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=own_fd)
                self.file = os.fdopen(fd, "rb")
            elif estimate <= FILE_MAX[kind]:
                try:
                    self.tee = Tee(own_fd, name, FILE_MAX[kind])
                except (FeedError, OSError) as e:
                    _log.info("Trailer %s wird nur gestreamt (%s)", name, e)

    @property
    def saved(self) -> bool:
        return self.file is not None

    def _get(self, url: str) -> bytes:
        if self.cancel.is_set():
            raise FeedError("abgebrochen")
        return self.feed.get(url)

    def init(self) -> bytes:
        if self.file is not None:
            top = []
            while len(top) < 2:
                head = self.file.read(8)
                if len(head) < 8:
                    raise FeedError("abgelegte Datei abgeschnitten")
                size = struct.unpack(">I", head[:4])[0]
                if size < 8 or size > SEGMENT_MAX:
                    raise FeedError("abgelegte Datei kaputt")
                top.append(head + self.file.read(size - 8))
            return b"".join(top)
        data = with_duration(self._get(self.spec["init"]), self.spec["seconds"])
        if self.tee:
            self.tee.write(data)
        return data

    def segments(self) -> Iterator[bytes]:
        """Die Stücke in Reihenfolge (je moof+mdat bzw. ein ganzes Stück aus dem CDN)."""
        if self.file is not None:
            while True:
                head = self.file.read(8)
                if not head:
                    return
                if len(head) < 8:
                    raise FeedError("abgelegte Datei abgeschnitten")
                size = struct.unpack(">I", head[:4])[0]
                if size < 8 or size > SEGMENT_MAX:
                    raise FeedError("abgelegte Datei kaputt")
                box = head + self.file.read(size - 8)
                if box[4:8] == b"moof":
                    head = self.file.read(8)
                    msize = struct.unpack(">I", head[:4])[0] if len(head) == 8 else 0
                    if msize < 8 or msize > SEGMENT_MAX or head[4:8] != b"mdat":
                        raise FeedError("abgelegte Datei kaputt")
                    box += head + self.file.read(msize - 8)
                yield box
        parts = self.spec["parts"]
        while self.next < len(parts):
            url = parts[self.next][0]
            data = strip_segment(self._get(url))
            self.next += 1
            if self.tee:
                self.tee.write(data)
            yield data
        if self.tee:
            if self.tee.commit():
                self.feed.saved(self.key, self.kind)
            self.tee = None

    def finish(self) -> None:
        """Die Seite hört nicht mehr zu: den Rest nur noch in die Datei holen (eigene Spiele), sonst nichts."""
        if self.tee is None or self.tee.failed:
            self.close()
            return
        try:
            for _ in self.segments():
                if self.feed.closed.is_set():
                    raise FeedError("Home endet")
        except Exception as e:  # noqa: BLE001
            if self.tee:
                self.tee.abort(str(e))
        self.close()

    def close(self) -> None:
        if self.tee is not None:
            self.tee.abort()
            self.tee = None
        if self.file is not None:
            self.file.close()
            self.file = None


# ====================================================================== Dienst
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    feed: "Feed"

    def handle_error(self, request, client_address):  # noqa: D401 (keine Spuren auf stderr)
        _log.debug("Trailer-Verbindung endete mit Fehler")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "couch"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _fail(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_HEAD(self):
        self._fail(405)

    def do_POST(self):
        self._fail(405)

    def do_GET(self):
        feed = self.server.feed
        # Nur unsere eigene Adresse (kein fremder Rechnername über DNS auf 127.0.0.1), nur bekannte Schlüssel
        if self.headers.get("Host", "") != feed.host:
            return self._fail(403)
        m = PATH_RE.match(self.path)
        if not m:
            return self._fail(404)
        token, file_name = m.group(1), m.group(2)
        kind = next(k for k, v in FILES.items() if v == file_name)
        spec = feed.lookup(token)
        if spec is None or (kind != "video" and spec.get("audio") is None):
            return self._fail(404)
        rng = self.headers.get("Range", "")
        if rng and not re.fullmatch(r"bytes=0-\d*", rng.strip()):
            return self._fail(416)                      # Vorspulen kann ein Strom nicht
        stream = feed.open_stream(token, spec, kind)
        if stream is None:
            return self._fail(503)
        try:
            self.send_response(200)
            self.send_header("Content-Type", CTYPES[kind])
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for chunk in stream.chunks():
                view = memoryview(chunk)
                for off in range(0, len(view), CHUNK):
                    self.wfile.write(view[off:off + CHUNK])
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass                                        # Seite hat aufgehört (anderes Spiel, Ende, Fehler)
        except FeedError as e:
            _log.info("Trailer-Strom %s endet: %s", spec.get("key"), e)
        except OSError as e:
            if e.errno not in (errno.EPIPE, errno.ECONNRESET):
                _log.info("Trailer-Strom: %s", e)
        except Exception:
            _log.exception("Trailer-Strom %s", spec.get("key"))
        finally:
            stream.close()


class Stream:
    def __init__(self, feed: "Feed", token: str, spec: dict, kind: str):
        self.feed, self.token, self.spec, self.kind = feed, token, spec, kind
        self.cancel = threading.Event()
        self.tracks: list[Track] = []
        self.started = time.monotonic()
        self.sent = 0
        self.dir_fd: int | None = None

    def _own(self) -> int | None:
        own = self.spec.get("own")
        if own is None:
            return None
        try:
            return own.open()
        except Exception as e:  # noqa: BLE001 (Refuse, OSError): dann eben nur streamen
            _log.info("Trailer für %s nur gestreamt: %s", own.label or self.spec.get("key"), e)
            return None

    def _limit(self, data: bytes) -> bytes:
        self.sent += len(data)
        if self.sent > STREAM_MAX:
            raise FeedError("Strom zu groß")
        if time.monotonic() - self.started > float(self.spec.get("seconds") or 0) + STREAM_LIMIT_S:
            raise FeedError("Zeitgrenze des Stroms")
        if self.feed.closed.is_set():
            raise FeedError("Home endet")
        return data

    def chunks(self) -> Iterator[bytes]:
        self.dir_fd = self._own()
        key = self.spec["key"]
        if self.kind in ("video", "av"):
            self.tracks.append(Track(self.feed, "video", self.spec["video"], self.dir_fd, key, self.cancel,
                                     float(self.spec.get("estimate") or 0)))
        if self.kind in ("audio", "av"):
            self.tracks.append(Track(self.feed, "audio", self.spec["audio"], self.dir_fd, key, self.cancel))
        if self.kind != "av":
            track = self.tracks[0]
            yield self._limit(track.init())
            for seg in track.segments():
                yield self._limit(seg)
            return
        video, audio = self.tracks
        vinit, ainit = video.init(), audio.init()
        yield self._limit(mux_init(vinit, ainit, self.spec["video"]["seconds"]))
        vscale, ascale = track_timescale(vinit), track_timescale(ainit)
        seq = 1
        vit, ait = video.segments(), audio.segments()
        vnext, anext = next(vit, None), next(ait, None)
        while vnext is not None or anext is not None:
            take_video = anext is None or (vnext is not None and segment_time(vnext, vscale) <= segment_time(anext, ascale))
            if take_video:
                out, seq = retrack(vnext, 1, seq)
                vnext = next(vit, None)
            else:
                out, seq = retrack(anext, 2, seq)
                anext = next(ait, None)
            yield self._limit(out)

    def close(self) -> None:
        self.cancel.set()
        self.feed.stream_done(self)                     # die Seite spielt nicht mehr: Platz für den nächsten Strom
        try:
            for track in self.tracks:
                # eigene Spiele: den Rest noch in die Datei holen (ohne Abbruchmarke), sonst nichts
                track.cancel = threading.Event()
                track.finish()
        finally:
            if self.dir_fd is not None:
                os.close(self.dir_fd)
                self.dir_fd = None


class Feed:
    """Der Dienst: register(spec) -> Schlüssel, url(Schlüssel, Art). spec = {"key", "seconds", "estimate",
    "video": {"init", "parts", "seconds"}, "audio": {…} | None, "own": OwnDir | None}."""

    def __init__(self, get: Callable[[str], bytes] = fetch, allowed: Callable[[str], bool] | None = None):
        self._get = get
        self._allowed = allowed
        self._lock = threading.Lock()
        self._specs: "OrderedDict[str, dict]" = OrderedDict()
        self._by_key: dict[str, str] = {}
        self._streams: set = set()
        # (Beginn, Ende der Wiedergabe geschätzt, key) für den Hänger-Schutz. Der Strom selbst ist meist nach Sekunden
        # durch (das Netz-Programm von WebKit liest vor), gespielt wird bis Beginn + Länge: so lange gilt er als aktiv.
        self._plays: list[tuple[float, float, str]] = []
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.closed = threading.Event()
        self.on_saved: Callable[[str, str], None] | None = None
        self.host = ""
        self.port = 0

    # ------------------------------------------------------------ Lebenslauf
    def start(self) -> None:
        with self._lock:
            if self._server is not None or self.closed.is_set():
                return
            server = _Server(("127.0.0.1", 0), _Handler)
            server.feed = self
            self.port = server.server_address[1]
            self.host = "127.0.0.1:%d" % self.port
            self._server = server
            self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5},
                                            name="couch-trailerfeed", daemon=True)
            self._thread.start()
        _log.info("Trailer-Dienst auf %s", self.host)

    def stop(self) -> None:
        self.closed.set()
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    # ------------------------------------------------------------ Schlüssel
    def register(self, spec: dict) -> str:
        """Schlüssel für einen Trailer; derselbe key bekommt denselben Schlüssel (die Seite merkt sich Antworten)."""
        key = str(spec.get("key") or "")
        if not KEY_RE.match(key):
            raise FeedError("ungültiger key")
        for track in (spec.get("video"), spec.get("audio")):
            if track is None:
                continue
            for url in [track["init"]] + [p[0] for p in track["parts"]]:
                if self._allowed is not None and not self._allowed(url):
                    raise FeedError("Adresse nicht erlaubt")
        if spec.get("video") is None:
            raise FeedError("ohne Bild")
        self.start()
        with self._lock:
            token = self._by_key.get(key)
            if token is None or token not in self._specs:
                token = secrets.token_urlsafe(24)
            self._specs[token] = dict(spec)
            self._specs.move_to_end(token)
            self._by_key[key] = token
            while len(self._specs) > TOKENS:
                old, gone = self._specs.popitem(last=False)
                if self._by_key.get(gone.get("key")) == old:
                    del self._by_key[gone["key"]]
        return token

    def lookup(self, token: str) -> dict | None:
        with self._lock:
            return self._specs.get(token)

    def url(self, token: str, kind: str) -> str:
        return "http://%s/t/%s/%s" % (self.host, token, FILES[kind])

    # ------------------------------------------------------------ Ströme
    def get(self, url: str) -> bytes:
        if self._allowed is not None and not self._allowed(url):
            raise FeedError("Adresse nicht erlaubt")
        try:
            return self._get(url)
        except urllib.error.HTTPError as e:
            e.close()
            raise FeedError("HTTP %d" % e.code)
        except (OSError, ValueError) as e:
            raise FeedError(str(e) or type(e).__name__)

    def open_stream(self, token: str, spec: dict, kind: str) -> Stream | None:
        with self._lock:
            if len(self._streams) >= STREAMS or self.closed.is_set():
                return None
            stream = Stream(self, token, spec, kind)
            self._streams.add(stream)
            now = time.monotonic()
            self._plays.append((now, now + float(spec.get("seconds") or 0) + PLAY_SLACK_S, str(spec.get("key") or "")))
            self._plays = self._plays[-16:]
        return stream

    def stream_done(self, stream: Stream) -> None:
        with self._lock:
            self._streams.discard(stream)

    def active(self) -> set[str]:
        """Schlüssel (key) der Trailer, die gerade gestreamt werden oder nach ihrem Beginn noch spielen könnten
        (Beginn bis Beginn + Länge + PLAY_SLACK_S): Hängt jetzt die Seite, war womöglich er es."""
        now = time.monotonic()
        with self._lock:
            keys = {s.spec.get("key", "") for s in self._streams}
            keys |= {k for start, end, k in self._plays if start <= now <= end}
        return keys

    def saved(self, key: str, kind: str) -> None:
        _log.info("Trailer %s (%s) im Spielordner abgelegt", key, kind)
        if self.on_saved is not None:
            try:
                self.on_saved(key, kind)
            except Exception:
                _log.exception("on_saved")


def own_files(dir_fd: int, key: str) -> dict:
    """Welche Spuren eines Trailers im eigenen Ordner fertig liegen: {"video": bool, "audio": bool}."""
    out = {}
    for kind in ("video", "audio"):
        st = _is_file(dir_fd, "%s-%s" % (key, FILES[kind]))
        out[kind] = bool(st is not None and 0 < st.st_size <= FILE_MAX[kind])
    return out


def prune(dir_fd: int, keep_key: str) -> int:
    """Ältere eigene Trailer-Dateien im Ordner einzeln entfernen (nur Namen nach OWN_FILE_RE, nur gewöhnliche
    Dateien, keine Teildatei, in die gerade jemand schreibt). → Zahl der entfernten Dateien."""
    st = os.fstat(dir_fd)
    gone = 0
    for name in os.listdir(dir_fd):
        if not OWN_FILE_RE.match(name) or name.startswith(keep_key + "-"):
            continue
        with _writing_lock:
            busy = (st.st_dev, st.st_ino, name[:-5] if name.endswith(".part") else name) in _writing
        if busy:
            continue
        _unlink_own(dir_fd, name)
        gone += 1
    return gone
