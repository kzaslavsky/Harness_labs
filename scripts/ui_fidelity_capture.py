#!/usr/bin/env python3
"""CC-03 UI fidelity capture: the first convergence-campaign measurer.

Walks a declared route x viewport x theme x interaction matrix against a
static fixture app and records, per cell, a screenshot, DOM snapshot,
computed styles for a declared selector list, an ARIA snapshot, a console
log, and a network log (measurer-requirements). Coverage status per cell is
one of ``ok``, ``unreachable``, or ``unstable`` (``CAPTURE_CELL_STATUSES`` in
``harness_labs.core.convergence_contract``); a cell is ``unstable`` when its
two end-state DOM/computed-style digest reads disagree (contracts-verdicts).

Exit contract (AC-CC03-2): exit 0 whenever capture ran at all, regardless of
per-cell status; exit nonzero only when the browser driver could not launch,
or when the configured ``pre_journal_sanitizer`` hook rejected an artifact.

The browser interpreter is resolved from ``--python`` (default
``sys.executable``); whether the run used a real browser or the built-in
stub driver is recorded in the receipt, never inferred, together with a skip
reason when no real browser was available. No Playwright or other
real-browser package is a hard dependency of this module: the import is
attempted lazily, only inside the real driver, only when requested or
auto-detected as available under the resolved interpreter.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import html.parser
import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from harness_labs.core.audit import AuditActor, AuditJournal
from harness_labs.core.controller_evidence import EvidenceCatalog
from harness_labs.core.convergence_contract import CAPTURE_CELL_STATUSES
from harness_labs.core.verification_images import capture_failure_images

RECEIPT_SCHEMA = "ui-fidelity-capture-receipt/1"

EXIT_OK = 0
EXIT_LAUNCH_FAILURE = 2
EXIT_SANITIZER_FAILURE = 3

ARTIFACT_KINDS = (
    "screenshot",
    "dom_snapshot",
    "computed_styles",
    "aria_snapshot",
    "console_log",
    "network_log",
)

_MEDIA_TYPES = {
    "screenshot": "image/png",
    "dom_snapshot": "text/html",
    "computed_styles": "application/json",
    "aria_snapshot": "application/json",
    "console_log": "application/json",
    "network_log": "application/json",
}

# A fixed 1x1 transparent PNG. The stub driver never renders anything, so
# every stub screenshot is honestly this placeholder rather than a
# fabricated image; content-addressing means every stub cell's screenshot
# artifact naturally collapses to one stored record.
_PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x60\x00"
    b"\x00\x00\x02\x00\x01U\xa2N\x2e\x00\x00\x00\x00IEND\xaeB`\x82"
)


class CaptureError(RuntimeError):
    """Raised for a failure that aborts the whole capture run."""


class BrowserLaunchError(CaptureError):
    """The requested (or auto-selected) browser driver could not launch."""


class SanitizerError(CaptureError):
    """The configured ``pre_journal_sanitizer`` hook rejected an artifact."""


class RouteUnreachable(CaptureError):
    """One cell's route could not be opened; the cell (not the run) fails."""


# ---------------------------------------------------------------------------
# Sanitizer hook resolution and application (AC-CC03-4)
# ---------------------------------------------------------------------------


def _identity_sanitizer(kind: str, content: bytes) -> bytes:
    return content


def _load_sanitizer_module(module_ref: str) -> Any:
    """Load the ``--sanitizer`` module.

    Every failure here is a failure of the ``pre_journal_sanitizer`` hook
    itself (a bad spec, a broken module) and must raise :class:`SanitizerError`
    -- not the base :class:`CaptureError` -- so it lands on the one exit path
    ``main`` promises for a sanitizer failure (AC-CC03-2) instead of escaping
    as an uncaught traceback outside the exit contract.
    """

    path = Path(module_ref)
    if path.suffix == ".py" and path.is_file():
        spec = importlib.util.spec_from_file_location(
            f"_ui_fidelity_sanitizer_{path.stem}", path
        )
        if spec is None or spec.loader is None:
            raise SanitizerError(f"could not load sanitizer module from {module_ref!r}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - a broken sanitizer module is a hook failure
            raise SanitizerError(
                f"sanitizer module {module_ref!r} failed to load: {exc}"
            ) from exc
        return module
    try:
        return importlib.import_module(module_ref)
    except Exception as exc:  # noqa: BLE001 - not just ImportError: a module's own
        # top-level code can raise anything during import, and that is still a
        # failure of the sanitizer hook itself, not an uncaught traceback
        # outside the exit contract (AC-CC03-2).
        raise SanitizerError(
            f"could not import sanitizer module {module_ref!r}: {exc}"
        ) from exc


def _resolve_spec_sanitizer(spec: str) -> Callable[[str, bytes], bytes]:
    """Resolve a ``<module-or-path>:<callable>`` sanitizer spec to the
    callable it names -- shared by the legacy ``--sanitizer`` path and the
    ``text`` entry of a ``--sanitizer-policy`` mapping (``dtr-sn``)."""

    module_ref, sep, attr = spec.rpartition(":")
    if not sep or not module_ref or not attr:
        raise SanitizerError(
            f"invalid sanitizer spec: {spec!r} (want module_or_path:callable)"
        )
    module = _load_sanitizer_module(module_ref)
    try:
        func = getattr(module, attr)
    except AttributeError as exc:
        raise SanitizerError(f"sanitizer {spec!r} has no attribute {attr!r}") from exc
    if not callable(func):
        raise SanitizerError(f"sanitizer {spec!r} is not callable")
    return func


# Artifact kinds whose raw content is binary (media type outside the
# ``text/*``/``application/json`` families) -- the policy mapping's
# ``binary`` verbs apply to these; every other kind is a "text" kind and
# passes through the policy's single ``text`` hook (``dtr-sn``).
_BINARY_ARTIFACT_KINDS = frozenset(
    kind
    for kind, media_type in _MEDIA_TYPES.items()
    if not (media_type.startswith("text/") or media_type == "application/json")
)


def _resolve_binary_policy_verb(
    kind: str, verb: str, content: bytes, text_hook: Callable[[str, bytes], bytes]
) -> bytes:
    """Apply one binary artifact kind's declared policy verb.

    ``scan`` routes the content through the policy's declared ``text`` hook
    (which already takes ``(kind, bytes)``) -- an observable, transforming
    step, not a silent pass-through -- so a scanned artifact is distinguishable
    from an ``admit:<reason>`` one, which alone admits the content unchanged
    as the explicit bypass; no product-specific scanning logic is embedded
    here (``dtr-sn``'s "mechanism only" constraint: the hook itself is
    product config). ``reject`` refuses it, naming the refusing rule so
    ``--dry-run`` reports are actionable.
    """

    if verb == "scan":
        return text_hook(kind, content)
    if verb.startswith("admit:") and len(verb) > len("admit:"):
        return content
    if verb == "reject":
        raise SanitizerError(
            f"sanitizer policy rule 'binary.{kind}=reject' refused the {kind!r} artifact"
        )
    raise SanitizerError(
        f"sanitizer policy binary verb for {kind!r} must be 'scan', "
        f"'admit:<reason>', or 'reject', got {verb!r}"
    )


def _resolve_policy_sanitizer(
    policy: Mapping[str, Any],
) -> Callable[[str, bytes], bytes]:
    """Resolve the ``{"text": <hook-ref>, "binary": {"<kind>": <verb>}}``
    mapping form (``--sanitizer-policy``) into a per-kind dispatching
    callable.

    A missing or empty ``text`` entry fails closed with
    :class:`SanitizerError` rather than a later ``AttributeError`` (mirrors
    the driver-side ``resolve_pre_journal_sanitizer`` requirement,
    AC-SN-4's sibling on the capture surface). An undeclared binary kind
    also fails closed (AC-SN-2). A ``binary`` entry naming a kind that is
    not one of :data:`_BINARY_ARTIFACT_KINDS` (including any kind not in
    ``ARTIFACT_KINDS`` at all) is refused here, at policy-resolution time --
    otherwise it would be silently ignored, since dispatch only ever
    consults ``binary_policy`` for a kind it already knows is binary, and a
    declared refusal for a text kind would admit that artifact with no
    diagnostic.
    """

    if not isinstance(policy, Mapping):
        raise SanitizerError(
            f"--sanitizer-policy must hold a JSON object, got {type(policy).__name__}"
        )
    text_spec = policy.get("text")
    if not isinstance(text_spec, str) or not text_spec.strip():
        raise SanitizerError(
            "--sanitizer-policy must carry a non-empty 'text' hook reference"
        )
    text_hook = _resolve_spec_sanitizer(text_spec)
    binary_policy = policy.get("binary", {})
    if not isinstance(binary_policy, Mapping):
        raise SanitizerError("--sanitizer-policy 'binary' entry must be a JSON object")
    for kind, verb in binary_policy.items():
        if kind not in _BINARY_ARTIFACT_KINDS:
            raise SanitizerError(
                f"--sanitizer-policy 'binary' entry names {kind!r}, which is not a "
                f"binary artifact kind ({sorted(_BINARY_ARTIFACT_KINDS)!r}); text-kind "
                "artifacts are governed by the policy's 'text' hook, not 'binary'"
            )
        if not isinstance(verb, str) or not verb.strip():
            raise SanitizerError(
                f"--sanitizer-policy binary policy for {kind!r} must be a non-empty string"
            )

    def _dispatch(kind: str, content: bytes) -> bytes:
        if kind in _BINARY_ARTIFACT_KINDS:
            verb = binary_policy.get(kind)
            if verb is None:
                raise SanitizerError(
                    f"sanitizer policy rule 'binary.{kind}=<undeclared>' refused the "
                    f"{kind!r} artifact (undeclared binary kinds fail closed)"
                )
            return _resolve_binary_policy_verb(kind, verb, content, text_hook)
        return text_hook(kind, content)

    return _dispatch


def resolve_sanitizer(
    spec: str | None, *, policy: Mapping[str, Any] | None = None
) -> Callable[[str, bytes], bytes]:
    """Resolve the configured sanitizer to a ``(kind, content) -> content``
    callable that dispatches on artifact kind (``dtr-sn``).

    Exactly one of ``spec`` (the legacy ``--sanitizer <module>:<callable>``
    form, applied uniformly to every kind, unchanged semantics) or
    ``policy`` (the ``--sanitizer-policy`` mapping form, dispatching text
    kinds through its ``text`` hook and binary kinds through their declared
    verb) is expected to be set -- the CLI enforces mutual exclusivity via
    an ``argparse`` mutually exclusive group; this function additionally
    refuses both being set. Neither set (the default) resolves to an
    identity pass-through, since the hook is campaign-configured and this
    script has no campaign config of its own to read one from. Every
    failure to resolve a *supplied* spec or policy is a
    :class:`SanitizerError` (AC-CC03-2's only sanitizer-related exit path),
    not the base :class:`CaptureError`.
    """

    if policy is not None:
        if spec:
            raise SanitizerError(
                "--sanitizer and --sanitizer-policy are mutually exclusive"
            )
        return _resolve_policy_sanitizer(policy)
    if not spec:
        return _identity_sanitizer
    return _resolve_spec_sanitizer(spec)


def sanitize_before_journal(
    sanitizer: Callable[[str, bytes], bytes], kind: str, content: bytes
) -> bytes:
    """Run one artifact through the sanitizer before it is journaled/digested.

    Called strictly before ``EvidenceCatalog.add`` (which computes the
    content digest) and before the artifact is written to disk, so a
    sanitizer failure aborts the run before anything derived from the raw
    artifact becomes durable.
    """

    try:
        result = sanitizer(kind, content)
    except Exception as exc:  # noqa: BLE001 - any sanitizer failure aborts the run
        raise SanitizerError(
            f"pre_journal_sanitizer rejected a {kind!r} artifact: {exc}"
        ) from exc
    if not isinstance(result, (bytes, bytearray)):
        raise SanitizerError(
            f"pre_journal_sanitizer for {kind!r} must return bytes, "
            f"got {type(result).__name__}"
        )
    return bytes(result)


# ---------------------------------------------------------------------------
# A minimal, dependency-free HTML element index used by the stub driver
# ---------------------------------------------------------------------------


class _Element:
    __slots__ = ("tag", "attrs", "text_parts")

    def __init__(self, tag: str, attrs: Sequence[tuple[str, str | None]]) -> None:
        self.tag = tag
        self.attrs: dict[str, str] = {k: (v if v is not None else "") for k, v in attrs}
        self.text_parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()


class _SimpleHTMLParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self._stack: list[_Element] = []

    def handle_starttag(self, tag: str, attrs: Sequence[tuple[str, str | None]]) -> None:
        element = _Element(tag, attrs)
        self.elements.append(element)
        self._stack.append(element)

    def handle_startendtag(
        self, tag: str, attrs: Sequence[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1].text_parts.append(data)


def _select(elements: Sequence[_Element], selector: str) -> _Element | None:
    """A minimal selector matcher: one of ``#id``, ``.class``, or ``tag``."""

    selector = selector.strip()
    if selector.startswith("#"):
        wanted = selector[1:]
        return next((el for el in elements if el.attrs.get("id") == wanted), None)
    if selector.startswith("."):
        wanted = selector[1:]
        return next(
            (el for el in elements if wanted in (el.attrs.get("class") or "").split()),
            None,
        )
    return next((el for el in elements if el.tag == selector), None)


def _format_attrs(attrs: Mapping[str, str]) -> str:
    return " ".join(f'{key}="{value}"' for key, value in sorted(attrs.items()))


def _parse_inline_style(style: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in style.split(";"):
        if ":" not in declaration:
            continue
        prop, _, value = declaration.partition(":")
        prop = prop.strip()
        value = value.strip()
        if prop:
            result[prop] = value
    return result


# ---------------------------------------------------------------------------
# Driver contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellState:
    dom_snapshot: str
    computed_styles: dict[str, dict[str, str]]
    aria_snapshot: list[dict[str, Any]]
    console_log: list[dict[str, Any]]
    network_log: list[dict[str, Any]]
    screenshot: bytes


class Driver:
    kind: str

    def launch(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def open_cell(
        self, *, route: str, viewport: Mapping[str, Any], theme: str
    ) -> Any:
        raise NotImplementedError

    def apply_interaction(self, handle: Any, steps: Sequence[Mapping[str, Any]]) -> None:
        raise NotImplementedError

    def read_state(self, handle: Any, selectors: Sequence[str]) -> CellState:
        """Read the current end state. Called twice per cell, back to back,
        with a real settle wait in between (``_END_STATE_SETTLE_SECONDS`` in
        ``_run_cell``) -- never told which call is "the second one", so a
        driver has no way to special-case, and therefore fabricate, the
        disagreement AC-CC03-3 looks for.
        """

        raise NotImplementedError

    def close_cell(self, handle: Any) -> None:
        raise NotImplementedError


class StubDriver(Driver):
    """No-JS-engine driver: reads the fixture's static markup directly.

    Never fails to "launch" -- it needs no external process -- so it is
    always available as the honest fallback when no real browser is. It
    applies the fixture's own ``data-style-<theme>`` convention (documented
    in ``app.js``) structurally instead of executing any script, and
    structurally emulates the one general dynamic-content convention any
    fixture route may declare -- an element carrying ``data-dynamic-interval-
    ms`` gets its text recomputed from real elapsed wall-clock time on every
    read, exactly like ``app.js``'s ``setInterval`` would in a real browser.
    Nothing here is keyed to a specific fixture route or element id: a route
    with no such attribute reads back identically every time.
    """

    kind = "stub"

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir

    def launch(self) -> None:
        return

    def close(self) -> None:
        return

    def open_cell(self, *, route: str, viewport: Mapping[str, Any], theme: str) -> Any:
        path = self.app_dir / route
        if not path.is_file():
            raise RouteUnreachable(f"route file not found: {route}")
        text = path.read_text(encoding="utf-8")
        parser = _SimpleHTMLParser()
        parser.feed(text)
        return {
            "route": route,
            "viewport": viewport,
            "theme": theme,
            "elements": parser.elements,
            "opened_at": time.monotonic(),
            "interaction_log": [],
        }

    def apply_interaction(self, handle: Any, steps: Sequence[Mapping[str, Any]]) -> None:
        for step in steps:
            if step.get("action") != "click":
                continue
            selector = step.get("selector")
            if not selector:
                continue
            element = _select(handle["elements"], selector)
            if element is not None and element.tag == "button":
                expanded = element.attrs.get("aria-expanded") == "true"
                element.attrs["aria-expanded"] = "false" if expanded else "true"
                handle["interaction_log"].append(
                    f"click {selector!r} -> aria-expanded="
                    f"{element.attrs['aria-expanded']}"
                )

    def _apply_theme(self, elements: Sequence[_Element], theme: str) -> None:
        key = f"data-style-{theme}"
        for element in elements:
            declared = element.attrs.get(key)
            if declared:
                element.attrs["style"] = declared

    def _apply_dynamic_text(self, elements: Sequence[_Element], opened_at: float) -> None:
        """Recompute every ``data-dynamic-interval-ms`` element's text from
        real elapsed time since the cell opened, independent of which read
        this is -- the generic structural emulation of ``app.js``'s
        ``setInterval(..., interval)`` convention (documented there).
        """

        now = time.monotonic()
        for element in elements:
            raw_interval = element.attrs.get("data-dynamic-interval-ms")
            if not raw_interval:
                continue
            try:
                interval_seconds = float(raw_interval) / 1000.0
            except ValueError:
                continue
            if interval_seconds <= 0:
                continue
            tick = int((now - opened_at) / interval_seconds)
            element.text_parts = [f"ts-tick:{tick}"]

    def read_state(self, handle: Any, selectors: Sequence[str]) -> CellState:
        elements: list[_Element] = handle["elements"]
        self._apply_theme(elements, handle["theme"])
        self._apply_dynamic_text(elements, handle["opened_at"])

        dom_snapshot = "\n".join(
            f"<{el.tag} {_format_attrs(el.attrs)}>{el.text}</{el.tag}>" for el in elements
        )
        computed_styles = {
            selector: (
                _parse_inline_style(match.attrs.get("style", ""))
                if (match := _select(elements, selector)) is not None
                else {}
            )
            for selector in selectors
        }
        aria_snapshot = [
            {
                "tag": el.tag,
                "id": el.attrs.get("id"),
                "role": el.attrs.get("role"),
                **{k: v for k, v in el.attrs.items() if k.startswith("aria-")},
            }
            for el in elements
            if el.attrs.get("role") or any(k.startswith("aria-") for k in el.attrs)
        ]
        console_log = [
            {
                "level": "info",
                "text": f"stub-driver: no live console capture for route {handle['route']!r}",
            }
        ] + [{"level": "info", "text": entry} for entry in handle["interaction_log"]]
        network_log = [
            {
                "url": handle["route"],
                "method": "GET",
                "status": 200,
                "note": "stub-driver: no live network capture, static file read only",
            }
        ]
        return CellState(
            dom_snapshot=dom_snapshot,
            computed_styles=computed_styles,
            aria_snapshot=aria_snapshot,
            console_log=console_log,
            network_log=network_log,
            screenshot=_PLACEHOLDER_PNG,
        )

    def close_cell(self, handle: Any) -> None:
        return


class RealBrowserDriver(Driver):
    """Playwright-backed driver. Never imported at module load time.

    Serves the fixture app over a local ``http.server`` thread and drives a
    real Chromium instance against it. Only reachable when ``playwright`` is
    importable under the resolved ``--python`` interpreter and a browser
    actually launches; harness CI carries no such dependency, so in the
    common case this class is defined but never instantiated.
    """

    kind = "real"

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._base_url = ""
        self._playwright: Any = None
        self._browser: Any = None

    def _start_server(self) -> None:
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(self.app_dir))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = self._server.server_address[1]
        self._base_url = f"http://127.0.0.1:{port}"
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()

    def _stop_server(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self._server = None
        self._server_thread = None

    def launch(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserLaunchError(f"playwright is not importable: {exc}") from exc
        self._start_server()
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure aborts the run
            self._stop_server()
            raise BrowserLaunchError(f"browser could not launch: {exc}") from exc

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._stop_server()

    def open_cell(self, *, route: str, viewport: Mapping[str, Any], theme: str) -> Any:
        # Pin the device scale factor and emulate reduced motion at page
        # creation, and disable CSS animation/transition duration once
        # loaded (readiness gating, no-readiness-gating): without this, the
        # two end-state reads a real capture takes race whatever the page
        # happens to be animating, instead of only disagreeing on evidence
        # this fixture actually declares as dynamic.
        page = self._browser.new_page(
            viewport={"width": viewport["width"], "height": viewport["height"]},
            device_scale_factor=1,
            reduced_motion="reduce",
        )
        console_log: list[dict[str, Any]] = []
        network_log: list[dict[str, Any]] = []
        page.on(
            "console",
            lambda msg: console_log.append({"level": msg.type, "text": msg.text}),
        )
        page.on(
            "request",
            lambda req: network_log.append({"url": req.url, "method": req.method}),
        )
        url = f"{self._base_url}/{route}?theme={theme}"
        try:
            response = page.goto(url, wait_until="load")
        except Exception as exc:  # noqa: BLE001 - navigation failure -> unreachable cell
            page.close()
            raise RouteUnreachable(f"navigation to {route!r} failed: {exc}") from None
        if response is None or response.status >= 400:
            status = getattr(response, "status", None)
            page.close()
            raise RouteUnreachable(f"navigation to {route!r} returned status {status}")
        self._gate_readiness(page)
        return {"page": page, "console_log": console_log, "network_log": network_log, "route": route}

    def _gate_readiness(self, page: Any) -> None:
        """Best-effort: freeze CSS animation/transition durations and wait
        for web fonts to finish loading, so a page with no declared dynamic
        content reads back identically on both end-state reads.
        """

        try:
            page.add_style_tag(
                content=(
                    "*, *::before, *::after { animation-duration: 0s !important; "
                    "animation-delay: 0s !important; transition-duration: 0s !important; "
                    "transition-delay: 0s !important; }"
                )
            )
        except Exception:  # noqa: BLE001 - readiness gating must not fail the cell
            pass
        try:
            page.evaluate("() => (document.fonts ? document.fonts.ready : null)")
        except Exception:  # noqa: BLE001
            pass

    def apply_interaction(self, handle: Any, steps: Sequence[Mapping[str, Any]]) -> None:
        page = handle["page"]
        for step in steps:
            if step.get("action") == "click" and step.get("selector"):
                try:
                    page.click(step["selector"], timeout=1000)
                except Exception:  # noqa: BLE001 - a missing target is not a launch failure
                    pass

    def read_state(self, handle: Any, selectors: Sequence[str]) -> CellState:
        page = handle["page"]
        dom_snapshot = page.content()
        computed_styles: dict[str, dict[str, str]] = {}
        for selector in selectors:
            try:
                styles = page.eval_on_selector(
                    selector,
                    "el => { const s = getComputedStyle(el); "
                    "return {color: s.color, fontSize: s.fontSize, "
                    "backgroundColor: s.backgroundColor}; }",
                )
            except Exception:  # noqa: BLE001 - selector absent on this page
                styles = None
            computed_styles[selector] = styles or {}
        try:
            aria = page.accessibility.snapshot() or {}
        except Exception:  # noqa: BLE001
            aria = {}
        screenshot = page.screenshot()
        return CellState(
            dom_snapshot=dom_snapshot,
            computed_styles=computed_styles,
            aria_snapshot=[aria] if isinstance(aria, dict) else list(aria),
            console_log=list(handle["console_log"]),
            network_log=list(handle["network_log"]),
            screenshot=screenshot,
        )

    def close_cell(self, handle: Any) -> None:
        handle["page"].close()


def _real_driver_available(python_path: str) -> tuple[bool, str | None]:
    """Probe, under ``python_path``, whether a real browser driver can run.

    Never imports playwright in *this* process -- the probe runs in a
    subprocess under the resolved interpreter, which may differ from the one
    running this script.
    """

    probe = "import importlib; importlib.import_module('playwright.sync_api')"
    try:
        result = subprocess.run(
            [python_path, "-c", probe],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not probe interpreter {python_path!r}: {exc}"
    if result.returncode != 0:
        stderr_lines = (result.stderr or "").strip().splitlines()
        reason = stderr_lines[-1] if stderr_lines else (
            f"playwright not importable under {python_path!r}"
        )
        return False, reason
    return True, None


# ---------------------------------------------------------------------------
# Matrix walk
# ---------------------------------------------------------------------------


def _state_digest(state: CellState) -> str:
    """Digest only the end-state DOM and computed styles (AC-CC03-3)."""

    payload = json.dumps(
        {"dom_snapshot": state.dom_snapshot, "computed_styles": state.computed_styles},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# A fixed pause between a cell's two end-state reads (AC-CC03-3,
# no-readiness-gating). Long enough that any element the fixture declares
# dynamic (``data-dynamic-interval-ms``, a 5ms interval in this fixture) has
# a real chance to have changed by the second read on *either* driver --
# so instability is observed from genuine elapsed time, never fabricated by
# a driver branching on which read it has been asked to perform.
_END_STATE_SETTLE_SECONDS = 0.03


def _capture_screenshot_evidence(
    catalog: EvidenceCatalog, cell_id: str, sanitized_png: bytes
) -> Any:
    """Route a cell's (already-sanitized) screenshot through
    ``harness_labs.core.verification_images.capture_failure_images``
    (build-order-cc-03's reuse mandate: "selection, size/count budgets,
    atomic copy into the evidence catalog").

    That module scopes itself to images a *failing pytest run* left under a
    ``tmp_path``-shaped ``basetemp``; a capture cell has neither, so a
    single-file scratch directory stands in for ``basetemp`` and
    ``{"exit_code": 1}`` stands in for the pytest command result -- the one
    value ``capture_failure_images`` actually inspects on that mapping is
    ``exit_code`` (to skip a *passing* run), and every cell's screenshot is
    evidence worth budgeting and persisting regardless of that cell's own
    ``ok``/``unreachable``/``unstable`` status. Returns the resulting
    :class:`~harness_labs.core.verification_images.CapturedImages` (falsy
    when capture is switched off or the budget excluded the image). The
    catalog's own ``AuditJournal`` (``catalog.add``, below) is the artifact's
    only route to durable storage -- ``EvidenceCatalog.restore`` documents
    the principle this follows: "without writing a duplicate artifact". A
    hand-rolled second copy under the capture script's own output directory
    would be exactly that duplicate, and would not be adding "only the
    matrix walk and receipt on top" of the reused persistence.
    """

    with tempfile.TemporaryDirectory(prefix="ui-fidelity-screenshot-") as scratch:
        scratch_dir = Path(scratch)
        (scratch_dir / "screenshot.png").write_bytes(sanitized_png)
        return capture_failure_images(
            command={"exit_code": 1},
            basetemp=scratch_dir,
            evidence=catalog,
            producer_task_id=cell_id,
            limit=1,
        )


def _run_cell(
    *,
    driver: Driver,
    route: str,
    viewport: Mapping[str, Any],
    theme: str,
    interaction: Mapping[str, Any],
    selectors: Sequence[str],
    sanitizer: Callable[[str, bytes], bytes],
    catalog: EvidenceCatalog,
) -> dict[str, Any]:
    # A malformed matrix entry (e.g. a viewport/interaction object missing
    # its declared "name") must fail this one cell, not crash the whole run
    # with an uncaught KeyError outside the exit contract (AC-CC03-2) -- so
    # every label used to build the cell's own identity is read defensively,
    # before anything that could itself raise.
    viewport_name = viewport.get("name") if isinstance(viewport, Mapping) else None
    interaction_name = (
        interaction.get("name") if isinstance(interaction, Mapping) else None
    )
    cell_id = f"{route}|{viewport_name}|{theme}|{interaction_name}"

    def _unreachable(reason: str) -> dict[str, Any]:
        return {
            "route": route,
            "viewport": viewport_name,
            "theme": theme,
            "interaction": interaction_name,
            "cell_id": cell_id,
            "status": "unreachable",
            "reason": reason,
            "artifacts": {},
            "artifact_paths": {},
        }

    if viewport_name is None or interaction_name is None:
        return _unreachable(
            "malformed matrix entry: viewport and interaction objects must "
            "each declare a 'name'"
        )

    try:
        handle = driver.open_cell(route=route, viewport=viewport, theme=theme)
    except RouteUnreachable as exc:
        return _unreachable(str(exc))
    except CaptureError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unanticipated open_cell fault
        # fails the cell, not the run: same exit-contract carve-out as the
        # apply_interaction/read_state guard below.
        return _unreachable(f"cell open failed: {exc}")

    try:
        try:
            driver.apply_interaction(handle, interaction.get("steps", ()))
            read_1 = driver.read_state(handle, selectors)
            digest_1 = _state_digest(read_1)
            time.sleep(_END_STATE_SETTLE_SECONDS)
            read_2 = driver.read_state(handle, selectors)
            digest_2 = _state_digest(read_2)
        except CaptureError:
            raise
        except Exception as exc:  # noqa: BLE001 - a per-cell driver fault fails
            # the cell, not the run: the exit contract (AC-CC03-2) reserves a
            # nonzero exit for a browser that could not launch at all, or a
            # sanitizer rejection -- a mid-cell driver hiccup must not escape
            # and discard every cell already captured.
            return _unreachable(f"cell capture failed: {exc}")
    finally:
        try:
            driver.close_cell(handle)
        except Exception:  # noqa: BLE001 - a cleanup fault must not replace
            # this cell's already-computed return value with an uncaught
            # exception that escapes the exit contract (AC-CC03-2).
            pass

    status = "ok" if digest_1 == digest_2 else "unstable"
    assert status in CAPTURE_CELL_STATUSES

    final = read_2
    try:
        raw_by_kind = {
            "screenshot": final.screenshot,
            "dom_snapshot": final.dom_snapshot.encode("utf-8"),
            "computed_styles": json.dumps(
                final.computed_styles, sort_keys=True
            ).encode("utf-8"),
            "aria_snapshot": json.dumps(final.aria_snapshot, sort_keys=True).encode(
                "utf-8"
            ),
            "console_log": json.dumps(final.console_log, sort_keys=True).encode(
                "utf-8"
            ),
            "network_log": json.dumps(final.network_log, sort_keys=True).encode(
                "utf-8"
            ),
        }
    except Exception as exc:  # noqa: BLE001 - a driver returning a malformed
        # CellState (non-string DOM, non-JSON-serializable styles) must fail
        # this cell, not escape the exit contract (AC-CC03-2) as an uncaught
        # traceback -- the same carve-out as the open_cell/read_state guards
        # above.
        return _unreachable(f"end-state serialization failed: {exc}")

    artifacts: dict[str, str] = {}
    artifact_paths: dict[str, str] = {}
    screenshot_evidence: dict[str, Any] | None = None
    for kind in ARTIFACT_KINDS:
        sanitized = sanitize_before_journal(sanitizer, kind, raw_by_kind[kind])
        # sanitize_before_journal already raises SanitizerError (a
        # CaptureError) on the one failure the exit contract reserves a
        # nonzero exit for; catalog.add failing for any other reason (e.g. a
        # disk write error inside AuditJournal.write_artifact) must fail this
        # cell, not escape the exit contract as an uncaught traceback with no
        # receipt.
        try:
            if kind == "screenshot":
                captured = _capture_screenshot_evidence(catalog, cell_id, sanitized)
                screenshot_evidence = {
                    "scope": captured.scope,
                    "considered": captured.considered,
                    "selected": len(captured.descriptors),
                }
            # catalog.add is the artifact's only route to durable storage: it
            # journals through AuditJournal.write_artifact's own
            # temp-file-plus-rename-plus-fsync atomic write (the reuse
            # mandate's "atomic copy into the evidence catalog"). No second,
            # hand-rolled copy is written here.
            record = catalog.add(
                kind=kind,
                content=sanitized,
                media_type=_MEDIA_TYPES[kind],
                producer_task_id=cell_id,
            )
        except CaptureError:
            raise
        except Exception as exc:  # noqa: BLE001 - artifact persistence fault
            return _unreachable(f"artifact persistence failed for {kind!r}: {exc}")
        artifacts[kind] = record.ref
        artifact_paths[kind] = record.audit_path or ""

    return {
        "route": route,
        "viewport": viewport_name,
        "theme": theme,
        "interaction": interaction_name,
        "cell_id": cell_id,
        "status": status,
        "end_state_digests": {"read_1": digest_1, "read_2": digest_2},
        "artifacts": artifacts,
        "artifact_paths": artifact_paths,
        "screenshot_evidence": screenshot_evidence,
    }


def _load_matrix(matrix_path: str) -> dict[str, Any]:
    """Load and validate ``--matrix``.

    Raised as :class:`BrowserLaunchError`: capture cannot open a single cell
    without a valid matrix, so a malformed one belongs to the same "could not
    launch" exit-contract bucket (AC-CC03-2) rather than an uncaught
    ``json.JSONDecodeError``/``KeyError``/``TypeError`` traceback with no
    receipt at all. This includes each of ``routes``/``viewports``/
    ``themes``/``interactions`` actually being a list: ``run_capture``'s
    matrix walk (``for route in routes: ...``) is unguarded, so a present but
    non-list value (``null``, an int, ...) must be rejected here, before that
    walk runs, rather than surfacing as an uncaught ``TypeError`` there.
    """

    try:
        raw = Path(matrix_path).read_text(encoding="utf-8")
        decoded = json.loads(raw)
        matrix = {
            "routes": decoded["routes"],
            "viewports": decoded["viewports"],
            "themes": decoded["themes"],
            "interactions": decoded["interactions"],
            "selectors": decoded.get("selectors", []),
        }
        for key in ("routes", "viewports", "themes", "interactions", "selectors"):
            if not isinstance(matrix[key], list):
                raise TypeError(f"{key!r} must be a list, got {type(matrix[key]).__name__}")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BrowserLaunchError(
            f"could not load capture matrix {matrix_path!r}: {exc}"
        ) from exc
    return matrix


def _load_sanitizer_policy(policy_path: str) -> Mapping[str, Any]:
    """Load the ``--sanitizer-policy <json-file>`` mapping."""

    try:
        raw = Path(policy_path).read_text(encoding="utf-8")
        policy = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SanitizerError(
            f"could not load --sanitizer-policy {policy_path!r}: {exc}"
        ) from exc
    if not isinstance(policy, Mapping):
        raise SanitizerError(
            f"--sanitizer-policy {policy_path!r} must hold a JSON object, "
            f"got {type(policy).__name__}"
        )
    return policy


def _resolve_sanitizer_from_args(
    args: argparse.Namespace,
) -> Callable[[str, bytes], bytes]:
    """Resolve the CLI's configured sanitizer -- ``--sanitizer-policy`` (the
    mapping form) or legacy ``--sanitizer`` (a spec string), mutually
    exclusive (``dtr-sn``)."""

    policy_path = getattr(args, "sanitizer_policy", None)
    if policy_path:
        return resolve_sanitizer(None, policy=_load_sanitizer_policy(policy_path))
    return resolve_sanitizer(args.sanitizer)


def _sample_bundle() -> dict[str, bytes]:
    """One representative raw artifact per kind, used by ``--dry-run`` to
    exercise the resolved sanitizer without a real capture (AC-SN-3)."""

    return {
        "screenshot": _PLACEHOLDER_PNG,
        "dom_snapshot": b"<html><body>dry-run sample</body></html>",
        "computed_styles": json.dumps({"sample": True}, sort_keys=True).encode("utf-8"),
        "aria_snapshot": json.dumps({"role": "sample"}, sort_keys=True).encode("utf-8"),
        "console_log": json.dumps([], sort_keys=True).encode("utf-8"),
        "network_log": json.dumps([], sort_keys=True).encode("utf-8"),
    }


def run_sanitizer_dry_run(args: argparse.Namespace) -> dict[str, Any]:
    """``--dry-run``: run the resolved sanitizer over a sample bundle and
    report would-be rejections, naming the refusing rule, without opening an
    :class:`AuditJournal` or writing any artifact (AC-SN-3): the journal
    file is absent after this run rather than merely unchanged.
    """

    sanitizer = _resolve_sanitizer_from_args(args)
    bundle = _sample_bundle()
    report: list[dict[str, Any]] = []
    for kind in ARTIFACT_KINDS:
        entry: dict[str, Any] = {"kind": kind, "media_type": _MEDIA_TYPES[kind]}
        try:
            sanitize_before_journal(sanitizer, kind, bundle[kind])
        except SanitizerError as exc:
            entry["would_reject"] = True
            entry["reason"] = str(exc)
        else:
            entry["would_reject"] = False
            entry["reason"] = None
        report.append(entry)
    return {
        "schema": RECEIPT_SCHEMA,
        "dry_run": True,
        "sanitizer_report": report,
        "cells": [],
    }


def run_capture(args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    app_dir = Path(args.app_dir).resolve()
    matrix = _load_matrix(args.matrix)
    routes = matrix["routes"]
    viewports = matrix["viewports"]
    themes = matrix["themes"]
    interactions = matrix["interactions"]
    selectors = matrix["selectors"]

    python_path = args.python or sys.executable
    sanitizer = _resolve_sanitizer_from_args(args)
    # Back the catalog with a real AuditJournal so catalog.add() genuinely
    # journals every artifact via AuditJournal.write_artifact's atomic-write
    # primitive (AC-CC03-4: "before it is journaled") instead of only
    # bookkeeping an in-memory record -- the reuse mandate's "atomic copy
    # into the evidence catalog". Construction failure means capture cannot
    # produce durable evidence for a single cell, so it belongs to the same
    # "could not launch" exit-contract bucket _load_matrix already uses.
    try:
        audit = AuditJournal(
            out_dir / "audit",
            "ui-fidelity-capture",
            actor=AuditActor("ui-fidelity-capture", "measurer"),
        )
    except Exception as exc:  # noqa: BLE001 - any journal setup fault aborts the run
        raise BrowserLaunchError(f"could not open evidence audit journal: {exc}") from exc
    catalog = EvidenceCatalog(audit=audit)

    requested = args.driver
    skip_reason: str | None = None
    driver: Driver
    if requested == "stub":
        driver = StubDriver(app_dir)
    else:
        available, reason = _real_driver_available(python_path)
        if available:
            driver = RealBrowserDriver(app_dir)
        elif requested == "real":
            raise BrowserLaunchError(reason or "real browser driver unavailable")
        else:  # auto, unavailable: fall back to the stub, honestly recorded
            driver = StubDriver(app_dir)
            skip_reason = reason or "real browser driver unavailable"

    try:
        driver.launch()
    except BrowserLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001 - any launch failure aborts the run
        raise BrowserLaunchError(str(exc)) from exc

    cells: list[dict[str, Any]] = []
    try:
        for route in routes:
            for viewport in viewports:
                for theme in themes:
                    for interaction in interactions:
                        cells.append(
                            _run_cell(
                                driver=driver,
                                route=route,
                                viewport=viewport,
                                theme=theme,
                                interaction=interaction,
                                selectors=selectors,
                                sanitizer=sanitizer,
                                catalog=catalog,
                            )
                        )
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001 - a cleanup fault must not discard
            # a run that already captured cells; the exit contract reserves
            # a nonzero exit for launch/sanitizer failure, not teardown.
            pass

    return {
        "schema": RECEIPT_SCHEMA,
        "driver": {
            "kind": driver.kind,
            "requested": requested,
            "python": python_path,
            "skip_reason": skip_reason,
        },
        "matrix": {
            "routes": routes,
            "viewport_count": len(viewports),
            "theme_count": len(themes),
            "interaction_count": len(interactions),
            "selectors": list(selectors),
        },
        "cells": cells,
        "sanitizer": {
            "spec": args.sanitizer,
            "policy_path": getattr(args, "sanitizer_policy", None),
            "artifacts_checked": len(cells) * len(ARTIFACT_KINDS),
        },
        "evidence": _evidence_summary(cells, audit.artifacts_dir),
        "audit_run_dir": str(audit.run_dir),
    }


def _evidence_summary(
    cells: Sequence[Mapping[str, Any]], audit_artifacts_dir: Path
) -> dict[str, Any]:
    """Summarize the ``verification_images`` reuse (build-order-cc-03).

    ``add_dir`` names the one directory every artifact this run persisted
    lives under -- the grant a downstream worker-facing executor would pass
    as ``--add-dir`` (mirroring ``claude_task_executor.py``'s per-directory
    grant for ``attached_image_paths``) -- populated only when at least one
    screenshot was actually selected and budgeted through
    ``capture_failure_images``. It names ``AuditJournal.artifacts_dir``
    itself (the directory ``catalog.add`` already journals every artifact
    into), not a second directory this script maintains on its own, so the
    grant points at the one place the pixels actually live.
    """

    selected = sum(
        cell["screenshot_evidence"]["selected"]
        for cell in cells
        if cell.get("screenshot_evidence")
    )
    return {
        "screenshots_selected_via_verification_images": selected,
        "add_dir": [str(audit_artifacts_dir.resolve())] if selected else [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-dir",
        default=None,
        help="Static fixture app root (required unless --dry-run)",
    )
    parser.add_argument(
        "--matrix",
        default=None,
        help="Path to a matrix.json declaration (required unless --dry-run)",
    )
    parser.add_argument("--out", required=True, help="Output directory for the receipt and artifacts")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Interpreter used to resolve/launch the browser driver (default: sys.executable)",
    )
    parser.add_argument(
        "--driver",
        choices=("auto", "stub", "real"),
        default="auto",
        help="Force stub/real, or auto-detect (default: auto)",
    )
    sanitizer_group = parser.add_mutually_exclusive_group()
    sanitizer_group.add_argument(
        "--sanitizer",
        default=None,
        help="pre_journal_sanitizer hook as <module-or-path.py>:<callable> (default: identity)",
    )
    sanitizer_group.add_argument(
        "--sanitizer-policy",
        default=None,
        help=(
            "Path to a JSON file holding the {'text': <hook-ref>, "
            "'binary': {'<kind>': 'scan'|'admit:<reason>'|'reject'}} sanitizer "
            "policy mapping; mutually exclusive with --sanitizer"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the resolved sanitizer over a sample bundle, report would-be "
            "rejections, and journal nothing"
        ),
    )
    return parser


def _write_error_receipt(path: Path, *, kind: str, error: str, python_path: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "error": {"kind": kind, "message": error},
                "driver": {"python": python_path},
                "cells": [],
                "exit_code": None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.dry_run and (not args.app_dir or not args.matrix):
        parser.error("--app-dir and --matrix are required unless --dry-run is set")
    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # No output directory means no receipt can be written either, so
        # this cannot be folded into _write_error_receipt; it belongs to the
        # same "could not launch" exit-contract bucket as a browser that
        # never starts (AC-CC03-2) rather than an uncaught traceback.
        print(
            f"ui_fidelity_capture: could not create output directory {args.out!r}: {exc}",
            file=sys.stderr,
        )
        return EXIT_LAUNCH_FAILURE
    receipt_path = out_dir / "receipt.json"

    try:
        # --dry-run never opens an AuditJournal or writes an artifact: the
        # journal is absent afterward rather than merely unchanged (AC-SN-3).
        receipt = run_sanitizer_dry_run(args) if args.dry_run else run_capture(args, out_dir)
    except SanitizerError as exc:
        _write_error_receipt(
            receipt_path, kind="sanitizer_failure", error=str(exc), python_path=args.python
        )
        print(f"ui_fidelity_capture: sanitizer failure: {exc}", file=sys.stderr)
        return EXIT_SANITIZER_FAILURE
    except BrowserLaunchError as exc:
        _write_error_receipt(
            receipt_path, kind="browser_launch_failure", error=str(exc), python_path=args.python
        )
        print(f"ui_fidelity_capture: browser launch failure: {exc}", file=sys.stderr)
        return EXIT_LAUNCH_FAILURE

    receipt["exit_code"] = EXIT_OK
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
