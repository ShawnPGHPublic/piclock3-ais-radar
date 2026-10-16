"""A half-circle of AIS traffic, looking out from a point on the shore.

The flat edge is the beach.  Targets in the water are dots; the beam is
only a sweep, the way a plan-position display draws one.  AISHub is polled.
AISStream stays open while the page is the one on screen, and is closed
when it is not - a hidden page does not hold a socket.

The widget fills whatever region the layout gave it, and the caption uses
font-size as a fraction of that region, which is how every widget sizes.
"""
import logging
import math
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

from PyQt5.QtCore import QPointF, QRectF, Qt, QThread, QTimer
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt5.QtNetwork import QNetworkReply
from PyQt5.QtWidgets import QLabel, QWidget

from PiClock3.WebGet import WebGet
from PiClock3.Widget import Widget

from .radarmath import (
    bounding_box,
    format_radius,
    in_scope,
    mark_pixels,
    normalize_unit,
    parse_aishub,
    parse_aisstream,
    project,
    radius_meters,
    remember_identity,
    stream_note,
)
from .stream import dumps, open_stream, Reader

logger = logging.getLogger(__name__)

GLASS = QColor(6, 18, 13, 235)
AIS_HUB = 'https://data.aishub.net/ws.php'


class Scope(QWidget):
    """the half-disk.  The caption is a separate label under it."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.targets = []
        self.sweep = 0.0
        self.bearing = 0.0
        self.radius_m = 1.0
        self.top_gap = 0.0
        self.trace = QColor('#5EE08A')
        self.rings = ('', '')

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        width, height = self.width(), self.height()
        if width < 8 or height < 8:
            painter.end()
            return
        gap = max(0.0, float(self.top_gap))
        radius = min(width / 2.0, max(1.0, height - gap))
        cx = width / 2.0
        cy = gap + radius
        diameter = radius * 2.0
        self._glass(painter, cx, cy, radius, diameter)
        self._rings(painter, cx, cy, radius)
        self._spokes(painter, cx, cy, radius)
        self._sweep(painter, cx, cy, radius)
        self._targets(painter, cx, cy, radius)
        self._marks(painter, cx, cy, radius)
        painter.end()

    def _glass(self, painter, cx, cy, radius, diameter):
        path = QPainterPath()
        path.moveTo(cx + radius, cy)
        # 0 degrees is east, positive is counterclockwise, so 180 degrees
        # of that is the upper half: east, through north, to west.
        path.arcTo(cx - radius, cy - radius, diameter, diameter, 0, 180)
        path.closeSubpath()
        painter.setClipPath(path)
        painter.fillPath(path, GLASS)
        pen = QPen(self._dim(160))
        pen.setWidthF(max(1.0, radius * 0.008))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

    def _rings(self, painter, cx, cy, radius):
        pen = QPen(self._dim(110))
        pen.setWidthF(max(1.0, radius * 0.004))
        painter.setPen(pen)
        for frac in (1.0 / 3.0, 2.0 / 3.0, 1.0):
            r = radius * frac
            painter.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 0, 180 * 16)
        pen.setColor(self._dim(200))
        pen.setWidthF(max(1.2, radius * 0.01))
        painter.setPen(pen)
        painter.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))

    def _spokes(self, painter, cx, cy, radius):
        pen = QPen(self._dim(70))
        pen.setWidthF(max(1.0, radius * 0.003))
        painter.setPen(pen)
        for deg in range(-60, 61, 30):
            self._spoke(painter, cx, cy, radius, deg)

    def _sweep(self, painter, cx, cy, radius):
        # sweep runs 0..180 across the half-disk, left shore to right shore.
        for i in range(22, -1, -1):
            deg = -90.0 + self.sweep - i * 1.6
            if deg < -90 or deg > 90:
                continue
            color = QColor(self.trace)
            color.setAlpha(int(150 * (1.0 - i / 22.0)))
            pen = QPen(color)
            pen.setWidthF(max(1.0, radius * (0.012 if i == 0 else 0.004)))
            painter.setPen(pen)
            self._spoke(painter, cx, cy, radius, deg)

    def _targets(self, painter, cx, cy, radius):
        ordered = sorted(self.targets, key=lambda item: -(item[6] or 0))
        for right, forward, sog, cog, heading, kind, length, beam in ordered:
            x = cx + right * radius
            y = cy - forward * radius
            moving = sog is not None and sog >= 0.4
            sized = length is not None and length > 0
            if kind in (None, '', 'other') and not sized:
                self._dot(painter, x, y, radius, moving, sog, cog)
                continue
            len_px, beam_px = mark_pixels(length, beam, kind, radius, self.radius_m or 1)
            rel = (heading if heading is not None else cog)
            if rel is None:
                rel = self.bearing
            color = QColor(self.trace)
            color.setAlpha(230 if moving else 140)
            painter.save()
            painter.translate(x, y)
            painter.rotate(rel - self.bearing)
            pen = QPen(color)
            pen.setWidthF(max(1.0, min(len_px, beam_px) * 0.08))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            self._hull(painter, kind, len_px, beam_px)
            painter.restore()

    def _dot(self, painter, x, y, radius, moving, sog, cog):
        dot = max(2.0, radius * (0.014 if moving else 0.009))
        color = QColor(self.trace)
        color.setAlpha(230 if moving else 130)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(x, y), dot, dot)
        if moving and cog is not None:
            pen = QPen(color)
            pen.setWidthF(max(1.0, radius * 0.004))
            painter.setPen(pen)
            rel = math.radians(cog - self.bearing)
            length = min(radius * 0.09, radius * (0.018 + sog * 0.003))
            painter.drawLine(
                QPointF(x, y),
                QPointF(x + math.sin(rel) * length,
                        y - math.cos(rel) * length))

    def _hull(self, painter, kind, length, beam):
        # bow is up. Qt y grows down, so the bow is the negative-y end.
        path = QPainterPath()
        half_l = length / 2.0
        half_w = max(1.0, beam / 2.0)
        if kind == 'pleasure':
            path.moveTo(0, -half_l)
            path.lineTo(half_w, half_l)
            path.lineTo(-half_w, half_l)
        elif kind == 'fast':
            path.moveTo(0, -half_l)
            path.lineTo(half_w, length * 0.05)
            path.lineTo(beam * 0.14, half_l)
            path.lineTo(-beam * 0.14, half_l)
            path.lineTo(-half_w, length * 0.05)
        elif kind == 'fishing':
            path.moveTo(0, -half_l)
            path.lineTo(half_w, -length / 6.0)
            path.lineTo(half_w, half_l)
            path.lineTo(-half_w, half_l)
            path.lineTo(-half_w, -length / 6.0)
        elif kind == 'tug':
            bow = length * 0.38
            path.moveTo(0, -half_l)
            path.lineTo(half_w, -half_l + bow)
            path.lineTo(half_w, half_l)
            path.lineTo(-half_w, half_l)
            path.lineTo(-half_w, -half_l + bow)
        else:
            bow = length * (0.22 if kind == 'passenger' else 0.16 if kind == 'tanker' else 0.12)
            path.moveTo(0, -half_l)
            path.lineTo(half_w, -half_l + bow)
            path.lineTo(half_w, half_l - length * 0.05)
            path.lineTo(beam * 0.16, half_l)
            path.lineTo(-beam * 0.16, half_l)
            path.lineTo(-half_w, half_l - length * 0.05)
            path.lineTo(-half_w, -half_l + bow)
        path.closeSubpath()
        painter.drawPath(path)
        if kind == 'tanker':
            r = max(1.3, beam * 0.22)
            painter.drawEllipse(QPointF(0, length * 0.02), r, r)
        elif kind == 'passenger':
            painter.drawRect(QRectF(-beam * 0.28, length * 0.02, beam * 0.56, length * 0.28))
        elif kind == 'cargo':
            painter.drawLine(QPointF(-beam * 0.28, -length * 0.02),
                             QPointF(beam * 0.28, -length * 0.02))
            painter.drawLine(QPointF(-beam * 0.28, length * 0.16),
                             QPointF(beam * 0.28, length * 0.16))
        elif kind == 'fishing':
            painter.drawLine(QPointF(-beam * 0.85, -length * 0.02),
                             QPointF(beam * 0.85, -length * 0.02))

    def _marks(self, painter, cx, cy, radius):
        painter.setPen(self._dim(210))
        font = painter.font()
        font.setPixelSize(max(8, int(radius * 0.055)))
        painter.setFont(font)
        labels = (
            (self._deg(self.bearing - 90), -78),
            (self._deg(self.bearing), 0),
            (self._deg(self.bearing + 90), 78),
        )
        for text, deg in labels:
            x, y = self._at(cx, cy, radius * 0.86, deg)
            painter.drawText(QRectF(x - 28, y - 10, 56, 20),
                             Qt.AlignCenter, text)
        font.setPixelSize(max(7, int(radius * 0.042)))
        painter.setFont(font)
        painter.setPen(self._dim(170))
        for frac, text in ((1.0 / 3.0, self.rings[0]), (2.0 / 3.0, self.rings[1])):
            if not text:
                continue
            y = cy - radius * frac
            painter.drawText(QRectF(cx + 4, y - 12, radius * 0.4, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.setBrush(self.trace)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(cx, cy), max(2.0, radius * 0.012),
                            max(2.0, radius * 0.012))

    def _spoke(self, painter, cx, cy, radius, deg):
        x, y = self._at(cx, cy, radius, deg)
        painter.drawLine(QPointF(cx, cy), QPointF(x, y))

    def _at(self, cx, cy, radius, deg):
        rad = math.radians(deg)
        return (cx + math.sin(rad) * radius, cy - math.cos(rad) * radius)

    def _dim(self, alpha):
        color = QColor(self.trace)
        color.setAlpha(alpha)
        return color

    @staticmethod
    def _deg(bearing):
        return '%d' % (int(round(bearing)) % 360)


class _AisThread(QThread):
    """holds the AISStream socket off the UI thread and queues texts."""

    def __init__(self, api_key, box):
        super().__init__()
        self.api_key = api_key
        self.box = box
        self.error = ''
        self.subscribed = False
        self._stop = False
        self._sock = None
        self._lock = threading.Lock()
        self._queue = []

    def stop(self):
        self._stop = True
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def drain(self):
        with self._lock:
            items = self._queue
            self._queue = []
        return items

    def run(self):
        sock = None
        try:
            sock, rest = open_stream()
            self._sock = sock
            from .stream import send_text
            send_text(sock, dumps(self.api_key, self.box))
            self.subscribed = True
            reader = Reader(sock, rest, lambda: self._stop)
            for text in reader.messages():
                if self._stop:
                    break
                with self._lock:
                    self._queue.append(text)
                    extra = len(self._queue) - 400
                    if extra > 0:
                        del self._queue[:extra]
        except Exception as exc:
            if not self._stop:
                self.error = 'stream down'
                logger.warning('aisstream %s', type(exc).__name__)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


class AisRadar(Widget):
    """called whatever suits.  The loader finds the Plugin subclass."""

    def __init__(self, piclock, name, config):
        super().__init__(piclock, name, config)
        self.scope = None
        self.caption = None
        self.anim = None
        self.timer = None
        self.targets = {}
        self.identity = {}
        self.last_ok = None
        self.status = 'waiting'
        self.sweep = 0.0
        self._sized = None
        self._inflight = False
        self.stream = None
        self.stream_next = 0.0
        self._box = None

    def start(self):
        rect = self.region.frameRect()
        self.scope = Scope(self.region)
        self.caption = QLabel(self.region)
        self.caption.setObjectName('aisradar')
        self.caption.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self._layout(rect)
        self.applyEffect(self.caption, rect.height())

        self.anim = QTimer(self)
        self.anim.timeout.connect(self._animate)
        self.anim.start(50)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.fetch)
        self.timer.start(self._refresh_ms())
        self.fetch()

    def pageChange(self):
        if self.region.isVisible():
            self.fetch()
        else:
            self._stop_stream()

    def fetch(self):
        if self.scope is None or not self.region.isVisible():
            return
        self._prune()
        if self._source() == 'aisstream':
            self._ensure_stream()
        else:
            self._fetch_aishub()
        self._caption()

    def _animate(self):
        if self.scope is None or not self.region.isVisible():
            return
        rect = self.region.frameRect()
        if (rect.width(), rect.height()) != self._sized:
            self._layout(rect)
        if self.stream is not None:
            self._drain()
            self._mark_live()
        if self._source() == 'aisstream':
            self._ensure_stream()
        period = max(2.0, float(self.config.get('sweep') or 6))
        self.sweep = (self.sweep + (0.050 / period) * 180.0) % 180.0
        self.scope.sweep = self.sweep
        self.scope.bearing = self._bearing()
        self.scope.trace = self._trace()
        try:
            self.scope.radius_m = self._place()[2]
        except (TypeError, ValueError):
            self.scope.radius_m = 1.0
        self.scope.targets = self._visible()
        self.scope.rings = self._ring_labels()
        self.scope.setFont(self.region.font())
        self.scope.update()

    def _layout(self, rect):
        props = self.scaleFont({'font-size': self.config['font-size']},
                               rect.height())
        px = _px(props.get('font-size'), rect.height())
        cap = max(8, int(round(px * 1.25)))
        radius = min(rect.width() / 2.0, max(1.0, rect.height() - cap))
        scope_h = int(round(radius))
        scope_h = min(scope_h, max(1, rect.height() - cap))
        self.scope.top_gap = 0
        self.scope.setGeometry(0, 0, rect.width(), scope_h)
        self.caption.setGeometry(0, scope_h, rect.width(), cap)
        color = self.color()
        if color:
            props['color'] = color
        self.caption.setStyleSheet(self.styleRule('aisradar', props))
        self._sized = (rect.width(), rect.height())
        self._caption()

    def _caption(self):
        if self.caption is None:
            return
        try:
            label = format_radius(self.config['radius'],
                                  self.config['radius-unit'])
        except (TypeError, ValueError):
            label = 'radius'
        if self.last_ok is None:
            when = self.status or 'waiting'
        else:
            when = self._clock(self.last_ok)
        self.caption.setText('%s  ·  %s' % (label, when))

    def _clock(self, epoch):
        try:
            zone = self.now().tzinfo
        except Exception:
            zone = None
        moment = datetime.fromtimestamp(epoch, zone or timezone.utc)
        return moment.strftime('%H:%M:%S')

    def _fetch_aishub(self):
        if self._inflight:
            return
        username = self._secret('username')
        if not username:
            self.status = 'no key'
            return
        try:
            lat, lon, radius_m = self._place()
        except (TypeError, ValueError):
            self.status = 'bad position'
            return
        box = bounding_box(lat, lon, radius_m)
        url = (
            '%s?username=%s&format=1&output=json&compress=0'
            '&latmin=%.5f&latmax=%.5f&lonmin=%.5f&lonmax=%.5f&interval=%d'
            % (AIS_HUB, quote(username), box['latmin'], box['latmax'],
               box['lonmin'], box['lonmax'], self._max_age())
        )
        self._inflight = True
        WebGet(url, self._on_aishub)

    def _on_aishub(self, error, data, _params):
        self._inflight = False
        if error != QNetworkReply.NoError or not data:
            self.status = 'no reply'
            self._caption()
            return
        try:
            raw = _body(data)
            vessels, err = parse_aishub(raw)
        except Exception:
            logger.warning('%s: could not read AISHub reply', self.name)
            self.status = 'bad reply'
            self._caption()
            return
        if err:
            self.status = err
            self._caption()
            return
        now = time.time()
        self.targets = {}
        for vessel in vessels:
            self._remember(vessel, now)
        self.last_ok = now
        self.status = ''
        self._caption()

    def _ensure_stream(self):
        if self.stream is not None and self.stream.isRunning():
            if self.stream.box != self._wanted_box():
                self._stop_stream()
            else:
                return
        now = time.time()
        if now < self.stream_next:
            return
        key = self._secret('apikey')
        if not key:
            self.status = 'no key'
            return
        try:
            box = self._wanted_box()
        except (TypeError, ValueError):
            self.status = 'bad position'
            return
        self.stream_next = now + 15
        self._box = box
        self.stream = _AisThread(key, box)
        self.stream.start()
        self.status = self.status if self.last_ok else 'connecting'

    def _wanted_box(self):
        lat, lon, radius_m = self._place()
        box = bounding_box(lat, lon, radius_m)
        return (round(box['latmin'], 4), round(box['latmax'], 4),
                round(box['lonmin'], 4), round(box['lonmax'], 4))

    def _mark_live(self):
        thread = self.stream
        if thread is None or not thread.subscribed or thread.error:
            return
        if self.status == 'connecting':
            self.status = 'live'
            self._caption()

    def _drain(self):
        if self.stream is None:
            return
        now = time.time()
        heard = False
        noted = False
        for text in self.stream.drain():
            try:
                note = stream_note(text)
            except Exception:
                note = None
            if note == 'live':
                if self.status == 'connecting':
                    self.status = 'live'
                    noted = True
                continue
            if isinstance(note, str) and note.startswith('error:'):
                self.status = note.split(':', 1)[1] or 'rejected'
                noted = True
                self._caption()
                continue
            try:
                vessel = parse_aisstream(text)
            except Exception:
                continue
            if not vessel:
                continue
            if vessel.get('name_only'):
                remember_identity(self.identity, vessel)
                current = self.targets.get(vessel['mmsi'])
                if current:
                    remember_identity(self.identity, current)
                    self._keep_identity(current, vessel)
                    if vessel.get('name'):
                        current['name'] = vessel['name']
                continue
            remember_identity(self.identity, vessel)
            self._remember(vessel, now)
            heard = True
        if heard:
            self.last_ok = now
            self.status = ''
            self._caption()
        elif not noted and self.stream.error and not self.stream.isRunning():
            self.status = self.stream.error
            self._caption()

    def _remember(self, vessel, now):
        key = vessel['mmsi'] or '%.5f,%.5f' % (vessel['lat'], vessel['lon'])
        vessel['seen'] = now
        previous = self.targets.get(key)
        remember_identity(self.identity, vessel)
        if previous:
            if not vessel.get('name'):
                vessel['name'] = previous.get('name', '')
            self._keep_identity(vessel, previous)
            remember_identity(self.identity, vessel)
        self.targets[key] = vessel

    def _keep_identity(self, vessel, previous):
        if vessel.get('kind') in (None, '', 'other') and previous.get('kind') not in (None, '', 'other'):
            vessel['kind'] = previous['kind']
        if vessel.get('length') is None and previous.get('length') is not None:
            vessel['length'] = previous['length']
        if vessel.get('beam') is None and previous.get('beam') is not None:
            vessel['beam'] = previous['beam']

    def _stop_stream(self):
        thread = self.stream
        self.stream = None
        if thread is not None:
            thread.stop()

    def _visible(self):
        try:
            lat, lon, radius_m = self._place()
        except (TypeError, ValueError):
            return []
        bearing = self._bearing()
        now = time.time()
        limit = self._max_age() * 60
        found = []
        for vessel in self.targets.values():
            if now - vessel.get('seen', 0) > limit:
                continue
            point = project(vessel['lat'], vessel['lon'], lat, lon,
                            bearing, radius_m)
            if not in_scope(point):
                continue
            found.append((point[0] ** 2 + point[1] ** 2, point[0], point[1],
                          vessel.get('sog'), vessel.get('cog'), vessel.get('heading'),
                          vessel.get('kind') or 'other', vessel.get('length'),
                          vessel.get('beam')))
        found.sort()
        cap = int(self.config.get('max-targets') or 250)
        return [(r, f, sog, cog, heading, kind, length, beam)
                for _d, r, f, sog, cog, heading, kind, length, beam in found[:cap]]

    def _prune(self):
        if len(self.targets) < 2000:
            return
        ranked = sorted(self.targets.items(),
                        key=lambda item: item[1].get('seen', 0))
        self.targets = dict(ranked[-1000:])

    def _ring_labels(self):
        try:
            radius = float(self.config['radius'])
            unit = self.config['radius-unit']
        except (TypeError, ValueError, KeyError):
            return ('', '')
        return (format_radius(radius / 3.0, unit),
                format_radius(radius * 2.0 / 3.0, unit))

    def _place(self):
        lat = self._number('latitude')
        lon = self._number('longitude')
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError('position')
        radius_m = radius_meters(self.config['radius'],
                                 self.config['radius-unit'])
        return lat, lon, radius_m

    def _number(self, key):
        value = self.config[key]
        if isinstance(value, str):
            value = self.expand(value).strip()
        return float(value)

    def _bearing(self):
        try:
            return float(self.config.get('bearing') or 0) % 360.0
        except (TypeError, ValueError):
            return 0.0

    def _source(self):
        name = str(self.config.get('source') or 'aishub').strip().lower()
        if name in ('aisstream', 'aisstream.io', 'stream'):
            return 'aisstream'
        return 'aishub'

    def _secret(self, key):
        raw = self.config.get(key, '')
        if raw is None:
            return ''
        if not isinstance(raw, str):
            raw = str(raw)
        try:
            value = self.expand(raw).strip()
        except Exception:
            return ''
        if not value or '{' in value or '}' in value:
            return ''
        return value

    def _max_age(self):
        try:
            return max(1, min(180, int(float(self.config.get('max-age') or 10))))
        except (TypeError, ValueError):
            return 10

    def _refresh_ms(self):
        try:
            seconds = int(float(self.config.get('refresh') or 60))
        except (TypeError, ValueError):
            seconds = 60
        if self._source() == 'aishub':
            seconds = max(60, seconds)
        else:
            seconds = max(5, seconds)
        return seconds * 1000

    def _trace(self):
        color = QColor(self.color() or '#5EE08A')
        if not color.isValid() or color.lightness() < 70:
            color = QColor('#5EE08A')
        return color


def _px(font_size, height):
    text = str(font_size or '')
    if text.endswith('px'):
        try:
            return max(8, int(float(text[:-2])))
        except ValueError:
            return max(8, int(height * 0.048))
    return max(8, int(height * 0.048))


def _body(data):
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    data_fn = getattr(data, 'data', None)
    if callable(data_fn):
        raw = data_fn()
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
    return bytes(data)
