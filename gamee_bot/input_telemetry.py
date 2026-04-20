"""Synthetic user interaction telemetry for Telegram Mini App WebView emulation.

Generates realistic touch, scroll, and focus event sequences that mimic
organic human interaction patterns. Events follow mathematical curves
(cubic Bezier) for natural acceleration/deceleration, include micro-jitter
timing, and are packaged into the standard batched telemetry format.

Usage::

    gen = InputTelemetryGenerator(screen_width=393, screen_height=852)

    # Generate a complete interaction session (tap + scroll + read pauses)
    events = gen.generate_session_interaction(
        page_height=3200,
        num_taps=3,
        num_scrolls=5,
    )

    # Package for POST
    payload = gen.assemble_telemetry_payload(events)
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any


# ── Bezier curve math ──────────────────────────────────────────────────

def _cubic_bezier(
    t: float,
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> tuple[float, float]:
    """Evaluate cubic Bezier curve at parameter t ∈ [0, 1]."""
    u = 1.0 - t
    x = u*u*u*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t*t*t*p3[0]
    y = u*u*u*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t*t*t*p3[1]
    return (x, y)


def _ease_in_out(t: float) -> float:
    """Smooth ease-in-out timing function (sinusoidal)."""
    return 0.5 * (1.0 - math.cos(math.pi * t))


def _jitter(value: float, magnitude: float = 0.5) -> float:
    """Add micro-jitter to a coordinate value."""
    return value + random.gauss(0.0, magnitude)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ── Touch event types ─────────────────────────────────────────────────

TOUCH_START = "touchstart"
TOUCH_MOVE = "touchmove"
TOUCH_END = "touchend"
SCROLL_EVENT = "scroll"
POINTER_DOWN = "pointerdown"
POINTER_MOVE = "pointermove"
POINTER_UP = "pointerup"
FOCUS_EVENT = "focus"
BLUR_EVENT = "blur"
VISIBILITY_CHANGE = "visibilitychange"


@dataclass(slots=True)
class InputEvent:
    """Single telemetry event matching WebView touch/scroll format."""

    event_type: str
    timestamp: float   # ms since session start
    x: float = 0.0
    y: float = 0.0
    # Touch-specific
    pressure: float = 0.0
    radius_x: float = 0.0
    radius_y: float = 0.0
    rotation_angle: float = 0.0
    # Scroll-specific
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    # Viewport
    viewport_width: int = 393
    viewport_height: int = 852
    # Meta
    target: str = ""
    is_primary: bool = True
    pointer_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the frontend telemetry event format."""
        d: dict[str, Any] = {
            "type": self.event_type,
            "ts": round(self.timestamp, 1),
        }
        if self.event_type in (TOUCH_START, TOUCH_MOVE, TOUCH_END,
                               POINTER_DOWN, POINTER_MOVE, POINTER_UP):
            d["x"] = round(self.x, 2)
            d["y"] = round(self.y, 2)
            d["pressure"] = round(self.pressure, 4)
            d["radiusX"] = round(self.radius_x, 2)
            d["radiusY"] = round(self.radius_y, 2)
            d["rotationAngle"] = round(self.rotation_angle, 2)
            d["isPrimary"] = self.is_primary
            d["pointerId"] = self.pointer_id
        if self.event_type == SCROLL_EVENT:
            d["scrollX"] = round(self.scroll_x, 1)
            d["scrollY"] = round(self.scroll_y, 1)
        if self.target:
            d["target"] = self.target
        return d


@dataclass(slots=True)
class TelemetryBatch:
    """A batch of events ready for POST, matching the server-expected format."""

    events: list[dict[str, Any]]
    session_id: str
    timestamp_start: float
    timestamp_end: float
    viewport_width: int
    viewport_height: int
    device_pixel_ratio: float
    platform: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "sid": self.session_id,
            "events": self.events,
            "ts_start": round(self.timestamp_start, 1),
            "ts_end": round(self.timestamp_end, 1),
            "vw": self.viewport_width,
            "vh": self.viewport_height,
            "dpr": self.device_pixel_ratio,
            "platform": self.platform,
        }


class InputTelemetryGenerator:
    """Generates organic-looking input telemetry for a mobile session.

    All coordinates are in CSS pixels for a 393×852 viewport (typical
    Android phone). Timing uses milliseconds since session start.
    """

    def __init__(
        self,
        *,
        screen_width: int = 393,
        screen_height: int = 852,
        device_pixel_ratio: float = 2.75,
        session_id: str | None = None,
    ) -> None:
        self.sw = screen_width
        self.sh = screen_height
        self.dpr = device_pixel_ratio
        self.session_id = session_id or self._gen_session_id()
        self._clock_ms: float = random.uniform(200, 800)  # initial offset
        self._scroll_y: float = 0.0

    @staticmethod
    def _gen_session_id() -> str:
        import hashlib
        return hashlib.md5(
            f"{time.time()}-{random.random()}".encode()
        ).hexdigest()[:16]

    def _advance_clock(self, ms: float) -> float:
        """Advance internal clock and return the new timestamp."""
        self._clock_ms += ms
        return self._clock_ms

    def _reading_pause(self) -> float:
        """Human reading/reaction pause: 1.5–4.0s with natural distribution."""
        # Log-normal distribution for natural variance
        base = random.lognormvariate(math.log(2.5), 0.35)
        return _clamp(base, 1.5, 4.0) * 1000.0  # → ms

    def _micro_delay(self, lo_ms: float = 80, hi_ms: float = 300) -> float:
        """Micro-delay between touchstart and touchend."""
        return random.uniform(lo_ms, hi_ms)

    # ── Touch generation ───────────────────────────────────────────────

    def _touch_pressure(self) -> float:
        """Realistic finger pressure: 0.0–1.0, clustered around 0.3–0.7."""
        return _clamp(random.gauss(0.5, 0.15), 0.05, 0.95)

    def _touch_radius(self) -> tuple[float, float]:
        """Finger contact area radii (CSS px)."""
        rx = random.gauss(11.0, 2.5)
        ry = random.gauss(11.0, 2.5)
        return (_clamp(rx, 5.0, 20.0), _clamp(ry, 5.0, 20.0))

    def generate_tap(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        target: str = "",
    ) -> list[InputEvent]:
        """Generate a realistic tap: pointer down → small jitter → pointer up.

        Includes organic micro-movement between down and up (finger isn't
        perfectly still), and natural timing with 80–250ms hold duration.
        """
        if x is None:
            x = random.uniform(self.sw * 0.1, self.sw * 0.9)
        if y is None:
            y = random.uniform(self.sh * 0.15, self.sh * 0.85)

        events: list[InputEvent] = []
        pressure = self._touch_pressure()
        rx, ry = self._touch_radius()
        rotation = random.uniform(0, 15.0)

        # touchstart / pointerdown
        t_start = self._advance_clock(self._micro_delay(10, 30))
        events.append(InputEvent(
            event_type=TOUCH_START,
            timestamp=t_start,
            x=_jitter(x, 0.3), y=_jitter(y, 0.3),
            pressure=pressure,
            radius_x=rx, radius_y=ry,
            rotation_angle=rotation,
            target=target,
            viewport_width=self.sw, viewport_height=self.sh,
        ))
        events.append(InputEvent(
            event_type=POINTER_DOWN,
            timestamp=t_start + random.uniform(0.1, 0.5),
            x=events[-1].x, y=events[-1].y,
            pressure=pressure,
            radius_x=rx, radius_y=ry,
            rotation_angle=rotation,
            target=target,
            viewport_width=self.sw, viewport_height=self.sh,
        ))

        # 1–3 micro-movements during hold (finger slight drift)
        n_moves = random.randint(1, 3)
        hold_ms = self._micro_delay(80, 250)
        for i in range(n_moves):
            t_move = self._advance_clock(hold_ms / (n_moves + 1))
            drift_x = _jitter(x, 1.2)
            drift_y = _jitter(y, 1.2)
            events.append(InputEvent(
                event_type=TOUCH_MOVE,
                timestamp=t_move,
                x=drift_x, y=drift_y,
                pressure=_clamp(pressure + random.gauss(0, 0.05), 0.05, 0.95),
                radius_x=rx + random.gauss(0, 0.5),
                radius_y=ry + random.gauss(0, 0.5),
                rotation_angle=rotation + random.gauss(0, 1.0),
                target=target,
                viewport_width=self.sw, viewport_height=self.sh,
            ))

        # touchend / pointerup
        t_end = self._advance_clock(hold_ms / (n_moves + 1))
        end_x = _jitter(x, 0.8)
        end_y = _jitter(y, 0.8)
        events.append(InputEvent(
            event_type=TOUCH_END,
            timestamp=t_end,
            x=end_x, y=end_y,
            pressure=0.0,
            radius_x=rx, radius_y=ry,
            rotation_angle=rotation,
            target=target,
            viewport_width=self.sw, viewport_height=self.sh,
        ))
        events.append(InputEvent(
            event_type=POINTER_UP,
            timestamp=t_end + random.uniform(0.1, 0.5),
            x=end_x, y=end_y,
            pressure=0.0,
            radius_x=rx, radius_y=ry,
            rotation_angle=rotation,
            target=target,
            viewport_width=self.sw, viewport_height=self.sh,
        ))

        return events

    # ── Scroll generation ──────────────────────────────────────────────

    def generate_scroll(
        self,
        distance_px: float | None = None,
        *,
        direction: str = "down",
        page_height: float = 3200,
    ) -> list[InputEvent]:
        """Generate a realistic scroll gesture: Bezier-curved swipe + inertia.

        The scroll follows a cubic Bezier path for natural acceleration
        (ease-in) and deceleration (ease-out). Includes 8–20 intermediate
        touchmove events and a final scroll event.
        """
        if distance_px is None:
            distance_px = random.uniform(200, 600)

        sign = -1.0 if direction == "down" else 1.0

        # Start position: random horizontal, lower-middle for down-scroll
        start_x = random.uniform(self.sw * 0.2, self.sw * 0.8)
        start_y = random.uniform(self.sh * 0.5, self.sh * 0.75) if direction == "down" \
            else random.uniform(self.sh * 0.25, self.sh * 0.5)

        # Bezier control points for natural curve
        end_y = start_y + sign * distance_px
        # Slight horizontal drift during scroll (not perfectly vertical)
        drift_x = random.gauss(0, 8.0)
        p0 = (start_x, start_y)
        p1 = (start_x + drift_x * 0.3, start_y + sign * distance_px * 0.15)
        p2 = (start_x + drift_x * 0.7, start_y + sign * distance_px * 0.85)
        p3 = (start_x + drift_x, end_y)

        events: list[InputEvent] = []
        pressure = self._touch_pressure()
        rx, ry = self._touch_radius()

        # touchstart
        t_start = self._advance_clock(self._micro_delay(5, 15))
        events.append(InputEvent(
            event_type=TOUCH_START,
            timestamp=t_start,
            x=p0[0], y=p0[1],
            pressure=pressure,
            radius_x=rx, radius_y=ry,
            viewport_width=self.sw, viewport_height=self.sh,
        ))

        # touchmove sequence along Bezier curve with ease-in-out timing
        n_steps = random.randint(8, 20)
        total_swipe_ms = random.uniform(250, 600)
        for i in range(1, n_steps + 1):
            t_param = i / n_steps
            eased_t = _ease_in_out(t_param)
            bx, by = _cubic_bezier(eased_t, p0, p1, p2, p3)

            # Time between move events: eased (faster in middle)
            dt = total_swipe_ms / n_steps
            # Add slight timing jitter
            dt += random.gauss(0, dt * 0.15)
            t_move = self._advance_clock(max(8.0, dt))

            events.append(InputEvent(
                event_type=TOUCH_MOVE,
                timestamp=t_move,
                x=_jitter(bx, 0.8),
                y=_jitter(by, 0.8),
                pressure=_clamp(pressure + random.gauss(0, 0.03), 0.05, 0.95),
                radius_x=rx + random.gauss(0, 0.3),
                radius_y=ry + random.gauss(0, 0.3),
                viewport_width=self.sw, viewport_height=self.sh,
            ))

        # touchend
        t_end = self._advance_clock(self._micro_delay(5, 20))
        events.append(InputEvent(
            event_type=TOUCH_END,
            timestamp=t_end,
            x=_jitter(p3[0], 0.5), y=_jitter(p3[1], 0.5),
            pressure=0.0,
            radius_x=rx, radius_y=ry,
            viewport_width=self.sw, viewport_height=self.sh,
        ))

        # Scroll event (after inertia settles)
        self._scroll_y = _clamp(
            self._scroll_y + (-sign * distance_px),
            0.0,
            max(0.0, page_height - self.sh),
        )
        t_scroll = self._advance_clock(random.uniform(30, 80))
        events.append(InputEvent(
            event_type=SCROLL_EVENT,
            timestamp=t_scroll,
            scroll_x=0.0,
            scroll_y=self._scroll_y,
            viewport_width=self.sw, viewport_height=self.sh,
        ))

        return events

    # ── Focus / Visibility events ──────────────────────────────────────

    def generate_focus_events(self) -> list[InputEvent]:
        """Simulate app focus gain (user returns to tab)."""
        t = self._advance_clock(random.uniform(5, 20))
        return [
            InputEvent(event_type=FOCUS_EVENT, timestamp=t,
                       viewport_width=self.sw, viewport_height=self.sh),
            InputEvent(event_type=VISIBILITY_CHANGE, timestamp=t + 1.0,
                       viewport_width=self.sw, viewport_height=self.sh,
                       target="visible"),
        ]

    def generate_blur_events(self) -> list[InputEvent]:
        """Simulate app losing focus (user switches away briefly)."""
        t = self._advance_clock(random.uniform(5, 20))
        return [
            InputEvent(event_type=BLUR_EVENT, timestamp=t,
                       viewport_width=self.sw, viewport_height=self.sh),
            InputEvent(event_type=VISIBILITY_CHANGE, timestamp=t + 1.0,
                       viewport_width=self.sw, viewport_height=self.sh,
                       target="hidden"),
        ]

    # ── Full session interaction ───────────────────────────────────────

    def generate_session_interaction(
        self,
        *,
        page_height: float = 3200,
        num_taps: int = 3,
        num_scrolls: int = 5,
        include_focus: bool = True,
    ) -> list[InputEvent]:
        """Generate a full organic session: focus → scroll/tap mix → reading pauses.

        Actions are interleaved with natural reading pauses (1.5–4s) and
        ordered to simulate a real user exploring the Mini App.
        """
        all_events: list[InputEvent] = []

        # Initial page focus
        if include_focus:
            all_events.extend(self.generate_focus_events())
            self._advance_clock(self._reading_pause())

        # Interleave taps and scrolls with reading pauses
        actions: list[str] = []
        actions.extend(["scroll"] * num_scrolls)
        actions.extend(["tap"] * num_taps)
        random.shuffle(actions)

        # Common tap targets in Gamee Mini App
        tap_targets = [
            "button.play-btn",
            "div.reward-card",
            "a.nav-tab",
            "button.claim-btn",
            "div.game-card",
            "button.continue-btn",
            "div.leaderboard-row",
        ]

        for action in actions:
            if action == "scroll":
                dist = random.uniform(150, 500)
                direction = "down" if random.random() < 0.85 else "up"
                all_events.extend(self.generate_scroll(
                    dist, direction=direction, page_height=page_height,
                ))
            else:
                target = random.choice(tap_targets)
                # Tap in realistic UI zones (avoid edges)
                tx = random.uniform(self.sw * 0.08, self.sw * 0.92)
                ty = random.uniform(self.sh * 0.12, self.sh * 0.88)
                all_events.extend(self.generate_tap(tx, ty, target=target))

            # Reading/reaction pause between actions
            self._advance_clock(self._reading_pause())

        # Occasional brief blur/focus (user glances at notification)
        if include_focus and random.random() < 0.3:
            all_events.extend(self.generate_blur_events())
            self._advance_clock(random.uniform(1500, 4000))
            all_events.extend(self.generate_focus_events())
            self._advance_clock(self._reading_pause())

        return all_events

    # ── Payload assembly ───────────────────────────────────────────────

    def assemble_telemetry_payload(
        self,
        events: list[InputEvent],
        *,
        batch_size: int = 50,
    ) -> list[TelemetryBatch]:
        """Package events into batched payloads for periodic POST.

        Splits the event stream into batches of `batch_size` events,
        each with its own time window and metadata.
        """
        if not events:
            return []

        serialized = [e.to_dict() for e in events]
        batches: list[TelemetryBatch] = []

        for i in range(0, len(serialized), batch_size):
            chunk = serialized[i:i + batch_size]
            ts_start = chunk[0]["ts"]
            ts_end = chunk[-1]["ts"]
            batches.append(TelemetryBatch(
                events=chunk,
                session_id=self.session_id,
                timestamp_start=ts_start,
                timestamp_end=ts_end,
                viewport_width=self.sw,
                viewport_height=self.sh,
                device_pixel_ratio=self.dpr,
                platform="android",
            ))

        return batches

    def generate_and_package(
        self,
        *,
        page_height: float = 3200,
        num_taps: int = 3,
        num_scrolls: int = 5,
    ) -> list[dict[str, Any]]:
        """Convenience: generate a full session and return ready-to-POST payloads."""
        events = self.generate_session_interaction(
            page_height=page_height,
            num_taps=num_taps,
            num_scrolls=num_scrolls,
        )
        batches = self.assemble_telemetry_payload(events)
        return [b.to_payload() for b in batches]
