"""The per-item timeout policy, and the one seam ``execution.timeout`` is enforced through.

``execution.timeout`` -- ``60.0`` by default, accepted range ``0 < t <= 3600`` -- is enforced
exactly once, through pytest-timeout's own per-item timer hookspec (AAP 0.3.2, AAP 0.7.1).
Foundry starts no thread, arms no signal alarm and keeps no watchdog, because a second timer
beside the peer's would fire twice on one item and leave ``SIGALRM`` in a state neither plugin
expects. A worker that dies outright belongs to :mod:`foundry.execution.sharding`, which reports
it as a ``run_error``.

The plugin registers ``pytest_timeout_set_timer`` and ``pytest_timeout_cancel_timer``, both
``firstresult`` hookspecs, as one-line delegations to :func:`apply_timer` and
:func:`cancel_timer` while ``config.pluginmanager.hasplugin("timeout")`` holds (AAP 0.2.1,
AAP 0.4.2). Returning ``False`` lets the chain fall through to the peer's own ``trylast``
implementation, which is what a disabled policy or a Foundry fault must do. The resolved
:class:`TimeoutPolicy` is authoritative rather than the settings the peer passes into the hook,
and the plugin seeds the peer's timeout from ``execution.timeout`` during session configuration
so that a project which set no ``--timeout`` of its own still reaches the substitution.

pytest-timeout is an optional peer, so nothing here imports it until a hook runs, which keeps it
out of ``sys.modules`` for the 150 ms startup budget (implicit requirement I5); it is looked up
by name because the distribution has shipped both ``pytest_timeout`` and ``_pytest_timeout``.
A peer that cannot be imported, or that refuses the derived settings, is reported as one
``WARNING`` per process through :func:`_announce_once`, and the call returns ``False``, so
Foundry's own plumbing never changes a test's outcome or the exit code (AAP 0.1.2, AAP 0.13.1).
Importing this module performs no I/O and arms no timer.
"""

from __future__ import annotations

import functools
import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol, cast

from foundry.core.exceptions import ConfigError
from foundry.core.limits import TIMEOUT_MAX, TIMEOUT_MIN, TIMEOUT_RANGE, range_fault_message
from foundry.obs.events import emit_plumbing_failure

__all__ = [
    "DEFAULT_TIMEOUT_METHOD",
    "FUNC_ONLY",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "TIMEOUT_KEY",
    "TIMEOUT_MARKER",
    "TIMEOUT_METHODS",
    "TIMEOUT_METHOD_SIGNAL",
    "TIMEOUT_METHOD_THREAD",
    "TIMEOUT_RANGE_TEXT",
    "TimeoutPolicy",
    "apply_timer",
    "cancel_timer",
    "is_timeout_failure",
]

TIMEOUT_METHOD_SIGNAL: Final[str] = "signal"
"""pytest-timeout's ``signal`` method: an interval timer that interrupts the test in place."""

TIMEOUT_METHOD_THREAD: Final[str] = "thread"
"""pytest-timeout's ``thread`` method: a timer thread that fails the test and dumps its stacks."""

DEFAULT_TIMEOUT_METHOD: Final[str] = TIMEOUT_METHOD_SIGNAL
"""The method Foundry asks for, which is pytest-timeout's own default.

The peer downgrades ``signal`` to ``thread`` by itself off the main thread or without ``SIGALRM``,
and AAP 0.4.5 fixes no key for a method, so Foundry selects none of its own.
"""

FUNC_ONLY: Final[bool] = True
"""Whether the timer covers the test's call phase only.

``True`` makes a timeout a ``call``-phase failure, the ``execution.timeout`` row of AAP 0.4.7, and
leaves fixture setup and teardown unbounded so a slow fixture is reported by the fixture.
"""

MIN_TIMEOUT_SECONDS: Final[float] = TIMEOUT_MIN
"""Exclusive lower bound of ``execution.timeout``; a limit of zero would arm no timer at all.

Defined once, in :data:`foundry.core.limits.TIMEOUT_MIN`, because this package may not import the
configuration model that publishes the bound (AAP 0.4.8).
"""

MAX_TIMEOUT_SECONDS: Final[float] = TIMEOUT_MAX
"""Inclusive upper bound of ``execution.timeout``: one hour for a single test (AAP 0.4.5).

Defined once, in :data:`foundry.core.limits.TIMEOUT_MAX`.
"""

TIMEOUT_RANGE_TEXT: Final[str] = TIMEOUT_RANGE
"""The accepted range as the message states it: ``0 < t <= 3600``.

Quoted from :data:`foundry.core.limits.TIMEOUT_RANGE`, the one spelling of the range, and it
matches the constraint :class:`foundry.config.keys.KeySpec` declares for ``execution.timeout``, so
the same sentence appears whichever surface reports the fault.
"""

TIMEOUT_KEY: Final[str] = "execution.timeout"
"""The dotted configuration key these limits belong to, and the key a :class:`ConfigError` names."""

TIMEOUT_MARKER: Final[str] = "Timeout ("
"""The literal prefix pytest-timeout puts in its own failure message.

It reports a timed-out test as ``Timeout (>60.0s) from pytest-timeout.``, and the prefix is the
stable part of that text across the releases in the declared range. It is only evidence for
labelling an attempt; the outcome itself still comes from pytest's own report.
"""

TIMEOUT_METHODS: Final[frozenset[str]] = frozenset({TIMEOUT_METHOD_SIGNAL, TIMEOUT_METHOD_THREAD})
"""The two timer methods pytest-timeout accepts; an immutable constant, not shared state."""

_TIMEOUT_MODULE_NAMES: Final[tuple[str, ...]] = ("pytest_timeout", "_pytest_timeout")
"""The module names pytest-timeout has used, in the order this module tries to import them."""

_MISSING_PLUGIN_MESSAGE: Final[str] = (
    "pytest-timeout is not importable, so the execution.timeout timer was left to it"
)
"""Logged when the optional peer cannot be imported at all; names no path and no value."""

_REFUSED_SETTINGS_MESSAGE: Final[str] = (
    "pytest-timeout refused the derived timer settings, so its own timer remains in effect"
)
"""Logged when the installed release rejects the settings; names no path and no value."""


@functools.cache
def _announce_once(fault: str, error_name: str = "") -> None:
    """Report one distinct pytest-timeout peer fault, once for the run's process.

    Both hook bodies run once per item, so an unlatched announcement floods the log: a peer whose
    hookspec is registered while its module cannot be imported, or which refuses the derived
    settings, would write a ``WARNING`` for every item's set call and again for every item's cancel
    call. ``WARNING`` is the level AAP 0.13.1 reserves for the failure-isolation cases, and a peer
    fault is one, so the level stays and the announcement is latched instead: the memo makes this
    body run on the first call for a given fault and never again, which is the once-per-run latch
    :meth:`foundry.obs.artifacts.ArtifactWriter._warn_once` implements with an instance flag. This
    module owns no instance to hang that flag on, and a bare module-level flag would be mutable
    state of the kind AAP 0.4.8 restricts, so the memo is keyed by the fault itself.

    ``error_name`` rides the message rather than travelling as ``exc=``, because a memo key cannot
    carry a fresh exception instance and the class name is the only part of an exception ever
    logged anyway: :func:`foundry.obs.events.emit_plumbing_failure` records
    ``type(exc).__name__`` and never the message, the arguments or the traceback.

    The memo holds no configuration, no path, no node id and no outcome, and nothing reads it to
    decide behaviour -- it exists only to keep a diagnostic from repeating. A test that needs a
    fresh announcement calls ``_announce_once.cache_clear()``.

    Args:
        fault: The static message naming what the peer could not do. It carries no path, no node
            id and no value, so the same fault always forms the same memo key.
        error_name: The class name of the exception the peer raised, or an empty string when the
            fault has none, in which case the logged message is ``fault`` alone.
    """
    message = f"{fault} ({error_name})" if error_name else fault
    emit_plumbing_failure(TIMEOUT_KEY, message)


_UNRENDERABLE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    AttributeError,
    TypeError,
    ValueError,
    RecursionError,
    RuntimeError,
)
"""Faults an exception's own ``__str__`` may raise while a message is being read."""

_TIMER_API_ERRORS: Final[tuple[type[Exception], ...]] = (AttributeError, TypeError)
"""Faults an incompatible pytest-timeout release raises: a renamed field, or a missing function.

Both mean the installed peer does not have the surface this module was written against, which is a
plumbing fault to warn about and fall back from rather than a reason to fail a test.
"""


class _SettingsFactory(Protocol):
    """The call surface of pytest-timeout's ``Settings`` namedtuple class.

    A protocol rather than the real class, because the class belongs to an optionally imported
    peer and must not leak into a signature. It states the four keyword arguments
    :func:`_build_settings` passes, so a release that renames one fails at that single
    construction site and is reported as one warning instead of a traceback in a test.
    """

    def __call__(
        self,
        *,
        timeout: float,
        method: str,
        func_only: bool,
        disable_debugger_detection: bool,
    ) -> object:
        """Build one settings record from the values Foundry derived."""
        ...


class _TimeoutModule(Protocol):
    """The three names this module uses from the installed pytest-timeout release."""

    Settings: _SettingsFactory
    """The settings type the release's timer functions read."""

    def pytest_timeout_set_timer(self, item: object, settings: object) -> object:
        """Arm the timer for ``item`` with ``settings``."""
        ...

    def pytest_timeout_cancel_timer(self, item: object) -> object:
        """Cancel whatever timer was armed for ``item``."""
        ...


@dataclass(frozen=True, slots=True)
class TimeoutPolicy:
    """One resolved per-test time limit, and the pytest-timeout settings derived from it.

    Frozen and slotted: a policy is read by the plugin's hook, by the execution coordinator and by
    diagnostics, and none of them may change the limit another depends on. The invariant holds for
    every construction route, because :meth:`__post_init__` validates the limit rather than leaving
    validation to :meth:`from_config`.

    Attributes:
        seconds: The limit for one test, in seconds. Whenever the policy is enabled it lies inside
            ``0 < seconds <= 3600``; a disabled policy may carry ``MIN_TIMEOUT_SECONDS``, since it
            arms nothing.
        enabled: Whether the limit is enforced. ``False`` models ``-p no:timeout`` and a test that
            needs the timer off without editing the project's configuration.
        method: The timer method to ask pytest-timeout for. pytest-timeout falls back to
            ``thread`` when ``signal`` cannot be used on the calling thread, so this is a
            preference the peer is free to overrule.
    """

    seconds: float
    enabled: bool = True
    method: str = DEFAULT_TIMEOUT_METHOD

    def __post_init__(self) -> None:
        """Coerce the limit to a validated ``float`` and settle the method.

        A limit outside ``0 < t <= 3600`` raises :class:`ConfigError` naming ``execution.timeout``
        and the accepted range, whether the instance came from :meth:`from_config` or from a
        direct constructor call. A disabled policy carries no timer, so its limit is stored as
        given instead of being rejected.

        An unrecognised method falls back to :data:`DEFAULT_TIMEOUT_METHOD` rather than raising:
        Foundry derives the method itself and AAP 0.4.5 fixes no key for one, so a value it does
        not understand is a defect in Foundry's own wiring, which must never fail a test over a
        cosmetic setting.
        """
        if self.enabled:
            object.__setattr__(self, "seconds", _validated_seconds(self.seconds))
        if self.method not in TIMEOUT_METHODS:
            object.__setattr__(self, "method", DEFAULT_TIMEOUT_METHOD)

    @classmethod
    def from_config(cls, seconds: float) -> TimeoutPolicy:
        """Return the enabled policy for a resolved ``execution.timeout`` value.

        Args:
            seconds: The resolved limit in seconds, from whichever configuration layer won.

        Returns:
            An enabled policy enforcing exactly ``seconds``.

        Raises:
            ConfigError: When ``seconds`` is not a finite number inside
                ``0 < t <= 3600``. The message names ``execution.timeout`` and the accepted range,
                and the error carries that key as its ``key`` attribute, so the command line maps
                it to exit code 2 without parsing prose.
        """
        return cls(seconds=seconds)

    @classmethod
    def disabled(cls) -> TimeoutPolicy:
        """Return a policy that arms no timer.

        Used for ``-p no:timeout`` and for a test that needs the time limit switched off. An item
        run under this policy keeps whatever timer pytest-timeout itself would have set, because
        :func:`apply_timer` returns ``False`` and the ``firstresult`` chain falls through to the
        peer's own implementation.

        Returns:
            A disabled policy carrying ``MIN_TIMEOUT_SECONDS``.
        """
        return cls(seconds=MIN_TIMEOUT_SECONDS, enabled=False, method=DEFAULT_TIMEOUT_METHOD)

    @property
    def is_enabled(self) -> bool:
        """Whether a timer would be armed for an item under this policy.

        Returns:
            ``True`` only when the policy is enabled and carries a positive limit, which is the
            exact condition :func:`apply_timer` needs before it imports the optional peer.
        """
        return self.enabled and self.seconds > MIN_TIMEOUT_SECONDS

    def for_item(self, nodeid: str) -> float:
        """Return the limit that applies to ``nodeid``, in seconds.

        The configuration schema of AAP 0.4.5 declares no per-test override key, so every item gets
        the session limit and this method always returns :attr:`seconds`. It takes the node id
        anyway so that an additive per-item override, which AAP 0.4.9 allows a later release to
        introduce, needs no change at any call site.

        Args:
            nodeid: The pytest node id of the item being timed.

        Returns:
            The limit for that item, in seconds.
        """
        return self.seconds

    def message(self, nodeid: str) -> str:
        """Return the deterministic message naming the limit ``nodeid`` exceeded.

        AAP 0.4.7 requires the timeout to be reported in the failure message, so the text names
        the key and the limit:
        ``foundry: test exceeded execution.timeout of 60.0s``. It is identical for every item and
        carries no node id, because a parameterised node id can be built from data values and a
        failure message reaches the terminal, the JSON records and the HTML report.

        Args:
            nodeid: The node id of the item that timed out. It is deliberately not interpolated.

        Returns:
            The message, without a trailing newline.
        """
        return f"foundry: test exceeded {TIMEOUT_KEY} of {self.seconds}s"

    def settings(self) -> Mapping[str, object]:
        """Return the plain settings mapping the timer is derived from.

        Exposed so diagnostics and tests can read the derived values without importing the
        optional peer:
        ``{"timeout": 60.0, "method": "signal", "func_only": True}``. A fresh mapping is returned on
        every call, so a caller cannot alter the policy through it.

        Returns:
            The timeout, method and ``func_only`` flag :func:`apply_timer` passes to pytest-timeout.
        """
        return {"timeout": self.seconds, "method": self.method, "func_only": FUNC_ONLY}


def apply_timer(config: object, item: object, policy: TimeoutPolicy) -> bool:
    """Arm pytest-timeout's per-item timer for ``item`` from ``policy``.

    The body the plugin hook ``pytest_timeout_set_timer`` delegates to, called once per timed item,
    and it must never raise: Foundry's own plumbing cannot change a test's outcome or the run's
    exit code (AAP 0.1.2). A disabled policy returns ``False`` before the optional peer is looked
    up; otherwise its own ``pytest_timeout_set_timer`` is called directly with the settings built at
    :func:`_build_settings` -- never through ``config.pluginmanager.hook``, which would re-enter
    Foundry's implementation of the same ``firstresult`` hookspec and recurse. ``True`` means the
    timer is armed and ``False`` that nothing was armed, which lets the chain fall through to
    pytest-timeout's own implementation and leaves the project's timer as the peer set it.
    """
    if not policy.is_enabled:
        return False
    module = _load_timeout_module()
    if module is None:
        _announce_once(_MISSING_PLUGIN_MESSAGE)
        return False
    try:
        settings = _build_settings(module.Settings, policy, _configured_method(config))
        module.pytest_timeout_set_timer(item, settings)
    except _TIMER_API_ERRORS as error:
        _announce_once(_REFUSED_SETTINGS_MESSAGE, type(error).__name__)
        return False
    return True


def cancel_timer(item: object) -> bool:
    """Cancel the timer armed for ``item``.

    The counterpart of :func:`apply_timer`, used when the execution coordinator discards an attempt
    before starting the next one and by the plugin's ``pytest_timeout_cancel_timer`` delegation at
    teardown. pytest-timeout keeps the cancel callable on the item, so a cancel for an item that
    was never armed is a no-op in the peer rather than an error here.

    Args:
        item: The pytest ``Item`` whose timer is being cancelled.

    Returns:
        ``True`` when the peer cancelled the timer, ``False`` when the peer is not importable or
        refused the call. Nothing is raised either way: a cancellation fault is announced once for
        the run's process, as :func:`apply_timer`'s is, and the test's own outcome stands.
    """
    module = _load_timeout_module()
    if module is None:
        _announce_once(_MISSING_PLUGIN_MESSAGE)
        return False
    try:
        module.pytest_timeout_cancel_timer(item)
    except _TIMER_API_ERRORS as error:
        _announce_once(_REFUSED_SETTINGS_MESSAGE, type(error).__name__)
        return False
    return True


def is_timeout_failure(excinfo: BaseException | None) -> bool:
    """Return whether ``excinfo`` is pytest-timeout's own timeout failure.

    :mod:`foundry.execution.coordinator` and :mod:`foundry.execution.session` use this to label a
    failed attempt as a timeout and to record its phase as ``call``, which is the state the
    ``execution.timeout`` row of AAP 0.4.7 describes. The check is advisory: the outcome of a test
    always comes from pytest's own report, so a missed or extra match changes a label, never a
    verdict or an exit code.

    Evidence is the :data:`TIMEOUT_MARKER` prefix in the rendered message plus, when pytest is
    importable, that the exception really is pytest's ``Failed`` outcome exception. Anything it
    cannot recognise -- ``None``, a non-exception, an exception whose ``__str__`` raises -- yields
    ``False`` rather than an error.

    Args:
        excinfo: The exception to inspect. A pytest ``ExceptionInfo`` is accepted too, and its
            ``value`` is unwrapped.

    Returns:
        ``True`` when the exception looks like a pytest-timeout failure, otherwise ``False``.
    """
    error = _as_error(excinfo)
    if error is None:
        return False
    if TIMEOUT_MARKER not in _error_text(error):
        return False
    return _is_pytest_failure(error)


def _validated_seconds(seconds: object) -> float:
    """Return ``seconds`` as a finite ``float`` inside the accepted range, or raise.

    A value that is not a number at all raises the same :class:`ConfigError` as an out-of-range
    one, so the module keeps the exception boundary its contract states: ``ConfigError`` is the
    only type this module constructs, and a caller never sees a bare ``TypeError`` from a
    comparison. ``float`` also rejects ``nan`` and ``inf`` through the range comparison itself,
    since both fail ``MIN_TIMEOUT_SECONDS < value <= MAX_TIMEOUT_SECONDS``.

    Args:
        seconds: The candidate limit, as it arrived from configuration or from a caller.

    Returns:
        The limit as a ``float``.

    Raises:
        ConfigError: When the value is not a finite number inside ``0 < t <= 3600``.
    """
    try:
        # ``seconds`` arrives as ``object`` so a caller-supplied non-number becomes a
        # ConfigError rather than a bare comparison failure; mypy sees the narrow type only.
        value = float(seconds)  # type: ignore[arg-type]  # runtime coercion is deliberate
    except (TypeError, ValueError) as error:
        raise _range_error(seconds) from error
    if not MIN_TIMEOUT_SECONDS < value <= MAX_TIMEOUT_SECONDS:
        raise _range_error(seconds)
    return value


def _range_error(seconds: object) -> ConfigError:
    """Return the :class:`ConfigError` that reports an unusable ``execution.timeout`` value.

    The message is :func:`foundry.core.limits.range_fault_message`'s one wording, which names the
    key and the accepted range -- what the behaviour contract pins for an out-of-range value
    (``http.timeout = -1`` reports ``http.timeout`` and ``0 < t <= 300``); the offending value is
    echoed because it came from the project's own configuration and a timeout is never a secret.
    The hint repeats the range as the next step to take, and the key travels as the error's
    ``key`` attribute so the command line maps the fault to exit code 2 without parsing prose.
    """
    message = range_fault_message(TIMEOUT_KEY, seconds, TIMEOUT_RANGE_TEXT)
    hint = f"set {TIMEOUT_KEY} to a value in {TIMEOUT_RANGE_TEXT} seconds"
    return ConfigError(message, key=TIMEOUT_KEY, hint=hint)


def _build_settings(factory: _SettingsFactory, policy: TimeoutPolicy, method: str | None) -> object:
    """Build the pytest-timeout ``Settings`` record for one item.

    The single construction site for the peer's settings type, which is the point: a release that
    renames ``timeout``, ``method``, ``func_only`` or ``disable_debugger_detection`` raises
    ``TypeError`` here, and :func:`apply_timer` turns that one ``TypeError`` into one warning and a
    fallback instead of a traceback inside a test.

    ``func_only`` comes from :data:`FUNC_ONLY`, so the timer covers the call phase -- the phase the
    state table reports a timeout against -- and leaves fixture setup and teardown alone. Debugger
    detection is left enabled, which is the peer's safe default: a debugger that stops the test
    must not be killed by the timer.

    Args:
        factory: The installed release's ``Settings`` class.
        policy: The policy supplying the limit and the default method.
        method: The method the project configured on pytest-timeout, or ``None`` to use the
            policy's own.

    Returns:
        The settings record pytest-timeout's timer functions read.
    """
    return factory(
        timeout=policy.seconds,
        method=method or policy.method,
        func_only=FUNC_ONLY,
        disable_debugger_detection=False,
    )


def _load_timeout_module() -> _TimeoutModule | None:
    """Return the installed pytest-timeout module, or ``None`` when none can be imported.

    Called from inside the hook bodies only, never at import time, which is what keeps the optional
    peer out of ``sys.modules`` until a timer is actually armed. ``ImportError`` is the only fault
    caught: a peer that is installed but broken raises through, because that is the host project's
    dependency problem to see rather than something Foundry should hide.
    """
    for name in _TIMEOUT_MODULE_NAMES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        return cast("_TimeoutModule", module)
    return None


def _configured_method(config: object) -> str | None:
    """Return the timer method the project configured on pytest-timeout, if it chose one.

    pytest-timeout exposes ``--timeout-method`` and the ``timeout_method`` ini key, and a project
    that set either expects that choice to hold. Foundry substitutes the timer strategy, not the
    method, so an explicit ``thread`` survives. Nothing set yields ``None``, which leaves the
    policy's default in place -- which is also the answer for a project that never mentions
    pytest-timeout's options.
    """
    value = _config_value(config, "getoption", "timeout_method")
    if value is None:
        value = _config_value(config, "getini", "timeout_method")
    return value if isinstance(value, str) and value in TIMEOUT_METHODS else None


def _config_value(config: object, accessor: str, name: str) -> object | None:
    """Read ``name`` from ``config`` through ``accessor``, and ``None`` when it is not readable.

    ``getoption`` raises ``ValueError`` for an option nobody registered and ``getini`` for an ini
    name that is not declared, so a pytest-timeout that renamed its option -- or an item whose
    config is not a pytest ``Config`` at all -- yields ``None`` and the default method instead of
    an error.
    """
    reader = getattr(config, accessor, None)
    if not callable(reader):
        return None
    try:
        return cast("object | None", reader(name))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _as_error(excinfo: object) -> BaseException | None:
    """Return the exception ``excinfo`` carries, unwrapping a pytest ``ExceptionInfo``.

    A caller may hold either the exception or the info object that wraps it, and both mean "this
    attempt failed with this". Anything else yields ``None``, so the timeout check stays total.
    """
    if isinstance(excinfo, BaseException):
        return excinfo
    inner = getattr(excinfo, "value", None)
    return inner if isinstance(inner, BaseException) else None


def _error_text(error: BaseException) -> str:
    """Return the rendered message of ``error``, and an empty string when it cannot be rendered."""
    try:
        return str(error)
    except _UNRENDERABLE_ERRORS:
        # Failure-isolation rule: an exception that cannot be rendered simply carries no evidence,
        # because the caller is only labelling an attempt that already failed.
        return ""


def _is_pytest_failure(error: BaseException) -> bool:
    """Return whether ``error`` is the outcome exception ``pytest.fail`` raises.

    The class compared against is pytest's public ``pytest.fail.Exception``, so no private pytest
    namespace is imported. pytest is imported here rather than at module scope, and only on the
    path that already has a matching message (AAP 0.13.1). When pytest cannot be imported at all
    -- this module used from a script -- the marker alone decides, because outside a pytest run
    there is no pytest failure type to compare against.
    """
    try:
        from pytest import fail
    except ImportError:
        return True
    return isinstance(error, fail.Exception)
