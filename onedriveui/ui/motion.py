"""Fluent motion: explicit Bézier curves, gated durations, safe loops.

Three rules, all of them consequences of something verified on this machine:

  * **Every duration goes through :func:`DUR`.** Both `gtk-enable-animations`
    and `org.gnome.desktop.interface enable-animations` are `false` here, so a
    duration of 0 is the *normal* answer, not an edge case. Shipping a UI that
    animates when the user has asked for no animation is an accessibility bug.
  * **A 0 ms animation must still land on its end value.** `QPropertyAnimation`
    does apply the end value when it is started with duration 0 — but only once
    it is started, and callers routinely build an animation and forget. Every
    helper here writes the end value directly first, so the final state is
    reached whether or not there is an event loop to run in.
  * **A looping animation must never run at 0 ms and must never run hidden.**
    `setLoopCount(-1)` with `duration == 0` spins the animation timer with
    nothing to show; hidden, it burns CPU repainting a widget nobody can see.
    :class:`SafeLoop` refuses both.

The curves are `theme.CURVES` built as explicit `BezierSpline` segments.
`QEasingCurve.OutCubic` is a near miss for Fluent's standard `KeySpline 0,0,0,1`
(0.5 -> 0.875 against 0.8899) and is not used.
"""

from __future__ import annotations

import inspect
import weakref
from typing import Callable, Iterable

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QEvent, QObject, QParallelAnimationGroup,
    QPoint, QPropertyAnimation, QVariantAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from onedriveui.ui import theme
from onedriveui.ui.theme import DURATION, SPACING

#: The frozen duration names, re-exported so a call site never writes a number.
DUR_NAMES: tuple[str, ...] = tuple(DURATION)

#: The frozen curve names.
CURVE_NAMES: tuple[str, ...] = tuple(theme.CURVES)

#: Entrances decelerate, exits accelerate — Fluent's own rule.
CURVE_IN = "decelerate"
CURVE_OUT = "accelerate"

#: `QAbstractAnimation.start()` keeps the object alive; the helpers parent every
#: animation so Python cannot collect it mid-flight.
_KEEP = QAbstractAnimation.DeletionPolicy.KeepWhenStopped


def DUR(name_or_ms: str | int) -> int:
    """The duration in ms for a name or a literal, 0 when animation is off.

    The single gate. `theme.duration()` reads the desktop's animation
    preference (cached), so this is cheap enough to call per animation.

    Args:
        name_or_ms: A key of `theme.DURATION` ("faster", "fast", "normal",
            "slow", "flyout") or a literal millisecond count.

    Raises:
        KeyError: for an unknown duration name.
    """
    return theme.duration(name_or_ms)


def reduced_motion() -> bool:
    """True when the desktop has asked for no animation."""
    return not theme.animations_enabled()


def curve(name: str = CURVE_IN) -> QEasingCurve:
    """A Fluent easing curve as an explicit cubic Bézier.

    Raises:
        KeyError: for an unknown curve name.
    """
    return theme.curve(name)


def _prop_name(prop: str | bytes) -> tuple[bytes, str]:
    """-> (bytes for QPropertyAnimation, str for setProperty)."""
    if isinstance(prop, bytes):
        return prop, prop.decode("ascii")
    return prop.encode("ascii"), prop


def animate(target: QObject,
            prop: str | bytes,
            end: object,
            *,
            start: object | None = None,
            duration: str | int = "fast",
            easing: str = CURVE_IN,
            parent: QObject | None = None,
            on_finished: Callable[[], None] | None = None) -> QPropertyAnimation:
    """Animate one Qt property and return the running animation.

    The end value is written to the target **before** the animation starts when
    the duration gates to 0, so the final state is correct even with no event
    loop — that is what makes "animations disabled" a rendering setting rather
    than a behaviour change.

    Args:
        target: Any QObject exposing `prop` as a `QtCore.Property`.
        prop: The property name. It must match the `Property` attribute
            **exactly** — `QPropertyAnimation` on a name that does not resolve
            silently does nothing at all.
        end: The value to finish on.
        start: The value to begin from; the property's current value if omitted.
        duration: A `theme.DURATION` key or a literal ms count.
        easing: A `theme.CURVES` key.
        parent: Owner for the animation object. Defaults to `target`, which
            keeps it alive for the flight.
        on_finished: Connected to `finished` before the animation starts.

    Returns:
        The started `QPropertyAnimation`. Its `duration()` is 0 and its
        `endValue()` is `end` when animation is disabled.

    Raises:
        ValueError: if `target` does not actually expose `prop`.
    """
    raw, name = _prop_name(prop)
    if target.metaObject().indexOfProperty(name) < 0:
        raise ValueError(
            f"motion.animate: {type(target).__name__} has no Qt property {name!r}; "
            "QPropertyAnimation would silently do nothing"
        )
    ms = DUR(duration)
    anim = QPropertyAnimation(target, raw, parent if parent is not None else target)
    anim.setDuration(ms)
    if start is not None:
        anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(curve(easing))
    if on_finished is not None:
        anim.finished.connect(on_finished)
    if ms == 0:
        target.setProperty(name, end)
    anim.start(_KEEP)
    return anim


def stop(*animations: QAbstractAnimation | None) -> None:
    """Stop every animation given, ignoring the ones that are None."""
    for anim in animations:
        if anim is not None:
            anim.stop()


# ═════════════════════════════════════════════════════════════════════════════
# Composites
# ═════════════════════════════════════════════════════════════════════════════

def _opacity_effect(widget: QWidget) -> QGraphicsOpacityEffect:
    existing = widget.graphicsEffect()
    if isinstance(existing, QGraphicsOpacityEffect):
        return existing
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    return effect


def _detach_effect(widget: QWidget) -> None:
    """Drop the opacity effect once the fade is over.

    `QGraphicsEffect` forces the widget through an offscreen raster buffer on
    every repaint and is exclusive — one effect per widget, so a lingering
    opacity effect also blocks the drop shadow a container may want.
    """
    if isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect):
        widget.setGraphicsEffect(None)


def fade_in(widget: QWidget,
            *,
            duration: str | int = "fast",
            easing: str = CURVE_IN,
            on_finished: Callable[[], None] | None = None) -> QPropertyAnimation:
    """Fade `widget` from transparent to opaque, then detach the effect."""
    effect = _opacity_effect(widget)
    effect.setOpacity(0.0)
    widget.show()

    def _done() -> None:
        _detach_effect(widget)
        if on_finished is not None:
            on_finished()

    return animate(effect, b"opacity", 1.0, start=0.0, duration=duration,
                   easing=easing, parent=widget, on_finished=_done)


def fade_out(widget: QWidget,
             *,
             duration: str | int = "fast",
             easing: str = CURVE_OUT,
             hide_on_finish: bool = True,
             on_finished: Callable[[], None] | None = None) -> QPropertyAnimation:
    """Fade `widget` to transparent, optionally hiding it, then detach."""
    effect = _opacity_effect(widget)
    effect.setOpacity(1.0)

    def _done() -> None:
        if hide_on_finish:
            widget.hide()
        _detach_effect(widget)
        if on_finished is not None:
            on_finished()

    return animate(effect, b"opacity", 0.0, start=1.0, duration=duration,
                   easing=easing, parent=widget, on_finished=_done)


def rise_in(widget: QWidget,
            *,
            distance: int | None = None,
            duration: str | int = "flyout",
            easing: str = CURVE_IN,
            on_finished: Callable[[], None] | None = None) -> QParallelAnimationGroup:
    """The Fluent entrance: a fade plus a short rise into place.

    Args:
        widget: A CHILD widget. Animating a top-level window's `pos` does
            nothing on Wayland — the compositor owns the position and
            `QWidget.pos()` reports the request, not reality — so a window is
            refused rather than silently not moving.
        distance: How far below its final position to start. Defaults to the
            16 px `l` spacing step.

    Raises:
        ValueError: if `widget` is a top-level window.
    """
    if widget.isWindow():
        raise ValueError(
            "motion.rise_in: a top-level window's pos cannot be animated on "
            "Wayland; animate a child widget or the window's size instead"
        )
    rise = SPACING["l"] if distance is None else int(distance)
    final = widget.pos()
    begin = QPoint(final.x(), final.y() + rise)

    group = QParallelAnimationGroup(widget)
    effect = _opacity_effect(widget)
    effect.setOpacity(0.0)

    ms = DUR(duration)
    easing_curve = curve(easing)

    opacity = QPropertyAnimation(effect, b"opacity", group)
    opacity.setDuration(ms)
    opacity.setStartValue(0.0)
    opacity.setEndValue(1.0)
    opacity.setEasingCurve(easing_curve)

    move = QPropertyAnimation(widget, b"pos", group)
    move.setDuration(ms)
    move.setStartValue(begin)
    move.setEndValue(final)
    move.setEasingCurve(easing_curve)

    group.addAnimation(opacity)
    group.addAnimation(move)

    def _done() -> None:
        _detach_effect(widget)
        if on_finished is not None:
            on_finished()

    group.finished.connect(_done)
    if ms == 0:
        effect.setOpacity(1.0)
        widget.move(final)
    widget.show()
    group.start(_KEEP)
    return group


# ═════════════════════════════════════════════════════════════════════════════
# SafeLoop
# ═════════════════════════════════════════════════════════════════════════════

class SafeLoop(QObject):
    """A looping animation that cannot outlive its widget's visibility.

    `setLoopCount(-1)` repaints forever: behind a hidden widget it keeps the CPU
    awake, and at a gated duration of 0 ms it spins the animation timer with
    nothing to show. This wrapper refuses both — it watches the widget for
    Show/Hide and never starts when animation is disabled, applying a single
    representative frame once instead so the static picture is still correct.
    That frame is `static`, which defaults to `end` but must be given for any
    cyclic phase, whose end frame is by definition its empty first frame.

    Attributes:
        animation: The underlying `QVariantAnimation`. Read it in tests; do not
            start it directly, or the guards are bypassed.
    """

    def __init__(self,
                 widget: QWidget,
                 setter: Callable[[float], None],
                 *,
                 start: float = 0.0,
                 end: float = 1.0,
                 duration: str | int = "normal",
                 easing: str | None = None,
                 static: float | None = None,
                 parent: QObject | None = None) -> None:
        """
        Args:
            widget: The widget whose visibility gates the loop.
            setter: Called with each interpolated value. Must call `update()`,
                never `repaint()` — `update()` coalesces, `repaint()` is
                synchronous and will not keep up.
            start: First value of each cycle.
            end: Last value of each cycle.
            duration: One cycle, as a `theme.DURATION` key or literal ms.
            easing: A `theme.CURVES` key, or None for linear (the right answer
                for a rotation: any easing makes a spinner visibly lurch).
            static: The single value to land on when animation is disabled.
                Defaults to `end`, which is right for a one-shot but WRONG for a
                cyclic phase: a loop's last frame is its first frame, and for a
                spinner that is the emptiest one there is. A `ProgressRing` at
                phase 1.0 is the 30 degree minimum stub; an indeterminate
                `FluentProgressBar` at phase 1.0 has travelled its segment
                entirely off the end and paints NOTHING. Both pass 0.5.
        """
        super().__init__(parent if parent is not None else widget)
        # EVERY reference back to the widget is weak. A strong one would close
        # the loop widget -> SafeLoop -> widget, and a cycle can only be broken
        # by the cycle collector — which runs at an arbitrary moment, including
        # inside `QApplication.setStyleSheet()`'s repolish walk, where tearing a
        # QWidget down is a use-after-free. Weak refs let the widget die by
        # refcount, deterministically, outside any Qt call.
        self._widget_ref = weakref.ref(widget)
        self._setter_ref: weakref.WeakMethod | None = None
        self._setter: Callable[[float], None] | None = setter
        if inspect.ismethod(setter):
            self._setter_ref = weakref.WeakMethod(setter)
            self._setter = None
        self._start = float(start)
        self._end = float(end)
        self._static = self._end if static is None else float(static)
        self._wanted = False

        self.animation = QVariantAnimation(self)
        self.animation.setDuration(DUR(duration))
        self.animation.setStartValue(self._start)
        self.animation.setEndValue(self._end)
        self.animation.setLoopCount(-1)
        self.animation.setEasingCurve(
            curve(easing) if easing else QEasingCurve(QEasingCurve.Type.Linear)
        )
        self.animation.valueChanged.connect(self._on_value)
        widget.installEventFilter(self)

    @property
    def _widget(self) -> QWidget | None:
        """The gated widget, or None once it has been destroyed."""
        return self._widget_ref()

    # ── control ──────────────────────────────────────────────────────────
    def _resolve_setter(self) -> Callable[[float], None] | None:
        """-> the setter, or None once its owner has been collected."""
        if self._setter_ref is not None:
            return self._setter_ref()
        return self._setter

    def start(self) -> None:
        """Ask the loop to run. It only actually runs when it safely can."""
        self._wanted = True
        setter = self._resolve_setter()
        widget = self._widget
        if setter is None or widget is None:
            return
        if self.animation.duration() <= 0:
            # Animation is disabled: land on the representative frame once and
            # spin never. NOT the end value — see `static` in __init__.
            setter(self._static)
            return
        if not widget.isVisible():
            return
        if self.animation.state() != QAbstractAnimation.State.Running:
            self.animation.start(_KEEP)

    def stop(self) -> None:
        """Stop the loop and forget that it was wanted."""
        self._wanted = False
        self.animation.stop()

    def suspend(self) -> None:
        """Stop the loop but remember it was wanted, so a re-show resumes it."""
        self.animation.stop()

    def static_value(self) -> float:
        """The single frame this loop lands on when animation is disabled."""
        return self._static

    def is_running(self) -> bool:
        return self.animation.state() == QAbstractAnimation.State.Running

    def wanted(self) -> bool:
        return self._wanted

    # ── plumbing ─────────────────────────────────────────────────────────
    def _on_value(self, value: object) -> None:
        setter = self._resolve_setter()
        if setter is None:                # pragma: no cover - owner collected
            self.animation.stop()
            return
        try:
            setter(float(value))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Stop on Hide, resume on Show. Never swallows the event."""
        if watched is self._widget:
            kind = event.type()
            if kind in (QEvent.Type.Hide, QEvent.Type.Close):
                self.suspend()
            elif kind == QEvent.Type.Show and self._wanted:
                self.start()
        return False


def stop_all(animations: Iterable[QAbstractAnimation | SafeLoop | None]) -> None:
    """Stop a mixed bag of animations and loops. Used by `hideEvent`s."""
    for anim in animations:
        if anim is not None:
            anim.stop()


__all__ = [
    "DUR", "DUR_NAMES", "CURVE_NAMES", "CURVE_IN", "CURVE_OUT",
    "reduced_motion", "curve", "animate", "stop", "stop_all",
    "fade_in", "fade_out", "rise_in", "SafeLoop",
]
