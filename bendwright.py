#!/usr/bin/env python3
"""bendwright — local offline editor for Archify diagram IR JSON.

M3: stdlib loopback SPA + structural/Archify save + drag-to-move (lane/col snap).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

APP = "bendwright"
IR_KEYS = ("nodes", "edges", "lanes")

ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
_STYLE_BLOCK_RE = re.compile(rb"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_SVG_RE = re.compile(rb"<svg\b[^>]*>.*?</svg>", re.IGNORECASE | re.DOTALL)

# Static enums (M1). Dynamic $ref harvest is later polish.
STATIC_ENUMS: dict[str, list[str]] = {
    "node.type": [
        "frontend",
        "backend",
        "database",
        "cloud",
        "security",
        "messagebus",
        "external",
    ],
    "lane.variant": ["normal", "exception"],
    "edge.role": ["main", "branch", "async", "return", "error"],
    "edge.variant": ["default", "emphasis", "security", "dashed"],
    "edge.route": [
        "auto",
        "straight",
        "drop",
        "outside-right",
        "return-left",
        "bottom-channel",
        "up-channel",
    ],
    "edge.fromSide": ["left", "right", "top", "bottom"],
    "edge.toSide": ["left", "right", "top", "bottom"],
    "meta.quality_profile": ["standard", "showcase"],
    "cards.dot": ["cyan", "emerald", "violet", "amber", "rose", "orange", "slate"],
}

# ---------------------------------------------------------------------------
# Shared runtime state (set in main)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_file_path: Path | None = None
_diagram_type: str = "workflow"
_doc: dict[str, Any] = {}
_had_trailing_newline: bool = True
_archify_path: str | None = None
_preview_html: bytes | None = None
_diagram_html: bytes | None = None

# M18: short-request inflight accounting. Not a shutdown signal (M22 uses SSE presence).
_httpd: ThreadingHTTPServer | None = None
_inflight: int = 0
_inflight_lock = threading.Lock()

# M22: one held /api/alive SSE per tab. Auto-exit only after the last one drops.
ALIVE_KEEPALIVE_S = 5.0
ALIVE_GRACE_S = 25.0
_alive: int = 0
_alive_lock = threading.Lock()
_alive_seen: bool = False
_last_zero_ts: float | None = None
_auto_exit: bool = True


def _begin_request() -> None:
    """Enter a short request. Inflight does not affect M22 shutdown."""
    global _inflight
    with _inflight_lock:
        _inflight += 1


def _end_request() -> None:
    """Leave a short request."""
    global _inflight
    with _inflight_lock:
        _inflight -= 1


def detect_archify(cli_path: str | None) -> tuple[str | None, str]:
    """Resolve archify.mjs (first hit wins). Returns (path_or_None, banner_detail)."""
    candidates: list[Path] = []
    if cli_path:
        candidates.append(Path(cli_path))
    env_home = os.environ.get("ARCHIFY_HOME")
    if env_home:
        home = Path(env_home)
        candidates.append(home)
        candidates.append(home / "bin" / "archify.mjs")
    user_home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    # Neutral resolution order: alongside this script, a sibling "archify"
    # checkout, the current directory, then the user's home directory.
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, Path.cwd(), user_home):
        candidates.append(base / "archify" / "bin" / "archify.mjs")
        candidates.append(base / "archify.mjs")
    candidates.append(Path("bin") / "archify.mjs")
    on_path = shutil.which("archify")
    if on_path:
        candidates.append(Path(on_path))

    found: str | None = None
    for c in candidates:
        try:
            if c.is_file():
                found = str(c.resolve())
                break
        except OSError:
            continue

    if not found:
        return None, "none (no archify.mjs found; structural-check save, no preview)"
    if not shutil.which("node"):
        return None, "none (archify found but node not on PATH; structural-check save, no preview)"
    return found, found


def load_doc(path: Path) -> tuple[dict[str, Any], bool]:
    raw = path.read_text(encoding="utf-8")
    trailing = raw.endswith("\n")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise SystemExit(f"[{APP}] IR root must be a JSON object: {path}")
    return doc, trailing


def native_pick_path(initial_dir: str) -> dict[str, Any]:
    """Open a native OS file dialog via a tkinter subprocess (M12).

    Isolates Tk from the http.server worker thread. Returns
    {ok:true, path} | {ok:true, cancelled:true} | {ok:false, error:...}.
    """
    init_literal = json.dumps(initial_dir)
    script = (
        "import sys\n"
        "try:\n"
        "    from tkinter import Tk, filedialog\n"
        "except Exception:\n"
        "    sys.exit(2)\n"
        "root = Tk()\n"
        "root.withdraw()\n"
        "try:\n"
        '    root.attributes("-topmost", True)\n'
        "except Exception:\n"
        "    pass\n"
        "path = filedialog.askopenfilename(\n"
        f"    initialdir={init_literal},\n"
        '    title="Open diagram",\n'
        '    filetypes=[("Archify diagrams", "*.json"), ("All files", "*.*")],\n'
        ")\n"
        'print(path if path else "", end="")\n'
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "error": "native picker unavailable"}
    if proc.returncode != 0:
        return {"ok": False, "error": "native picker unavailable"}
    chosen = (proc.stdout or "").strip()
    if not chosen:
        return {"ok": True, "cancelled": True}
    return {"ok": True, "path": chosen}


def state_payload() -> dict[str, Any]:
    if _file_path is None:
        return {
            "file": None,
            "diagram_type": _diagram_type,
            "doc": None,
            "ir": {k: [] for k in IR_KEYS},
            "enums": merge_enums({}),
            "archify": _archify_path,
        }
    return {
        "file": str(_file_path),
        "diagram_type": _diagram_type,
        "doc": _doc,
        "ir": {k: list(_doc.get(k) or []) for k in IR_KEYS},
        "enums": merge_enums(_doc),
        "archify": _archify_path,
    }


def merge_enums(doc: dict[str, Any]) -> dict[str, list[str]]:
    """Static enums merged with distinct in-file values (order: static first)."""
    collected: dict[str, set[str]] = {k: set() for k in STATIC_ENUMS}

    for node in doc.get("nodes") or []:
        if isinstance(node, dict) and isinstance(node.get("type"), str):
            collected["node.type"].add(node["type"])

    for lane in doc.get("lanes") or []:
        if isinstance(lane, dict) and isinstance(lane.get("variant"), str):
            collected["lane.variant"].add(lane["variant"])

    for edge in doc.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        for key, enum_key in (
            ("role", "edge.role"),
            ("variant", "edge.variant"),
            ("route", "edge.route"),
            ("fromSide", "edge.fromSide"),
            ("toSide", "edge.toSide"),
        ):
            v = edge.get(key)
            if isinstance(v, str):
                collected[enum_key].add(v)

    meta = doc.get("meta")
    if isinstance(meta, dict):
        qp = meta.get("quality_profile")
        if isinstance(qp, str):
            collected["meta.quality_profile"].add(qp)

    cards = doc.get("cards")
    if isinstance(cards, list):
        for card in cards:
            if isinstance(card, dict) and isinstance(card.get("dot"), str):
                collected["cards.dot"].add(card["dot"])

    out: dict[str, list[str]] = {}
    for key, base in STATIC_ENUMS.items():
        merged: list[str] = []
        seen: set[str] = set()
        for v in base:
            if v not in seen:
                merged.append(v)
                seen.add(v)
        for v in sorted(collected[key]):
            if v not in seen:
                merged.append(v)
                seen.add(v)
        out[key] = merged
    return out


def structural_check(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    meta = doc.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta must be an object")
    elif "title" not in meta:
        errors.append("meta.title is required")

    nodes = doc.get("nodes")
    edges = doc.get("edges")
    lanes = doc.get("lanes")
    if not isinstance(nodes, list):
        errors.append("nodes must be an array")
        nodes = []
    if not isinstance(edges, list):
        errors.append("edges must be an array")
        edges = []
    if not isinstance(lanes, list):
        errors.append("lanes must be an array")
        lanes = []

    lane_ids: list[str] = []
    for i, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            errors.append(f"lanes[{i}] must be an object")
            continue
        for req in ("id", "label"):
            if req not in lane:
                errors.append(f"lanes[{i}].{req} is required")
        lid = lane.get("id")
        if isinstance(lid, str):
            if not ID_RE.match(lid):
                errors.append(f"lanes[{i}].id invalid: {lid!r}")
            lane_ids.append(lid)
        elif "id" in lane:
            errors.append(f"lanes[{i}].id must be a string")

    if len(lane_ids) != len(set(lane_ids)):
        errors.append("lane ids must be unique")
    lane_set = set(lane_ids)

    node_ids: list[str] = []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"nodes[{i}] must be an object")
            continue
        for req in ("id", "lane", "col", "type", "label"):
            if req not in node:
                errors.append(f"nodes[{i}].{req} is required")
        nid = node.get("id")
        if isinstance(nid, str):
            if not ID_RE.match(nid):
                errors.append(f"nodes[{i}].id invalid: {nid!r}")
            node_ids.append(nid)
        elif "id" in node:
            errors.append(f"nodes[{i}].id must be a string")

        nlane = node.get("lane")
        if isinstance(nlane, str) and nlane not in lane_set:
            errors.append(f"nodes[{i}].lane references missing lane: {nlane!r}")

        col = node.get("col")
        if "col" in node and (isinstance(col, bool) or not isinstance(col, int)):
            errors.append(f"nodes[{i}].col must be an integer")

    if len(node_ids) != len(set(node_ids)):
        errors.append("node ids must be unique")
    node_set = set(node_ids)

    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f"edges[{i}] must be an object")
            continue
        for req in ("from", "to"):
            if req not in edge:
                errors.append(f"edges[{i}].{req} is required")
        fr = edge.get("from")
        to = edge.get("to")
        if isinstance(fr, str) and fr not in node_set:
            errors.append(f"edges[{i}].from references missing node: {fr!r}")
        if isinstance(to, str) and to not in node_set:
            errors.append(f"edges[{i}].to references missing node: {to!r}")

    return errors


def atomic_write(path: Path, doc: dict[str, Any], trailing_newline: bool) -> None:
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    if trailing_newline:
        if not text.endswith("\n"):
            text += "\n"
    else:
        text = text.rstrip("\n")

    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_system_temp_json(doc: dict[str, Any], trailing_newline: bool) -> str:
    """Write candidate IR to a system-temp file; caller must delete."""
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    if trailing_newline:
        if not text.endswith("\n"):
            text += "\n"
    else:
        text = text.rstrip("\n")
    fd, tmp_name = tempfile.mkstemp(prefix=f"{APP}-cand-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return tmp_name


def run_archify(archify: str, argv: list[str], timeout: float = 120.0) -> tuple[Any, str, str]:
    """Run `node <archify> ...`. Returns (parsed_stdout_json_or_None, stdout, stderr)."""
    cmd = ["node", archify, *argv]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else f"timeout after {timeout}s"
        return None, out, err
    except OSError as e:
        return None, "", str(e)

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    parsed: Any = None
    stripped = stdout.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
    return parsed, stdout, stderr


def format_validate_errors(receipt: dict[str, Any] | None, stderr: str) -> list[str]:
    """Build human-readable errors from a failed validate receipt or non-JSON stdout."""
    if receipt is None or not isinstance(receipt, dict):
        tail = (stderr or "").strip()
        if len(tail) > 800:
            tail = tail[-800:]
        msg = "validate output not JSON"
        if tail:
            msg = f"{msg}: {tail}"
        return [msg]

    errors: list[str] = []
    diags = receipt.get("diagnostics")
    if isinstance(diags, list) and diags:
        first = diags[0]
        if isinstance(first, dict):
            message = first.get("message")
            if message:
                errors.append(str(message))
            fixes = first.get("supportedFixes")
            if isinstance(fixes, list) and fixes:
                errors.append(f"suggested fix: {fixes[0]}")
        errors.append(f"diagnostics: {len(diags)}")
    err = receipt.get("error")
    if isinstance(err, str) and err.strip() and not errors:
        errors.append(err.strip().splitlines()[0])
    if not errors:
        errors.append("validate failed")
    return errors


def deliver_preview(archify: str, diagram_type: str, ir_path: Path) -> tuple[bytes | None, str | None]:
    """Deliver IR to system-temp HTML; return (html_bytes_or_None, note_or_None)."""
    fd, html_tmp = tempfile.mkstemp(prefix=f"{APP}-prev-", suffix=".html")
    os.close(fd)
    try:
        parsed, _stdout, stderr = run_archify(
            archify,
            ["deliver", diagram_type, str(ir_path), html_tmp, "--json"],
        )
        note: str | None = None
        if not isinstance(parsed, dict) or not parsed.get("ok", False):
            if isinstance(parsed, dict):
                diags = parsed.get("diagnostics")
                if isinstance(diags, list) and diags and isinstance(diags[0], dict):
                    note = str(diags[0].get("message") or "deliver failed")
                elif parsed.get("error"):
                    note = str(parsed["error"]).splitlines()[0]
                else:
                    note = "deliver failed"
            else:
                tail = (stderr or "").strip()
                note = "deliver output not JSON" + (f": {tail[-400:]}" if tail else "")
            return None, note

        try:
            html = Path(html_tmp).read_bytes()
        except OSError as e:
            return None, f"deliver ok but could not read HTML: {e}"
        return html, None
    finally:
        try:
            os.unlink(html_tmp)
        except OSError:
            pass


def export_html_path(ir_path: Path) -> Path:
    """Sibling .html path: strip .workflow.json else .json, then + .html."""
    name = ir_path.name
    if name.endswith(".workflow.json"):
        stem = name[: -len(".workflow.json")]
    elif name.endswith(".json"):
        stem = name[: -len(".json")]
    else:
        stem = ir_path.stem
    return ir_path.parent / f"{stem}.html"


def format_deliver_errors(receipt: dict[str, Any] | None, stderr: str) -> list[str]:
    """Human-readable errors from a failed deliver --json receipt."""
    if receipt is None or not isinstance(receipt, dict):
        tail = (stderr or "").strip()
        if len(tail) > 800:
            tail = tail[-800:]
        msg = "deliver output not JSON"
        if tail:
            msg = f"{msg}: {tail}"
        return [msg]
    errors: list[str] = []
    diags = receipt.get("diagnostics")
    if isinstance(diags, list) and diags:
        first = diags[0]
        if isinstance(first, dict):
            message = first.get("message")
            if message:
                errors.append(str(message))
        errors.append(f"diagnostics: {len(diags)}")
    err = receipt.get("error")
    if isinstance(err, str) and err.strip() and not errors:
        errors.append(err.strip().splitlines()[0])
    if not errors:
        errors.append("deliver failed")
    return errors


def deliver_to_path(
    archify: str, diagram_type: str, ir_path: Path, out_path: Path
) -> dict[str, Any]:
    """Deliver IR to out_path via sibling tmp + os.replace. No lock around subprocess.

    On failure leaves any prior out_path untouched. Returns
    {ok, output, note?} or {ok:false, errors}.
    """
    parent = out_path.parent
    # Archify requires the deliver target to end in .html (rejects bare .tmp).
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out_path.stem}.", suffix=".html", dir=str(parent)
    )
    os.close(fd)
    try:
        parsed, _stdout, stderr = run_archify(
            archify,
            ["deliver", diagram_type, str(ir_path), tmp_name, "--json"],
        )
        if not isinstance(parsed, dict) or not parsed.get("ok", False):
            return {
                "ok": False,
                "errors": format_deliver_errors(
                    parsed if isinstance(parsed, dict) else None, stderr
                ),
            }
        os.replace(tmp_name, out_path)
        receipt: dict[str, Any] = {"ok": True, "output": str(out_path)}
        note = parsed.get("note") if isinstance(parsed, dict) else None
        if isinstance(note, str) and note.strip():
            receipt["note"] = note.strip()
        return receipt
    except OSError as e:
        return {"ok": False, "errors": [f"export write failed: {e}"]}
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def extract_diagram_html(preview: bytes) -> bytes | None:
    """Build a minimal same-origin diagram doc: page <style> blocks + <svg>, no scripts."""
    svg_m = _SVG_RE.search(preview)
    if not svg_m:
        return None
    parts: list[bytes] = [
        b"<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">\n",
    ]
    parts.extend(_STYLE_BLOCK_RE.findall(preview))
    parts.append(b"\n</head><body style=\"margin:0;background:#0b0f14;\">\n")
    parts.append(svg_m.group(0))
    parts.append(b"\n</body></html>\n")
    return b"".join(parts)


def set_preview_html(html: bytes | None) -> None:
    """Update in-memory preview and re-extract the cached diagram fragment."""
    global _preview_html, _diagram_html
    _preview_html = html
    _diagram_html = extract_diagram_html(html) if html else None


def fetch_layout(
    archify: str, diagram_type: str, ir_path: Path
) -> tuple[dict[str, Any] | None, str]:
    """Run validate --layout-json; parse stdout JSON only (stderr tail on failure)."""
    parsed, stdout, stderr = run_archify(
        archify,
        ["validate", diagram_type, str(ir_path), "--layout-json"],
    )
    if isinstance(parsed, dict) and (
        "columns" in parsed or "viewBox" in parsed or "nodes" in parsed
    ):
        return parsed, ""
    tail = (stderr or stdout or "").strip()
    if len(tail) > 800:
        tail = tail[-800:]
    return None, tail or "layout-json stdout was not JSON"


def validate_candidate(
    archify: str, diagram_type: str, doc: dict[str, Any], trailing_newline: bool
) -> tuple[bool, list[str]]:
    """Write candidate to system temp, run validate --json (no --quality)."""
    tmp_name = _write_system_temp_json(doc, trailing_newline)
    try:
        return _validate_temp_path(archify, diagram_type, tmp_name)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _validate_temp_path(
    archify: str, diagram_type: str, tmp_name: str
) -> tuple[bool, list[str]]:
    """Run validate --json against an existing temp IR path."""
    parsed, _stdout, stderr = run_archify(
        archify,
        ["validate", diagram_type, tmp_name, "--json"],
    )
    if not isinstance(parsed, dict):
        return False, format_validate_errors(None, stderr)
    if parsed.get("ok") is True:
        return True, []
    return False, format_validate_errors(parsed, stderr)


def preview_candidate(
    archify: str, diagram_type: str, doc: dict[str, Any], trailing_newline: bool
) -> tuple[bool, list[str], bytes | None, str | None, dict[str, Any] | None]:
    """Validate + deliver + layout-json from a temp candidate (never touches the real file).

    Returns (ok, errors, html_or_None, note_or_None, layout_or_None).
    """
    tmp_name = _write_system_temp_json(doc, trailing_newline)
    try:
        ok_v, v_errors = _validate_temp_path(archify, diagram_type, tmp_name)
        if not ok_v:
            return False, v_errors, None, None, None
        html, note = deliver_preview(archify, diagram_type, Path(tmp_name))
        layout, layout_err = fetch_layout(archify, diagram_type, Path(tmp_name))
        if layout is None and not note:
            note = layout_err or "layout-json failed"
        return True, [], html, note, layout
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{APP}] " + (fmt % args) + "\n")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def _read_json_body(self) -> tuple[Any | None, str | None]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return None, f"invalid JSON body: {e}"

    def _handle_alive(self) -> None:
        """SSE presence. Held open while the tab exists; not an inflight request."""
        global _alive, _alive_seen, _last_zero_ts
        self.close_connection = True
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
            OSError,
        ):
            return
        with _alive_lock:
            _alive += 1
            _alive_seen = True
        try:
            # Comment first so EventSource opens before the first sleep. No retry: field.
            while True:
                self.wfile.write(b":\n\n")
                self.wfile.flush()
                time.sleep(ALIVE_KEEPALIVE_S)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
            OSError,
        ):
            return
        finally:
            with _alive_lock:
                _alive -= 1
                if _alive == 0:
                    _last_zero_ts = time.monotonic()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/alive":
            self._handle_alive()
            return
        _begin_request()
        try:
            if path == "/":
                html = SPA_HTML.encode("utf-8")
                self._send(200, html, "text/html; charset=utf-8")
                return
            if path == "/preview":
                with _state_lock:
                    body = _preview_html
                if body is None:
                    self._send_json(
                        200,
                        {
                            "ok": False,
                            "error": "no preview available (archify absent or deliver not yet run)",
                        },
                    )
                    return
                self._send(200, body, "text/html; charset=utf-8")
                return
            if path == "/api/diagram":
                with _state_lock:
                    body = _diagram_html
                    archify = _archify_path
                if archify is None:
                    self._send_json(404, {"archify": False})
                    return
                if body is None:
                    self._send_json(
                        404,
                        {"ok": False, "error": "no diagram fragment (deliver not yet run)"},
                    )
                    return
                self._send(200, body, "text/html; charset=utf-8")
                return
            if path == "/api/layout":
                # Layout from in-memory _doc (may differ from disk until Save).
                with _state_lock:
                    archify = _archify_path
                    dtype = _diagram_type
                    doc = _doc
                    trailing = _had_trailing_newline
                    has_file = _file_path is not None
                if archify is None or not has_file:
                    self._send_json(404, {"archify": False})
                    return
                tmp_name = _write_system_temp_json(doc, trailing)
                try:
                    layout, err = fetch_layout(archify, dtype, Path(tmp_name))
                finally:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                if layout is None:
                    self._send_json(
                        200,
                        {"ok": False, "error": err or "layout-json failed"},
                    )
                    return
                self._send_json(200, layout)
                return
            if path == "/api/state":
                with _state_lock:
                    payload = state_payload()
                self._send_json(200, payload)
                return
            self._send_json(404, {"ok": False, "error": "not found"})
        finally:
            _end_request()

    def do_POST(self) -> None:  # noqa: N802
        _begin_request()
        try:
            path = urlparse(self.path).path
            if path == "/api/pick":
                self._handle_pick()
                return
            if path == "/api/open":
                self._handle_open()
                return
            if path == "/api/preview":
                self._handle_preview()
                return
            if path == "/api/export":
                self._handle_export()
                return
            if path != "/api/save":
                self._send_json(404, {"ok": False, "error": "not found"})
                return

            with _state_lock:
                if _file_path is None:
                    self._send_json(
                        200,
                        {"ok": False, "saved": False, "errors": ["no file open"]},
                    )
                    return

            candidate, err = self._read_json_body()
            if err is not None:
                self._send_json(
                    200,
                    {"ok": False, "saved": False, "errors": [err]},
                )
                return

            if not isinstance(candidate, dict):
                self._send_json(
                    200,
                    {"ok": False, "saved": False, "errors": ["body must be a JSON object"]},
                )
                return

            errors = structural_check(candidate)
            if errors:
                self._send_json(200, {"ok": False, "saved": False, "errors": errors})
                return

            with _state_lock:
                if _file_path is None:
                    self._send_json(
                        200,
                        {"ok": False, "saved": False, "errors": ["no file open"]},
                    )
                    return
                archify = _archify_path
                trailing = _had_trailing_newline
                dtype = _diagram_type
                target = _file_path

            if archify:
                ok_v, v_errors = validate_candidate(archify, dtype, candidate, trailing)
                if not ok_v:
                    self._send_json(200, {"ok": False, "saved": False, "errors": v_errors})
                    return

            with _state_lock:
                if _file_path is None:
                    self._send_json(
                        200,
                        {"ok": False, "saved": False, "errors": ["no file open"]},
                    )
                    return
                try:
                    atomic_write(target, candidate, trailing)
                except OSError as e:
                    self._send_json(
                        200,
                        {"ok": False, "saved": False, "errors": [f"write failed: {e}"]},
                    )
                    return
                global _doc
                _doc = candidate

            receipt: dict[str, Any] = {"ok": True, "saved": True, "structural": True}
            if archify:
                html, note = deliver_preview(archify, dtype, target)
                with _state_lock:
                    if html is not None:
                        set_preview_html(html)
                if note:
                    receipt["note"] = note
                else:
                    receipt["preview"] = True

            self._send_json(200, receipt)
        finally:
            _end_request()

    def _handle_export(self) -> None:
        """Ensure-saved-then-export sibling .html (M19). Lock off archify subprocess."""
        global _doc

        with _state_lock:
            if _file_path is None:
                self._send_json(200, {"ok": False, "errors": ["no file open"]})
                return
            if _archify_path is None:
                self._send_json(
                    200,
                    {
                        "ok": False,
                        "errors": ["archify not available; cannot render HTML"],
                    },
                )
                return
            archify = _archify_path
            trailing = _had_trailing_newline
            dtype = _diagram_type
            target = _file_path

        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        # Dirty path: client sends the IR doc; clean path: empty / {} -> deliver from disk.
        stripped = raw.strip()
        if stripped and stripped != b"{}":
            try:
                candidate = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self._send_json(
                    200, {"ok": False, "errors": [f"invalid JSON body: {e}"]}
                )
                return
            if not isinstance(candidate, dict):
                self._send_json(
                    200, {"ok": False, "errors": ["body must be a JSON object"]}
                )
                return
            errors = structural_check(candidate)
            if errors:
                self._send_json(200, {"ok": False, "errors": errors})
                return
            ok_v, v_errors = validate_candidate(archify, dtype, candidate, trailing)
            if not ok_v:
                self._send_json(200, {"ok": False, "errors": v_errors})
                return
            with _state_lock:
                if _file_path is None:
                    self._send_json(200, {"ok": False, "errors": ["no file open"]})
                    return
                try:
                    atomic_write(target, candidate, trailing)
                except OSError as e:
                    self._send_json(
                        200, {"ok": False, "errors": [f"write failed: {e}"]}
                    )
                    return
                _doc = candidate

        out_path = export_html_path(target)
        # Lock OFF during deliver (same as save/preview).
        receipt = deliver_to_path(archify, dtype, target, out_path)
        self._send_json(200, receipt)

    def _handle_preview(self) -> None:
        """Validate + deliver from a temp candidate; never write the real file (M13)."""
        global _doc

        with _state_lock:
            if _file_path is None:
                self._send_json(
                    200,
                    {
                        "ok": False,
                        "preview": False,
                        "saved": False,
                        "errors": ["no file open"],
                    },
                )
                return

        candidate, err = self._read_json_body()
        if err is not None:
            self._send_json(
                200,
                {"ok": False, "preview": False, "saved": False, "errors": [err]},
            )
            return

        if not isinstance(candidate, dict):
            self._send_json(
                200,
                {
                    "ok": False,
                    "preview": False,
                    "saved": False,
                    "errors": ["body must be a JSON object"],
                },
            )
            return

        errors = structural_check(candidate)
        if errors:
            self._send_json(
                200,
                {"ok": False, "preview": False, "saved": False, "errors": errors},
            )
            return

        with _state_lock:
            if _file_path is None:
                self._send_json(
                    200,
                    {
                        "ok": False,
                        "preview": False,
                        "saved": False,
                        "errors": ["no file open"],
                    },
                )
                return
            archify = _archify_path
            trailing = _had_trailing_newline
            dtype = _diagram_type

        if not archify:
            with _state_lock:
                _doc = candidate
            self._send_json(
                200,
                {
                    "ok": True,
                    "preview": True,
                    "saved": False,
                    "structural": True,
                },
            )
            return

        ok_p, v_errors, html, note, layout = preview_candidate(
            archify, dtype, candidate, trailing
        )
        if not ok_p:
            self._send_json(
                200,
                {
                    "ok": False,
                    "preview": False,
                    "saved": False,
                    "errors": v_errors,
                },
            )
            return

        with _state_lock:
            _doc = candidate
            if html is not None:
                set_preview_html(html)

        receipt: dict[str, Any] = {
            "ok": True,
            "preview": True,
            "saved": False,
            "structural": True,
        }
        if note:
            receipt["note"] = note
        if layout is not None:
            receipt["layout"] = layout
        self._send_json(200, receipt)

    def _handle_pick(self) -> None:
        """Native OS Open-file dialog via tkinter subprocess (M12)."""
        with _state_lock:
            current = _file_path
        if current is not None:
            initial_dir = str(current.parent)
        else:
            initial_dir = str(Path.cwd())
        try:
            result = native_pick_path(initial_dir)
        except Exception:
            result = {"ok": False, "error": "native picker unavailable"}
        self._send_json(200, result)

    def _handle_open(self) -> None:
        """Switch the active diagram file (M10). Keeps current file on any failure."""
        global _file_path, _diagram_type, _doc, _had_trailing_newline

        body, err = self._read_json_body()
        if err is not None:
            self._send_json(400, {"ok": False, "error": err})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "body must be a JSON object"})
            return
        raw_path = body.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            self._send_json(400, {"ok": False, "error": "path is required"})
            return
        try:
            target = Path(raw_path).resolve()
        except (OSError, RuntimeError) as e:
            self._send_json(400, {"ok": False, "error": f"bad path: {e}"})
            return
        if not target.is_file():
            self._send_json(400, {"ok": False, "error": f"file not found: {target}"})
            return
        try:
            raw_text = target.read_text(encoding="utf-8")
        except OSError as e:
            self._send_json(400, {"ok": False, "error": f"cannot read file: {e}"})
            return
        trailing = raw_text.endswith("\n")
        try:
            doc = json.loads(raw_text)
        except json.JSONDecodeError as e:
            self._send_json(400, {"ok": False, "error": f"invalid JSON: {e}"})
            return
        if not isinstance(doc, dict):
            self._send_json(400, {"ok": False, "error": "IR root must be a JSON object"})
            return
        dtype = doc.get("diagram_type")
        if not isinstance(dtype, str) or not dtype.strip():
            self._send_json(400, {"ok": False, "error": "missing diagram_type"})
            return
        if dtype != "workflow":
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": f"{dtype} diagrams are not supported yet - workflow only",
                },
            )
            return

        with _state_lock:
            archify = _archify_path
            _file_path = target
            _diagram_type = dtype
            _doc = doc
            _had_trailing_newline = trailing
            set_preview_html(None)

        if archify:
            html, _note = deliver_preview(archify, dtype, target)
            with _state_lock:
                if html is not None:
                    set_preview_html(html)
                else:
                    set_preview_html(None)

        with _state_lock:
            payload = state_payload()
        self._send_json(200, payload)


# ---------------------------------------------------------------------------
# Inline SPA
# ---------------------------------------------------------------------------
SPA_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bendwright</title>
<style>
:root {
  --bg: #0f1419;
  --panel: #1a2332;
  --border: #2d3a4d;
  --text: #e7ecf3;
  --muted: #8b9bb4;
  --accent: #3d8bfd;
  --danger: #e35d6a;
  --ok: #3dd68c;
  --input: #0d1117;
  --row-hover: #243044;
  --tab: #151c27;
  --focus: #5b9cff;
  font-family: "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  height: 100vh; display: flex; flex-direction: column;
}
header {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 16px; border-bottom: 1px solid var(--border);
  background: var(--panel); flex-wrap: wrap;
}
header h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: 0.02em; }
header .meta { color: var(--muted); font-size: 12px; flex: 1; min-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.toolbar { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
button, .btn {
  background: var(--tab); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 10px; font-size: 12px; cursor: pointer;
}
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.danger { border-color: var(--danger); color: var(--danger); }
button:disabled { opacity: 0.4; cursor: not-allowed; }
#status-bar {
  position: relative;
  height: 44px; box-sizing: border-box; flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
#status {
  font-size: 12px; padding: 6px 16px; padding-right: 96px;
  height: 44px; box-sizing: border-box; overflow: hidden;
  color: var(--muted); white-space: pre-wrap;
}
#status.ok { color: var(--ok); }
#status.err { color: var(--danger); }
#status.status-clipped { cursor: pointer; }
#status-more {
  display: none;
  position: absolute;
  right: 10px; top: 50%; transform: translateY(-50%);
  z-index: 1;
  font-size: 11px; padding: 2px 8px;
}
#status-bar.status-clipped #status-more { display: inline-block; }
#status-overlay {
  display: none;
  position: fixed;
  z-index: 50;
  top: 56px;
  left: 50%;
  transform: translateX(-50%);
  width: min(520px, calc(100vw - 24px));
  max-height: 40vh;
  overflow: auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 12px 36px rgba(0,0,0,0.55);
  padding: 12px 14px;
}
#status-overlay.active { display: block; }
#status-overlay.ok { border-color: var(--ok); }
#status-overlay.err { border-color: var(--danger); }
#status-overlay .open-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px; font-size: 13px; font-weight: 600;
}
#status-overlay .status-overlay-body {
  margin: 0; font-size: 12px; line-height: 1.45;
  white-space: pre-wrap; font-family: inherit; color: var(--text);
}
#status-overlay.ok .status-overlay-body { color: var(--ok); }
#status-overlay.err .status-overlay-body { color: var(--danger); }
.tabs {
  display: flex; gap: 2px; align-items: center; padding: 8px 16px 0; background: var(--bg);
  border-bottom: 1px solid var(--border); min-height: 40px; box-sizing: border-box;
}
.tabs button {
  border-radius: 6px 6px 0 0; border-bottom: none;
  background: var(--tab); padding: 8px 14px; flex-shrink: 0;
}
.tabs button.active { background: var(--panel); color: #fff; border-color: var(--border); }
#layout-hint {
  flex: 1; min-width: 0; margin-left: auto;
  font-size: 12px; color: var(--muted);
  text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  padding: 4px 8px; border-radius: 4px; box-sizing: border-box;
}
main { flex: 1; overflow: hidden; display: flex; background: var(--panel); }
.pane { display: none; flex: 1; overflow: hidden; }
.pane.active { display: flex; }
.split { display: flex; flex: 1; overflow: hidden; }
.list {
  width: 260px; min-width: 180px; border-right: 1px solid var(--border);
  overflow: auto; background: var(--tab);
}
.list-item {
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  cursor: pointer; font-size: 12px;
}
.list-item:hover { background: var(--row-hover); }
.list-item.active { background: var(--row-hover); border-left: 3px solid var(--accent); }
.list-item .sub { color: var(--muted); font-size: 11px; }
.form {
  flex: 1; overflow: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px;
}
.form h2 { margin: 0 0 4px; font-size: 14px; }
.field { display: flex; flex-direction: column; gap: 4px; max-width: 420px; }
.field label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.field input, .field select, .field textarea {
  background: var(--input); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 7px 9px; font-size: 13px; font-family: inherit;
}
.field input:focus, .field select:focus, .field textarea:focus {
  outline: none; border-color: var(--focus);
}
.row-actions { display: flex; gap: 8px; margin-top: 8px; }
#raw-wrap { flex: 1; display: flex; flex-direction: column; padding: 12px 16px; gap: 8px; }
#raw-editor {
  flex: 1; width: 100%; resize: none; background: var(--input); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px; padding: 10px;
  font-family: ui-monospace, Consolas, monospace; font-size: 12px; line-height: 1.4;
}
#layout-wrap {
  flex: 1; display: flex; flex-direction: column; min-height: 0; padding: 0;
  position: relative;
}
#layout-node-editor {
  display: none;
  position: absolute;
  z-index: 30;
  margin: 0;
  min-width: 220px;
  background: var(--panel);
  color: var(--text);
  border: 2px solid var(--accent);
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 13px;
  font-family: inherit;
  box-shadow: 0 8px 28px rgba(0,0,0,0.55);
  box-sizing: border-box;
}
#layout-node-editor.active { display: block; }
#layout-node-editor .bw-ed-field {
  display: flex; flex-direction: column; gap: 3px; margin-bottom: 8px;
}
#layout-node-editor .bw-ed-field label {
  font-size: 11px; color: var(--muted); font-weight: 600; letter-spacing: 0.02em;
}
#layout-node-editor .bw-ed-field input,
#layout-node-editor .bw-ed-field select {
  background: var(--input); color: var(--text); border: 1px solid var(--border);
  border-radius: 4px; padding: 5px 7px; font-size: 13px; font-family: inherit;
}
#layout-node-editor .bw-ed-field input:focus,
#layout-node-editor .bw-ed-field select:focus {
  outline: none; border-color: var(--focus);
}
#layout-node-editor .bw-ed-field input:disabled,
#layout-node-editor .bw-ed-field select:disabled {
  opacity: 0.65; cursor: not-allowed;
}
#layout-node-editor .bw-ed-hint {
  font-size: 11px; color: var(--muted); margin-top: 2px;
}
#layout-node-editor .bw-ed-actions {
  display: flex; align-items: center; gap: 10px; margin-top: 4px;
}
#layout-node-editor .bw-ed-actions .meta {
  font-size: 11px; color: var(--muted);
}
#layout-single-editor {
  display: none;
  position: absolute;
  z-index: 30;
  margin: 0;
  min-width: 180px;
  background: var(--panel);
  color: var(--text);
  border: 2px solid var(--accent);
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 13px;
  font-family: inherit;
  box-shadow: 0 8px 28px rgba(0,0,0,0.55);
  box-sizing: border-box;
}
#layout-single-editor.active { display: block; }
#layout-single-editor .bw-ed-field {
  display: flex; flex-direction: column; gap: 3px; margin-bottom: 8px;
}
#layout-single-editor .bw-ed-field label {
  font-size: 11px; color: var(--muted); font-weight: 600; letter-spacing: 0.02em;
}
#layout-single-editor .bw-ed-field input {
  background: var(--input); color: var(--text); border: 1px solid var(--border);
  border-radius: 4px; padding: 5px 7px; font-size: 13px; font-family: inherit;
}
#layout-single-editor .bw-ed-field input:focus {
  outline: none; border-color: var(--focus);
}
#layout-single-editor .bw-ed-actions {
  display: flex; align-items: center; gap: 10px; margin-top: 4px;
}
#layout-single-editor .bw-ed-actions .meta {
  font-size: 11px; color: var(--muted);
}
#layout-toolbar {
  display: flex; gap: 8px; align-items: center; padding: 8px 12px;
  border-bottom: 1px solid var(--border); font-size: 12px; color: var(--muted);
  flex-wrap: wrap;
}
#layout-zoom-controls {
  display: flex; gap: 4px; align-items: center; margin-left: auto;
}
#layout-zoom-pct {
  min-width: 3.5em; text-align: center; color: var(--text); font-variant-numeric: tabular-nums;
}
#layout-mode-toggle, #layout-quality-toggle {
  display: flex; gap: 4px; align-items: center;
}
#layout-mode-toggle button.mode-active,
#layout-quality-toggle button.mode-active {
  border-color: var(--accent); color: var(--accent); background: #1a2332;
}
#layout-quality-toggle button:disabled {
  opacity: 0.55; cursor: wait;
}
#layout-frame {
  flex: 1; width: 100%; border: 0; background: #0b0f14; min-height: 0;
}
#layout-hint.layout-hint-active {
  color: var(--accent);
  background: #1a2332;
  box-shadow: inset 0 0 0 1px var(--accent);
  font-weight: 600;
}
.tabs button.hidden { display: none; }
.empty { color: var(--muted); padding: 24px; font-size: 13px; }
#open-panel {
  display: none;
  position: absolute;
  z-index: 40;
  top: 48px;
  right: 16px;
  width: min(440px, calc(100vw - 24px));
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 12px 36px rgba(0,0,0,0.55);
  padding: 12px 14px;
}
#open-panel.active { display: block; }
#open-panel .open-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px; font-size: 13px; font-weight: 600;
}
#open-panel .field { max-width: none; margin-bottom: 8px; }
#open-panel .row-actions { margin-top: 0; }
#dirty-panel {
  display: none;
  position: fixed;
  z-index: 60;
  top: 56px;
  left: 50%;
  transform: translateX(-50%);
  width: min(420px, calc(100vw - 24px));
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 12px 36px rgba(0,0,0,0.55);
  padding: 12px 14px;
}
#dirty-panel.active { display: block; }
#dirty-panel .open-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px; font-size: 13px; font-weight: 600;
}
#dirty-panel .dirty-msg { font-size: 12px; color: var(--muted); margin-bottom: 10px; }
button.primary.dirty-emphasis { box-shadow: 0 0 0 2px rgba(61,139,253,0.45); }
</style>
</head>
<body>
<header>
  <h1>bendwright</h1>
  <div class="meta" id="file-meta">loading…</div>
  <div class="toolbar">
    <button type="button" id="btn-open" title="Open another diagram">Open</button>
    <button type="button" id="btn-undo" title="Ctrl+Z">Undo</button>
    <button type="button" id="btn-redo" title="Ctrl+Y">Redo</button>
    <button type="button" id="btn-discard" title="Reload from disk" disabled>Discard</button>
    <button type="button" class="primary" id="btn-save" title="Ctrl+S">Save</button>
    <button type="button" id="btn-export" title="Export rendered HTML beside the JSON" disabled>Export HTML</button>
  </div>
</header>
<div id="open-panel" aria-hidden="true">
  <div class="open-head">
    <span>Open diagram</span>
    <button type="button" id="btn-open-close" title="Close">Close</button>
  </div>
  <div class="row-actions" style="margin-bottom:10px">
    <button type="button" class="primary" id="btn-open-browse" title="Native file picker">Browse…</button>
  </div>
  <div class="field">
    <label for="open-path-input">Absolute path</label>
    <input type="text" id="open-path-input" placeholder="C:\path\to\file.workflow.json" spellcheck="false">
  </div>
  <div class="row-actions">
    <button type="button" id="btn-open-go">Open</button>
  </div>
</div>
<div id="dirty-panel" aria-hidden="true">
  <div class="open-head">
    <span>Unsaved changes</span>
    <button type="button" id="btn-dirty-close" title="Cancel">Close</button>
  </div>
  <div class="dirty-msg">Save, discard, or cancel before continuing.</div>
  <div class="row-actions">
    <button type="button" class="primary" id="btn-dirty-save">Save</button>
    <button type="button" id="btn-dirty-discard">Discard</button>
    <button type="button" id="btn-dirty-cancel">Cancel</button>
  </div>
</div>
<div id="status-overlay" role="dialog" aria-label="Full status message" aria-hidden="true">
  <div class="open-head">
    <span>Status</span>
    <button type="button" id="btn-status-close" title="Close">Close</button>
  </div>
  <pre id="status-overlay-body" class="status-overlay-body"></pre>
</div>
<div id="status-bar">
  <div id="status">Ready</div>
  <button type="button" id="status-more" title="Show full message" aria-hidden="true">Show more</button>
</div>
<div class="tabs" role="tablist">
  <button type="button" data-tab="layout" id="tab-layout" class="hidden">Layout</button>
  <button type="button" class="active" data-tab="nodes">Nodes</button>
  <button type="button" data-tab="edges">Edges</button>
  <button type="button" data-tab="lanes">Lanes</button>
  <button type="button" data-tab="raw">Raw JSON</button>
  <span id="layout-hint" title="Drag a node; drop snaps to nearest lane + column (preview; unsaved until Save). Use Connect / Edit to add or change connections.">Drag a node; drop snaps to nearest lane + column (preview; unsaved until Save). Use Connect / Edit to add or change connections.</span>
</div>
<main>
  <div class="pane" id="pane-layout">
    <div id="layout-wrap">
      <div id="layout-toolbar">
        <div id="layout-mode-toggle">
          <button type="button" id="btn-mode-move" class="mode-active" title="Move nodes / rename">Move</button>
          <button type="button" id="btn-mode-connect" title="Connect / edit: click a source node then a target to add; drag endpoints to reroute; click an edge to select or delete.">Connect / Edit</button>
        </div>
        <div id="layout-quality-toggle" title="meta.quality_profile (Archify default: standard)">
          <button type="button" id="btn-quality-standard" class="mode-active">standard</button>
          <button type="button" id="btn-quality-showcase">showcase</button>
        </div>
        <button type="button" id="btn-add-node" title="Add node at first free cell">+ Node</button>
        <button type="button" id="btn-delete-edge" disabled title="Delete selected edge">Delete edge</button>
        <div id="layout-zoom-controls">
          <button type="button" id="btn-zoom-fit" title="Fit to window">Fit</button>
          <button type="button" id="btn-zoom-out" title="Zoom out">−</button>
          <span id="layout-zoom-pct">—</span>
          <button type="button" id="btn-zoom-in" title="Zoom in">+</button>
          <button type="button" id="btn-zoom-100" title="100%">100%</button>
        </div>
        <a href="/preview" target="_blank" rel="noopener" style="color:var(--accent)">Open full preview</a>
      </div>
      <iframe id="layout-frame" title="Layout diagram"></iframe>
      <div id="layout-node-editor" role="dialog" aria-label="Edit node text">
        <div class="bw-ed-field">
          <label for="layout-edit-type">Type</label>
          <select id="layout-edit-type"></select>
        </div>
        <div class="bw-ed-field">
          <label for="layout-edit-label">Label</label>
          <input type="text" id="layout-edit-label" autocomplete="off" spellcheck="false">
        </div>
        <div class="bw-ed-field">
          <label for="layout-edit-sublabel">Sublabel</label>
          <input type="text" id="layout-edit-sublabel" autocomplete="off" spellcheck="false">
        </div>
        <div class="bw-ed-field">
          <label for="layout-edit-tag">Tag</label>
          <input type="text" id="layout-edit-tag" autocomplete="off" spellcheck="false">
        </div>
        <div class="bw-ed-field">
          <label for="layout-edit-brand">Brand</label>
          <input type="text" id="layout-edit-brand" autocomplete="off" spellcheck="false">
          <div id="layout-edit-brand-hint" class="bw-ed-hint" hidden></div>
        </div>
        <div class="bw-ed-actions">
          <button type="button" class="primary" id="layout-edit-save">Save</button>
          <button type="button" id="layout-edit-duplicate" disabled title="Clone node to a free cell">Duplicate</button>
          <button type="button" id="layout-edit-delete" disabled title="Delete node and its edges">Delete</button>
          <span class="meta">Enter=save · Esc=cancel</span>
        </div>
      </div>
      <div id="layout-single-editor" role="dialog" aria-label="Edit label">
        <div class="bw-ed-field">
          <label for="layout-single-label" id="layout-single-label-caption">Label</label>
          <input type="text" id="layout-single-label" autocomplete="off" spellcheck="false">
        </div>
        <div class="bw-ed-actions">
          <button type="button" class="primary" id="layout-single-save">Save</button>
          <span class="meta">Enter=save · Esc=cancel</span>
        </div>
      </div>
    </div>
  </div>
  <div class="pane active" id="pane-nodes">
    <div class="split">
      <div class="list" id="list-nodes"></div>
      <div class="form" id="form-nodes"><div class="empty">Select a node</div></div>
    </div>
  </div>
  <div class="pane" id="pane-edges">
    <div class="split">
      <div class="list" id="list-edges"></div>
      <div class="form" id="form-edges"><div class="empty">Select an edge</div></div>
    </div>
  </div>
  <div class="pane" id="pane-lanes">
    <div class="split">
      <div class="list" id="list-lanes"></div>
      <div class="form" id="form-lanes"><div class="empty">Select a lane</div></div>
    </div>
  </div>
  <div class="pane" id="pane-raw">
    <div id="raw-wrap">
      <div class="row-actions">
        <button type="button" class="primary" id="btn-raw-apply">Apply JSON</button>
        <span class="meta" style="font-size:12px;color:var(--muted)">Edits apply on blur or Apply (must be valid JSON object)</span>
      </div>
      <textarea id="raw-editor" spellcheck="false"></textarea>
    </div>
  </div>
</main>
<script>
(function () {
  "use strict";

  var state = {
    file: "",
    diagram_type: "workflow",
    doc: null,
    enums: {},
    archify: null,
    selected: { nodes: -1, edges: -1, lanes: -1 },
    tab: "nodes",
    undo: [],
    redo: [],
    rawDirty: false,
    dirty: false,
    lastSavedDoc: null,
    layout: null,
    layoutBusy: false,
    layoutLoaded: false,
    layoutZoom: null,
    layoutMode: "move",
    connectFrom: null,
    selectedEdgeIndex: null,
  };

  var MAX_HISTORY = 100;
  var LAYOUT_DRAG_THRESHOLD_PX = 4;
  var LAYOUT_LANE_HIT_H = 26;
  var LAYOUT_LANE_HIT_W_MAX = 280;
  var LAYOUT_ENDPOINT_R = 7;
  var layoutDrag = null;
  var endpointDrag = null;
  var nodeEdit = null;
  var singleEdit = null;
  var pendingDirtyAction = null;
  var statusKind = "";

  function $(id) { return document.getElementById(id); }

  function isStatusOverlayActive() {
    var panel = $("status-overlay");
    return !!(panel && panel.classList.contains("active"));
  }

  function closeStatusOverlay() {
    var panel = $("status-overlay");
    if (!panel) return;
    panel.classList.remove("active");
    panel.setAttribute("aria-hidden", "true");
  }

  function openStatusOverlay() {
    var el = $("status");
    var bar = $("status-bar");
    var panel = $("status-overlay");
    var body = $("status-overlay-body");
    if (!el || !panel || !body) return;
    if (!bar || !bar.classList.contains("status-clipped")) return;
    body.textContent = el.title || el.textContent || "";
    panel.classList.remove("ok", "err");
    if (statusKind === "ok" || statusKind === "err") panel.classList.add(statusKind);
    panel.classList.add("active");
    panel.setAttribute("aria-hidden", "false");
  }

  function measureStatusClip() {
    var el = $("status");
    var bar = $("status-bar");
    var more = $("status-more");
    if (!el || !bar) return;
    var clipped = el.scrollHeight > el.clientHeight + 1;
    if (clipped) {
      el.classList.add("status-clipped");
      bar.classList.add("status-clipped");
      if (more) more.setAttribute("aria-hidden", "false");
    } else {
      el.classList.remove("status-clipped");
      bar.classList.remove("status-clipped");
      if (more) more.setAttribute("aria-hidden", "true");
      closeStatusOverlay();
    }
  }

  var SERVER_GONE_MSG = "bendwright server stopped responding - relaunch bendwright to continue (your saved file is safe).";

  function setStatus(msg, kind) {
    var text = msg == null ? "" : String(msg);
    if (/Failed to fetch/i.test(text)) {
      text = SERVER_GONE_MSG;
      kind = "err";
    }
    var el = $("status");
    statusKind = kind || "";
    el.textContent = text || "";
    el.title = text || "";
    el.className = statusKind;
    var bar = $("status-bar");
    if (bar) bar.classList.remove("status-clipped");
    var more = $("status-more");
    if (more) more.setAttribute("aria-hidden", "true");
    closeStatusOverlay();
    requestAnimationFrame(function () {
      requestAnimationFrame(measureStatusClip);
    });
  }

  function isServerGoneError(e) {
    if (!e) return false;
    if (e.bendwrightOffline) return true;
    var msg = String(e.message || e);
    return /Failed to fetch|NetworkError when attempting to fetch|Load failed/i.test(msg);
  }

  function apiFetch(url, opts) {
    return window["fetch"](url, opts).catch(function (e) {
      if (!isServerGoneError(e)) throw e;
      var gone = new TypeError("Failed to fetch");
      gone.bendwrightOffline = true;
      setStatus(SERVER_GONE_MSG, "err");
      throw gone;
    });
  }

  function clone(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  function docsEqual(a, b) {
    if (a == null && b == null) return true;
    if (a == null || b == null) return false;
    return JSON.stringify(a) === JSON.stringify(b);
  }

  function updateDirtyUI() {
    var meta = $("file-meta");
    var saveBtn = $("btn-save");
    var discardBtn = $("btn-discard");
    var exportBtn = $("btn-export");
    var addBtn = $("btn-add-node");
    if (!state.file) {
      if (meta) meta.textContent = "No file open";
      if (saveBtn) {
        saveBtn.textContent = "Save";
        saveBtn.classList.remove("dirty-emphasis");
        saveBtn.disabled = true;
      }
      if (discardBtn) discardBtn.disabled = true;
      if (exportBtn) {
        exportBtn.disabled = true;
        exportBtn.title = "open a file first";
      }
      if (addBtn) addBtn.disabled = true;
      return;
    }
    var base = state.file + " · " + state.diagram_type +
      " · nodes " + ((state.doc && state.doc.nodes) || []).length +
      " / edges " + ((state.doc && state.doc.edges) || []).length +
      " / lanes " + ((state.doc && state.doc.lanes) || []).length;
    if (state.dirty) base += " · unsaved changes";
    if (meta) meta.textContent = base;
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = state.dirty ? "Save*" : "Save";
      saveBtn.classList.toggle("dirty-emphasis", !!state.dirty);
    }
    if (discardBtn) discardBtn.disabled = !state.dirty || !!state.layoutBusy;
    if (exportBtn) {
      if (state.archify) {
        exportBtn.disabled = false;
        exportBtn.title = "Export rendered HTML beside the JSON";
      } else {
        exportBtn.disabled = true;
        exportBtn.title = "archify not available; cannot render HTML";
      }
    }
    if (addBtn) addBtn.disabled = !state.doc || !!state.layoutBusy;
  }

  function markDirty() {
    state.dirty = true;
    updateDirtyUI();
  }

  function clearDirty() {
    state.dirty = false;
    state.lastSavedDoc = state.doc ? clone(state.doc) : null;
    updateDirtyUI();
  }

  function syncDirtyFromDoc() {
    state.dirty = !docsEqual(state.doc, state.lastSavedDoc);
    updateDirtyUI();
  }

  function pushHistory() {
    if (!state.doc) return;
    state.undo.push(clone(state.doc));
    if (state.undo.length > MAX_HISTORY) state.undo.shift();
    state.redo = [];
    updateHistoryButtons();
  }

  function updateHistoryButtons() {
    $("btn-undo").disabled = state.undo.length === 0;
    $("btn-redo").disabled = state.redo.length === 0;
  }

  function remountAfterBufferChange(statusMsg, statusKind) {
    syncDirtyFromDoc();
    renderAll();
    var baseMsg = statusMsg || "updated";
    if (state.dirty) baseMsg += " (unsaved)";
    if (!state.archify) {
      setStatus(baseMsg, statusKind || "");
      return Promise.resolve();
    }
    setLayoutBusy(true);
    setStatus("previewing…", "");
    return postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          var msg = baseMsg;
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, statusKind || "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
            syncDirtyFromDoc();
          });
        }
        setLayoutBusy(false);
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("Preview failed:\n- " + errs.join("\n- "), "err");
      })
      .catch(function (e) {
        setLayoutBusy(false);
        setStatus("Preview failed: " + e, "err");
      });
  }

  function undo() {
    if (!state.undo.length || state.layoutBusy) return;
    state.redo.push(clone(state.doc));
    state.doc = state.undo.pop();
    state.rawDirty = false;
    updateHistoryButtons();
    remountAfterBufferChange("Undid", "");
  }

  function redo() {
    if (!state.redo.length || state.layoutBusy) return;
    state.undo.push(clone(state.doc));
    state.doc = state.redo.pop();
    state.rawDirty = false;
    updateHistoryButtons();
    remountAfterBufferChange("Redid", "");
  }

  function ensureArrays() {
    if (!state.doc) return;
    if (!Array.isArray(state.doc.nodes)) state.doc.nodes = [];
    if (!Array.isArray(state.doc.edges)) state.doc.edges = [];
    if (!Array.isArray(state.doc.lanes)) state.doc.lanes = [];
  }

  function enumOptions(key, current) {
    var vals = (state.enums && state.enums[key]) ? state.enums[key].slice() : [];
    if (current != null && current !== "" && vals.indexOf(current) < 0) vals.push(String(current));
    return vals;
  }

  function selectHtml(key, value, fieldName) {
    var opts = enumOptions(key, value);
    var html = '<select data-field="' + fieldName + '">';
    html += '<option value="">—</option>';
    for (var i = 0; i < opts.length; i++) {
      var v = opts[i];
      var sel = (String(value) === String(v)) ? " selected" : "";
      html += '<option value="' + esc(v) + '"' + sel + ">" + esc(v) + "</option>";
    }
    html += "</select>";
    return html;
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fieldText(label, name, value) {
    return '<div class="field"><label>' + esc(label) + '</label>' +
      '<input type="text" data-field="' + name + '" value="' + esc(value == null ? "" : value) + '"></div>';
  }

  function fieldNumber(label, name, value, min, max) {
    var v = (value == null || value === "") ? 0 : value;
    return '<div class="field"><label>' + esc(label) + '</label>' +
      '<input type="number" data-field="' + name + '" value="' + esc(v) +
      '" min="' + min + '" max="' + max + '" step="1"></div>';
  }

  function fieldSelect(label, enumKey, name, value) {
    return '<div class="field"><label>' + esc(label) + '</label>' +
      selectHtml(enumKey, value, name) + "</div>";
  }

  function laneSelect(value) {
    var lanes = state.doc.lanes || [];
    var html = '<select data-field="lane">';
    html += '<option value="">—</option>';
    for (var i = 0; i < lanes.length; i++) {
      var id = lanes[i].id || "";
      var sel = (String(value) === String(id)) ? " selected" : "";
      html += '<option value="' + esc(id) + '"' + sel + ">" + esc(id) +
        (lanes[i].label ? " — " + esc(lanes[i].label) : "") + "</option>";
    }
    html += "</select>";
    return '<div class="field"><label>lane</label>' + html + "</div>";
  }

  function nodeSelect(field, value) {
    var nodes = state.doc.nodes || [];
    var html = '<select data-field="' + field + '">';
    html += '<option value="">—</option>';
    for (var i = 0; i < nodes.length; i++) {
      var id = nodes[i].id || "";
      var sel = (String(value) === String(id)) ? " selected" : "";
      html += '<option value="' + esc(id) + '"' + sel + ">" + esc(id) +
        (nodes[i].label ? " — " + esc(nodes[i].label) : "") + "</option>";
    }
    html += "</select>";
    return '<div class="field"><label>' + esc(field) + '</label>' + html + "</div>";
  }

  function renderLists() {
    if (!state.doc) {
      ["nodes", "edges", "lanes"].forEach(function (kind) {
        var el = $("list-" + kind);
        if (el) el.innerHTML = '<div class="empty">Open a diagram to edit</div>';
      });
      return;
    }
    ensureArrays();
    renderList("nodes", state.doc.nodes, function (n, i) {
      return '<div><strong>' + esc(n.id || ("#" + i)) + '</strong></div>' +
        '<div class="sub">' + esc(n.label || "") + " · lane " + esc(n.lane || "") +
        " · col " + esc(n.col == null ? "" : n.col) + "</div>";
    });
    renderList("edges", state.doc.edges, function (e, i) {
      return '<div><strong>' + esc(e.from || "?") + " → " + esc(e.to || "?") + "</strong></div>" +
        '<div class="sub">' + esc(e.role || "") + (e.variant ? " · " + esc(e.variant) : "") + "</div>";
    });
    renderList("lanes", state.doc.lanes, function (l, i) {
      return '<div><strong>' + esc(l.id || ("#" + i)) + '</strong></div>' +
        '<div class="sub">' + esc(l.label || "") + (l.variant ? " · " + esc(l.variant) : "") + "</div>";
    });
  }

  function renderList(kind, items, renderer) {
    var el = $("list-" + kind);
    var html = "";
    for (var i = 0; i < items.length; i++) {
      var active = state.selected[kind] === i ? " active" : "";
      html += '<div class="list-item' + active + '" data-kind="' + kind + '" data-index="' + i + '">' +
        renderer(items[i], i) + "</div>";
    }
    html += '<div class="list-item" data-kind="' + kind + '" data-index="-1" style="color:var(--accent)">+ Add ' +
      kind.slice(0, -1) + "</div>";
    el.innerHTML = html;
  }

  function renderForm(kind) {
    var form = $("form-" + kind);
    if (!state.doc) {
      form.innerHTML = '<div class="empty">Open a diagram</div>';
      return;
    }
    var idx = state.selected[kind];
    var items = state.doc[kind];
    if (idx < 0 || idx >= items.length) {
      form.innerHTML = '<div class="empty">Select a ' + kind.slice(0, -1) + "</div>";
      return;
    }
    var item = items[idx];
    var html = "<h2>" + esc(kind.slice(0, -1)) + " #" + idx + "</h2>";

    if (kind === "nodes") {
      html += fieldText("id", "id", item.id);
      html += laneSelect(item.lane);
      html += fieldNumber("col", "col", item.col, 0, 5);
      html += fieldSelect("type", "node.type", "type", item.type);
      html += fieldText("label", "label", item.label);
    } else if (kind === "edges") {
      html += nodeSelect("from", item.from);
      html += nodeSelect("to", item.to);
      html += fieldSelect("role", "edge.role", "role", item.role);
      html += fieldSelect("variant", "edge.variant", "variant", item.variant);
      html += fieldSelect("route", "edge.route", "route", item.route);
      html += fieldSelect("fromSide", "edge.fromSide", "fromSide", item.fromSide);
      html += fieldSelect("toSide", "edge.toSide", "toSide", item.toSide);
      html += fieldText("label", "label", item.label);
    } else if (kind === "lanes") {
      html += fieldText("id", "id", item.id);
      html += fieldText("label", "label", item.label);
      html += fieldSelect("variant", "lane.variant", "variant", item.variant);
    }

    html += '<div class="row-actions">' +
      '<button type="button" class="danger" data-action="remove" data-kind="' + kind + '">Remove</button>' +
      "</div>";
    form.innerHTML = html;
  }

  function renderRaw() {
    if (state.rawDirty) return;
    if (!state.doc) {
      $("raw-editor").value = "";
      return;
    }
    $("raw-editor").value = JSON.stringify(state.doc, null, 2);
  }

  function renderAll() {
    renderLists();
    renderForm("nodes");
    renderForm("edges");
    renderForm("lanes");
    renderRaw();
    updateHistoryButtons();
    syncQualityToggle();
    updateDirtyUI();
  }

  function showLayoutTabIfReady() {
    var tab = $("tab-layout");
    if (!tab) return;
    if (state.file && state.archify) tab.classList.remove("hidden");
    else tab.classList.add("hidden");
  }

  function preferLayoutTab() {
    if (state.file && state.archify) switchTab("layout");
    else switchTab("nodes");
  }

  function currentQualityProfile() {
    var meta = state.doc && state.doc.meta;
    if (meta && typeof meta === "object" && typeof meta.quality_profile === "string") {
      var qp = meta.quality_profile;
      if (qp === "showcase") return "showcase";
      if (qp === "standard") return "standard";
    }
    // Absent or unrecognized -> Archify default
    return "standard";
  }

  function syncQualityToggle() {
    var profile = currentQualityProfile();
    var stdBtn = $("btn-quality-standard");
    var showBtn = $("btn-quality-showcase");
    if (stdBtn) {
      stdBtn.classList.toggle("mode-active", profile === "standard");
      stdBtn.disabled = !!state.layoutBusy;
    }
    if (showBtn) {
      showBtn.classList.toggle("mode-active", profile === "showcase");
      showBtn.disabled = !!state.layoutBusy;
    }
  }

  function setQualityProfile(next) {
    if (next !== "standard" && next !== "showcase") return;
    if (!state.doc || state.layoutBusy) return;
    var hadMeta = state.doc.meta != null && typeof state.doc.meta === "object" && !Array.isArray(state.doc.meta);
    var prevHadKey = hadMeta && Object.prototype.hasOwnProperty.call(state.doc.meta, "quality_profile");
    var prevValue = prevHadKey ? state.doc.meta.quality_profile : undefined;
    // No-op only when the key already equals the chosen value. If absent, first
    // click (either way) writes meta.quality_profile explicitly.
    if (prevHadKey && prevValue === next) return;

    pushHistory();
    if (!hadMeta) state.doc.meta = {};
    state.doc.meta.quality_profile = next;
    state.rawDirty = false;
    syncQualityToggle();
    setLayoutBusy(true);
    setStatus("previewing quality_profile → " + next + "…", "");

    function revertProfile() {
      if (!hadMeta) {
        delete state.doc.meta;
      } else if (!prevHadKey) {
        delete state.doc.meta.quality_profile;
      } else {
        state.doc.meta.quality_profile = prevValue;
      }
      revertHistoryPush();
      syncQualityToggle();
    }

    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "quality_profile → " + next + " (unsaved)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            syncQualityToggle();
            renderAll();
          });
        }
        revertProfile();
        setLayoutBusy(false);
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("quality_profile not updated (reverted):\n- " + errs.join("\n- "), "err");
        renderRaw();
      })
      .catch(function (e) {
        revertProfile();
        setLayoutBusy(false);
        setStatus("quality_profile preview failed (reverted): " + e, "err");
        renderRaw();
      });
  }

  function switchTab(tab) {
    cancelInlineEditors();
    if (tab !== "layout") {
      clearConnectFrom();
      clearEdgeSelection(false);
    }
    state.tab = tab;
    var buttons = document.querySelectorAll(".tabs button");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle("active", buttons[i].getAttribute("data-tab") === tab);
    }
    var panes = ["nodes", "edges", "lanes", "layout", "raw"];
    for (var j = 0; j < panes.length; j++) {
      $("pane-" + panes[j]).classList.toggle("active", panes[j] === tab);
    }
    if (tab === "raw") renderRaw();
    if (tab === "layout") loadLayoutPane(false);
  }

  function clientToSvg(svg, clientX, clientY) {
    var pt = svg.createSVGPoint();
    pt.x = clientX;
    pt.y = clientY;
    var ctm = svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    return pt.matrixTransform(ctm.inverse());
  }

  function laneCentersFromLayout(layout, lanes) {
    var sums = {};
    var counts = {};
    var nodes = layout.nodes || [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!n || n.lane == null) continue;
      var cy = Number(n.y) + Number(n.height) / 2;
      if (!counts[n.lane]) { sums[n.lane] = 0; counts[n.lane] = 0; }
      sums[n.lane] += cy;
      counts[n.lane] += 1;
    }
    var known = [];
    for (var li = 0; li < lanes.length; li++) {
      var id = lanes[li].id;
      if (counts[id]) known.push({ i: li, y: sums[id] / counts[id] });
    }
    var a = 120;
    var b = 138;
    if (known.length >= 2) {
      var npts = known.length, sx = 0, sy = 0, sxx = 0, sxy = 0;
      for (var k = 0; k < npts; k++) {
        sx += known[k].i;
        sy += known[k].y;
        sxx += known[k].i * known[k].i;
        sxy += known[k].i * known[k].y;
      }
      var den = npts * sxx - sx * sx;
      if (den !== 0) {
        b = (npts * sxy - sx * sy) / den;
        a = (sy - b * sx) / npts;
      }
    } else if (known.length === 1) {
      a = known[0].y - b * known[0].i;
    }
    var centers = {};
    for (var j = 0; j < lanes.length; j++) {
      var lid = lanes[j].id;
      if (counts[lid]) centers[lid] = sums[lid] / counts[lid];
      else centers[lid] = a + b * j;
    }
    return centers;
  }

  function snapLaneCol(px, py, layout) {
    var cols = layout.columns || [];
    var bestCol = 0;
    var bestD = Infinity;
    for (var i = 0; i < cols.length; i++) {
      var d = Math.abs(px - cols[i]);
      if (d < bestD) { bestD = d; bestCol = i; }
    }
    var lanes = state.doc.lanes || [];
    var centers = laneCentersFromLayout(layout, lanes);
    var bestLane = (lanes[0] && lanes[0].id) || "";
    var bestLd = Infinity;
    for (var j = 0; j < lanes.length; j++) {
      var id = lanes[j].id;
      var d2 = Math.abs(py - centers[id]);
      if (d2 < bestLd) { bestLd = d2; bestLane = id; }
    }
    return { col: bestCol, lane: bestLane };
  }

  function findDocNode(id) {
    var nodes = state.doc.nodes || [];
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].id === id) return nodes[i];
    }
    return null;
  }

  function clearLayoutOverlays(svg) {
    if (!svg) return;
    var old = svg.querySelectorAll(
      "rect.bw-handle, polyline.bw-edge-hit, rect.bw-lane-hit, circle.bw-endpoint"
    );
    for (var i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
  }

  function updateModeButtons() {
    var moveBtn = $("btn-mode-move");
    var connBtn = $("btn-mode-connect");
    if (moveBtn) moveBtn.classList.toggle("mode-active", state.layoutMode === "move");
    if (connBtn) connBtn.classList.toggle("mode-active", state.layoutMode === "connect");
  }

  function updateDeleteEdgeButton() {
    var btn = $("btn-delete-edge");
    if (!btn) return;
    btn.disabled = state.selectedEdgeIndex == null || state.layoutBusy;
  }

  function updateLayoutHint() {
    var hint = $("layout-hint");
    if (!hint) return;
    var text;
    if (state.layoutMode === "connect") {
      hint.classList.add("layout-hint-active");
      text = state.connectFrom
        ? ("Source " + state.connectFrom + " → click a target to connect. Click an edge to select/delete. Drag endpoints to reroute. Esc cancels.")
        : "Click a source node, then a target to connect. Click an edge to select/delete. Drag endpoints to reroute. Esc cancels.";
    } else {
      hint.classList.remove("layout-hint-active");
      text = "Drag a node; drop snaps to nearest lane + column (preview; unsaved until Save). Use Connect / Edit to add or change connections.";
    }
    hint.textContent = text;
    hint.title = text;
  }

  function clearConnectFrom() {
    state.connectFrom = null;
    updateConnectHighlight();
    updateLayoutHint();
  }

  function clearEdgeSelection(refresh) {
    state.selectedEdgeIndex = null;
    updateDeleteEdgeButton();
    if (refresh !== false) refreshEdgeSelectionStyles();
  }

  function setLayoutMode(mode) {
    if (mode !== "move" && mode !== "connect") return;
    cancelInlineEditors();
    if (layoutDrag) {
      layoutDrag.rect.setAttribute("x", String(layoutDrag.origX));
      layoutDrag.rect.setAttribute("y", String(layoutDrag.origY));
      layoutDrag.rect.style.cursor = handleCursor();
      layoutDrag = null;
    }
    endpointDrag = null;
    state.layoutMode = mode;
    state.connectFrom = null;
    state.selectedEdgeIndex = null;
    updateModeButtons();
    updateDeleteEdgeButton();
    updateLayoutHint();
    // Remount so Connect-only bw-endpoint handles appear/disappear with mode.
    mountLayoutOverlays();
  }

  function handleCursor() {
    if (state.layoutBusy) return "wait";
    return state.layoutMode === "connect" ? "crosshair" : "grab";
  }

  function findNthDocEdgeIndex(from, to, nth) {
    var edges = state.doc.edges || [];
    var seen = 0;
    for (var i = 0; i < edges.length; i++) {
      if (String(edges[i].from) === String(from) && String(edges[i].to) === String(to)) {
        if (seen === nth) return i;
        seen++;
      }
    }
    return -1;
  }

  function edgePairExists(from, to) {
    return edgePairExistsExcluding(from, to, -1);
  }

  function edgePairExistsExcluding(from, to, skipIdx) {
    var edges = state.doc.edges || [];
    for (var i = 0; i < edges.length; i++) {
      if (i === skipIdx) continue;
      if (String(edges[i].from) === String(from) && String(edges[i].to) === String(to)) return true;
    }
    return false;
  }

  function updateConnectHighlight() {
    var iframe = $("layout-frame");
    var doc = iframe && iframe.contentDocument;
    if (!doc) return;
    var rects = doc.querySelectorAll("rect.bw-handle");
    for (var i = 0; i < rects.length; i++) {
      var id = rects[i].getAttribute("data-node-id");
      var on = state.connectFrom && id === state.connectFrom;
      rects[i].setAttribute("stroke", on ? "rgba(61,214,140,0.95)" : "rgba(61,139,253,0.55)");
      rects[i].setAttribute("stroke-width", on ? "2.5" : "1.5");
      rects[i].setAttribute("fill", on ? "rgba(61,214,140,0.18)" : "rgba(61,139,253,0.01)");
    }
  }

  function refreshEdgeSelectionStyles() {
    var iframe = $("layout-frame");
    var doc = iframe && iframe.contentDocument;
    if (!doc) return;
    var hits = doc.querySelectorAll("polyline.bw-edge-hit");
    for (var i = 0; i < hits.length; i++) {
      var idx = parseInt(hits[i].getAttribute("data-doc-index"), 10);
      var sel = state.selectedEdgeIndex != null && idx === state.selectedEdgeIndex;
      hits[i].setAttribute("stroke", sel ? "rgba(61,139,253,0.9)" : "rgba(61,139,253,0.01)");
      hits[i].setAttribute("stroke-width", sel ? "14" : "12");
    }
  }

  function applyOverlayInteractionStyles() {
    var iframe = $("layout-frame");
    var doc = iframe && iframe.contentDocument;
    if (!doc) return;
    var cursor = handleCursor();
    var rects = doc.querySelectorAll("rect.bw-handle");
    for (var i = 0; i < rects.length; i++) {
      rects[i].style.pointerEvents = state.layoutBusy ? "none" : "all";
      rects[i].style.cursor = cursor;
    }
    var hits = doc.querySelectorAll("polyline.bw-edge-hit");
    for (var j = 0; j < hits.length; j++) {
      hits[j].style.pointerEvents = state.layoutBusy ? "none" : "stroke";
      hits[j].style.cursor = state.layoutBusy ? "wait" : "pointer";
    }
    var laneHits = doc.querySelectorAll("rect.bw-lane-hit");
    for (var k = 0; k < laneHits.length; k++) {
      laneHits[k].style.pointerEvents = state.layoutBusy ? "none" : "all";
      laneHits[k].style.cursor = state.layoutBusy ? "wait" : "text";
    }
    var endpoints = doc.querySelectorAll("circle.bw-endpoint");
    for (var e = 0; e < endpoints.length; e++) {
      endpoints[e].style.pointerEvents = state.layoutBusy ? "none" : "all";
      endpoints[e].style.cursor = state.layoutBusy ? "wait" : "grab";
    }
    updateConnectHighlight();
    refreshEdgeSelectionStyles();
    updateDeleteEdgeButton();
  }

  function selectLayoutEdge(docIdx) {
    if (docIdx == null || isNaN(docIdx) || docIdx < 0) return;
    if (!(state.doc.edges && state.doc.edges[docIdx])) return;
    state.connectFrom = null;
    state.selectedEdgeIndex = docIdx;
    var e = state.doc.edges[docIdx];
    updateConnectHighlight();
    refreshEdgeSelectionStyles();
    updateDeleteEdgeButton();
    updateLayoutHint();
    setStatus("Selected edge " + e.from + " → " + e.to + " (Delete to remove)", "");
  }

  function isRoleRequiredError(errors) {
    var text = (errors || []).join(" ").toLowerCase();
    return text.indexOf("role") >= 0 &&
      (text.indexOf("required") >= 0 || text.indexOf("must have") >= 0 ||
       text.indexOf("missing") >= 0 || text.indexOf("enum") >= 0);
  }

  function postPreviewDoc() {
    return apiFetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.doc),
    }).then(function (r) { return r.json(); });
  }

  function revertHistoryPush() {
    if (state.undo.length) {
      state.undo.pop();
      updateHistoryButtons();
    }
  }

  function handleConnectNodeClick(nodeId) {
    if (!nodeId) return;
    if (!state.connectFrom) {
      state.connectFrom = nodeId;
      state.selectedEdgeIndex = null;
      updateConnectHighlight();
      refreshEdgeSelectionStyles();
      updateDeleteEdgeButton();
      updateLayoutHint();
      setStatus("Connect: source " + nodeId + " → click target (Esc cancels)", "");
      return;
    }
    if (state.connectFrom === nodeId) {
      setStatus("Self-loop refused (from === to)", "err");
      return;
    }
    if (edgePairExists(state.connectFrom, nodeId)) {
      setStatus("Duplicate edge refused: " + state.connectFrom + " → " + nodeId, "err");
      return;
    }
    var from = state.connectFrom;
    var to = nodeId;
    state.connectFrom = null;
    updateConnectHighlight();
    updateLayoutHint();
    addEdgeAndSave(from, to, false);
  }

  function addEdgeAndSave(from, to, withRole) {
    pushHistory();
    ensureArrays();
    var edge = { from: from, to: to };
    if (withRole) edge.role = "branch";
    state.doc.edges.push(edge);
    state.rawDirty = false;
    renderLists();
    setLayoutBusy(true);
    setStatus("previewing edge " + from + " → " + to + "…", "");
    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "Added edge " + from + " → " + to + " (unsaved)";
          if (withRole) msg += " (role:branch)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
          });
        }
        if (!withRole && isRoleRequiredError(receipt.errors)) {
          var last = state.doc.edges[state.doc.edges.length - 1];
          if (last && String(last.from) === String(from) && String(last.to) === String(to)) {
            last.role = "branch";
            setStatus("retrying edge preview with role:branch…", "");
            return postPreviewDoc().then(function (receipt2) {
              if (receipt2.ok) {
                markDirty();
                var msg2 = "Added edge " + from + " → " + to + " (role:branch; Archify required role) (unsaved)";
                if (receipt2.note) msg2 += "\nNote: " + receipt2.note;
                setStatus(msg2, "ok");
                return loadLayoutPane(true).then(function () {
                  setLayoutBusy(false);
                  renderAll();
                });
              }
              state.doc.edges.pop();
              revertHistoryPush();
              setLayoutBusy(false);
              var errs2 = receipt2.errors || [receipt2.error || "preview failed"];
              setStatus("Edge not added (reverted):\n- " + errs2.join("\n- "), "err");
              renderLists();
            });
          }
        }
        state.doc.edges.pop();
        revertHistoryPush();
        setLayoutBusy(false);
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("Edge not added (reverted):\n- " + errs.join("\n- "), "err");
        renderLists();
      })
      .catch(function (e) {
        state.doc.edges.pop();
        revertHistoryPush();
        setLayoutBusy(false);
        setStatus("Edge preview failed (reverted): " + e, "err");
        renderLists();
      });
  }

  function deleteSelectedEdge() {
    var idx = state.selectedEdgeIndex;
    if (idx == null || idx < 0) return;
    if (!(state.doc.edges && state.doc.edges[idx])) return;
    if (state.layoutBusy) return;
    var removed = state.doc.edges[idx];
    pushHistory();
    state.doc.edges.splice(idx, 1);
    state.selectedEdgeIndex = null;
    state.rawDirty = false;
    updateDeleteEdgeButton();
    renderLists();
    setLayoutBusy(true);
    setStatus("previewing delete edge " + removed.from + " → " + removed.to + "…", "");
    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "Deleted edge " + removed.from + " → " + removed.to + " (unsaved)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
          });
        }
        state.doc.edges.splice(idx, 0, removed);
        revertHistoryPush();
        state.selectedEdgeIndex = idx;
        setLayoutBusy(false);
        updateDeleteEdgeButton();
        refreshEdgeSelectionStyles();
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("Edge not deleted (reverted):\n- " + errs.join("\n- "), "err");
        renderLists();
      })
      .catch(function (e) {
        state.doc.edges.splice(idx, 0, removed);
        revertHistoryPush();
        state.selectedEdgeIndex = idx;
        setLayoutBusy(false);
        updateDeleteEdgeButton();
        refreshEdgeSelectionStyles();
        setStatus("Edge delete preview failed (reverted): " + e, "err");
        renderLists();
      });
  }

  var LAYOUT_ZOOM_MIN = 0.1;
  var LAYOUT_ZOOM_MAX = 4.0;
  var LAYOUT_ZOOM_STEP = 1.25;

  function clampLayoutZoom(z) {
    if (!(z > 0)) z = 1;
    return Math.max(LAYOUT_ZOOM_MIN, Math.min(LAYOUT_ZOOM_MAX, z));
  }

  function getSvgViewBoxSize(svg) {
    var vb = svg && svg.viewBox && svg.viewBox.baseVal;
    if (vb && vb.width > 0 && vb.height > 0) return { w: vb.width, h: vb.height };
    if (state.layout && state.layout.viewBox) {
      var raw = state.layout.viewBox;
      if (typeof raw === "string") {
        var parts = raw.trim().split(/[\s,]+/);
        if (parts.length >= 4) {
          var w = Number(parts[2]), h = Number(parts[3]);
          if (w > 0 && h > 0) return { w: w, h: h };
        }
      } else if (raw && raw.width > 0 && raw.height > 0) {
        return { w: Number(raw.width), h: Number(raw.height) };
      }
    }
    return { w: 800, h: 600 };
  }

  function prepareDiagramViewport(doc) {
    if (!doc) return null;
    var existing = doc.getElementById("bw-scroll");
    if (existing) return existing;
    var style = doc.createElement("style");
    style.setAttribute("data-bw-viewport", "1");
    style.textContent =
      "html,body{height:100%;margin:0;overflow:hidden;background:#0b0f14}" +
      "#bw-scroll{overflow:auto;width:100%;height:100%}" +
      "svg{display:block;max-width:none !important}" +
      /* Original diagram content must not steal hits from overlays (edge labels, paths). */
      "svg *{pointer-events:none}" +
      "svg .bw-handle{pointer-events:all}" +
      "svg .bw-edge-hit{pointer-events:stroke}" +
      "svg .bw-lane-hit{pointer-events:all}" +
      "svg .bw-endpoint{pointer-events:all}";
    (doc.head || doc.documentElement).appendChild(style);
    var svg = doc.querySelector("svg");
    if (!svg) return null;
    var wrap = doc.createElement("div");
    wrap.id = "bw-scroll";
    svg.parentNode.insertBefore(wrap, svg);
    wrap.appendChild(svg);
    return wrap;
  }

  function updateZoomLabel() {
    var el = $("layout-zoom-pct");
    if (!el) return;
    if (state.layoutZoom == null) {
      el.textContent = "—";
      return;
    }
    el.textContent = Math.round(state.layoutZoom * 100) + "%";
  }

  function setLayoutZoomCss(z) {
    z = clampLayoutZoom(z);
    state.layoutZoom = z;
    var iframe = $("layout-frame");
    var doc = iframe.contentDocument;
    if (!doc) { updateZoomLabel(); return z; }
    var svg = doc.querySelector("svg");
    if (!svg) { updateZoomLabel(); return z; }
    var vb = getSvgViewBoxSize(svg);
    svg.style.width = (vb.w * z) + "px";
    svg.style.height = (vb.h * z) + "px";
    updateZoomLabel();
    return z;
  }

  function applyLayoutZoom(z) {
    cancelInlineEditors();
    return setLayoutZoomCss(z);
  }

  function computeFitZoom() {
    var iframe = $("layout-frame");
    var doc = iframe.contentDocument;
    var svg = doc && doc.querySelector("svg");
    if (!svg) return 1;
    var vb = getSvgViewBoxSize(svg);
    var paneW = iframe.clientWidth || 1;
    var paneH = iframe.clientHeight || 1;
    return clampLayoutZoom(Math.min(paneW / vb.w, paneH / vb.h));
  }

  function stashLayoutViewport() {
    var iframe = $("layout-frame");
    var doc = iframe.contentDocument;
    var scroll = doc && doc.getElementById("bw-scroll");
    return {
      zoom: state.layoutZoom,
      scrollLeft: scroll ? scroll.scrollLeft : 0,
      scrollTop: scroll ? scroll.scrollTop : 0,
    };
  }

  function restoreLayoutViewport(stash) {
    if (stash && stash.zoom != null) {
      setLayoutZoomCss(stash.zoom);
      var doc = $("layout-frame").contentDocument;
      var scroll = doc && doc.getElementById("bw-scroll");
      if (scroll) {
        scroll.scrollLeft = stash.scrollLeft || 0;
        scroll.scrollTop = stash.scrollTop || 0;
      }
    } else {
      setLayoutZoomCss(computeFitZoom());
    }
  }

  function fitLayoutViewport() {
    cancelInlineEditors();
    setLayoutZoomCss(computeFitZoom());
    var doc = $("layout-frame").contentDocument;
    var scroll = doc && doc.getElementById("bw-scroll");
    if (scroll) {
      scroll.scrollLeft = 0;
      scroll.scrollTop = 0;
    }
  }

  function mountLaneHitOverlays(doc, svg) {
    var lanes = (state.doc && state.doc.lanes) || [];
    if (!lanes.length) return;
    var frames = svg.querySelectorAll(
      'rect[data-composition-frame-kind="lane"][data-composition-frame-id]'
    );
    var placed = 0;
    for (var fi = 0; fi < frames.length; fi++) {
      var frame = frames[fi];
      var fid = frame.getAttribute("data-composition-frame-id") || "";
      var m = /^lane-(\d+)$/.exec(fid);
      if (!m) continue;
      var idx = parseInt(m[1], 10);
      if (isNaN(idx) || idx < 0 || idx >= lanes.length) continue;
      var fx = Number(frame.getAttribute("x"));
      var fy = Number(frame.getAttribute("y"));
      var fw = Number(frame.getAttribute("width"));
      var fh = Number(frame.getAttribute("height"));
      if (!(fw > 0) || !(fh > 0) || isNaN(fx) || isNaN(fy)) continue;
      var stripH = Math.min(LAYOUT_LANE_HIT_H, Math.max(18, fh * 0.12));
      var stripW = Math.min(LAYOUT_LANE_HIT_W_MAX, Math.max(120, fw * 0.4));
      var rect = doc.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("class", "bw-lane-hit");
      rect.setAttribute("data-lane-index", String(idx));
      rect.setAttribute("data-lane-id", String(lanes[idx].id || ""));
      rect.setAttribute("x", String(fx));
      rect.setAttribute("y", String(fy));
      rect.setAttribute("width", String(stripW));
      rect.setAttribute("height", String(stripH));
      rect.setAttribute("fill", "rgba(61,139,253,0.01)");
      rect.setAttribute("stroke", "rgba(61,139,253,0.35)");
      rect.setAttribute("stroke-width", "1");
      rect.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(rect);
      placed++;
    }
    if (placed > 0) return;

    // Fallback: derived centers + left gutter when lane frames are absent.
    var centers = laneCentersFromLayout(state.layout, lanes);
    var cols = (state.layout && state.layout.columns) || [];
    var gutterRight = cols.length ? Math.max(80, cols[0] - 20) : 160;
    var ids = Object.keys(centers);
    var ys = [];
    for (var ci = 0; ci < ids.length; ci++) ys.push(centers[ids[ci]]);
    ys.sort(function (a, b) { return a - b; });
    var bandHalf = 40;
    if (ys.length >= 2) bandHalf = Math.max(24, (ys[1] - ys[0]) / 2);
    for (var li = 0; li < lanes.length; li++) {
      var lane = lanes[li];
      if (!lane || lane.id == null) continue;
      var cy = centers[lane.id];
      if (cy == null || isNaN(cy)) continue;
      var y0 = cy - bandHalf;
      var rect2 = doc.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect2.setAttribute("class", "bw-lane-hit");
      rect2.setAttribute("data-lane-index", String(li));
      rect2.setAttribute("data-lane-id", String(lane.id));
      rect2.setAttribute("x", "0");
      rect2.setAttribute("y", String(y0));
      rect2.setAttribute("width", String(gutterRight));
      rect2.setAttribute("height", String(LAYOUT_LANE_HIT_H));
      rect2.setAttribute("fill", "rgba(61,139,253,0.01)");
      rect2.setAttribute("stroke", "rgba(61,139,253,0.35)");
      rect2.setAttribute("stroke-width", "1");
      rect2.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(rect2);
    }
  }

  function mountLayoutOverlays() {
    var iframe = $("layout-frame");
    var doc = iframe.contentDocument;
    if (!doc) return;
    var svg = doc.querySelector("svg");
    if (!svg || !state.layout) return;
    clearLayoutOverlays(svg);

    // Lane header strips (under edges/nodes so node handles win on overlap).
    mountLaneHitOverlays(doc, svg);

    // Edge hit-lines next; node handles stay on top for connect/move.
    var layoutEdges = state.layout.edges || [];
    var pairSeen = {};
    for (var ei = 0; ei < layoutEdges.length; ei++) {
      var le = layoutEdges[ei];
      if (!le || le.from == null || le.to == null || !le.points || !le.points.length) continue;
      var key = String(le.from) + "\0" + String(le.to);
      var nth = pairSeen[key] || 0;
      pairSeen[key] = nth + 1;
      var docIdx = findNthDocEdgeIndex(le.from, le.to, nth);
      if (docIdx < 0) continue;
      var pts = [];
      for (var p = 0; p < le.points.length; p++) {
        var pt = le.points[p];
        if (!pt || pt.length < 2) continue;
        pts.push(Number(pt[0]) + "," + Number(pt[1]));
      }
      if (pts.length < 2) continue;
      var poly = doc.createElementNS("http://www.w3.org/2000/svg", "polyline");
      poly.setAttribute("class", "bw-edge-hit");
      poly.setAttribute("data-from", String(le.from));
      poly.setAttribute("data-to", String(le.to));
      poly.setAttribute("data-doc-index", String(docIdx));
      poly.setAttribute("points", pts.join(" "));
      poly.setAttribute("fill", "none");
      poly.setAttribute("stroke-linecap", "round");
      poly.setAttribute("stroke-linejoin", "round");
      poly.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(poly);
    }

    var nodes = state.layout.nodes || [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!n || !n.id) continue;
      var rect = doc.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("class", "bw-handle");
      rect.setAttribute("data-node-id", n.id);
      rect.setAttribute("x", String(n.x));
      rect.setAttribute("y", String(n.y));
      rect.setAttribute("width", String(n.width));
      rect.setAttribute("height", String(n.height));
      rect.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(rect);
    }

    // Connect-only endpoint handles ABOVE node/edge overlays (pointer precedence).
    if (state.layoutMode === "connect") {
      var pairSeenEp = {};
      for (var ej = 0; ej < layoutEdges.length; ej++) {
        var le2 = layoutEdges[ej];
        if (!le2 || le2.from == null || le2.to == null || !le2.points || !le2.points.length) continue;
        var key2 = String(le2.from) + "\0" + String(le2.to);
        var nth2 = pairSeenEp[key2] || 0;
        pairSeenEp[key2] = nth2 + 1;
        var docIdx2 = findNthDocEdgeIndex(le2.from, le2.to, nth2);
        if (docIdx2 < 0) continue;
        var pts2 = le2.points;
        var ends = [
          { end: "from", pt: pts2[0] },
          { end: "to", pt: pts2[pts2.length - 1] },
        ];
        for (var ee = 0; ee < ends.length; ee++) {
          var ep = ends[ee].pt;
          if (!ep || ep.length < 2) continue;
          var circ = doc.createElementNS("http://www.w3.org/2000/svg", "circle");
          circ.setAttribute("class", "bw-endpoint");
          circ.setAttribute("data-doc-index", String(docIdx2));
          circ.setAttribute("data-end", ends[ee].end);
          circ.setAttribute("cx", String(Number(ep[0])));
          circ.setAttribute("cy", String(Number(ep[1])));
          circ.setAttribute("r", String(LAYOUT_ENDPOINT_R));
          circ.setAttribute("fill", "rgba(255,170,50,0.95)");
          circ.setAttribute("stroke", "rgba(255,255,255,0.95)");
          circ.setAttribute("stroke-width", "1.5");
          circ.setAttribute("vector-effect", "non-scaling-stroke");
          svg.appendChild(circ);
        }
      }
    }

    applyOverlayInteractionStyles();
    updateLayoutHint();
    if (!doc._bwLayoutBound) {
      doc._bwLayoutBound = true;
      doc.addEventListener("pointerdown", onLayoutPointerDown);
      doc.addEventListener("pointermove", onLayoutPointerMove);
      doc.addEventListener("pointerup", onLayoutPointerUp);
      doc.addEventListener("pointercancel", onLayoutPointerUp);
      doc.addEventListener("dblclick", onLayoutDblClick);
      doc.addEventListener("keydown", onLayoutKeyDown);
      var scroll = doc.getElementById("bw-scroll");
      if (scroll) {
        scroll.addEventListener("scroll", function () { cancelInlineEditors(); });
      }
    }
  }

  function onLayoutKeyDown(ev) {
    if (ev.key === "Escape" || ev.key === "Esc") {
      if (endpointDrag) {
        ev.preventDefault();
        endpointDrag = null;
        mountLayoutOverlays();
        setStatus("Reroute cancelled", "");
        return;
      }
      if (state.connectFrom) {
        ev.preventDefault();
        clearConnectFrom();
        setStatus("Connect cancelled", "");
      }
      return;
    }
    if (ev.key === "Delete" || ev.key === "Backspace") {
      // Prefer deleting the edited node when the node editor is open
      // (iframe focus rarely sits in parent inputs, so this is safe here).
      if (nodeEdit) {
        ev.preventDefault();
        deleteLayoutNode();
        return;
      }
      if (state.selectedEdgeIndex != null) {
        ev.preventDefault();
        deleteSelectedEdge();
      }
    }
  }

  function hideNodeEditor() {
    var panel = $("layout-node-editor");
    if (!panel) return;
    panel.classList.remove("active");
    ["layout-edit-label", "layout-edit-sublabel", "layout-edit-tag", "layout-edit-brand"].forEach(function (id) {
      var el = $(id);
      if (el) {
        el.value = "";
        el.disabled = false;
      }
    });
    var typeEl = $("layout-edit-type");
    if (typeEl) {
      typeEl.innerHTML = "";
      typeEl.disabled = false;
    }
    var dupBtn = $("layout-edit-duplicate");
    if (dupBtn) dupBtn.disabled = true;
    var delBtn = $("layout-edit-delete");
    if (delBtn) delBtn.disabled = true;
    var hint = $("layout-edit-brand-hint");
    if (hint) {
      hint.hidden = true;
      hint.textContent = "";
    }
  }

  function fillNodeTypeSelect(current) {
    var sel = $("layout-edit-type");
    if (!sel) return;
    var opts = enumOptions("node.type", current);
    var html = "";
    for (var i = 0; i < opts.length; i++) {
      var v = opts[i];
      var selected = (String(current || "") === String(v)) ? " selected" : "";
      html += '<option value="' + esc(v) + '"' + selected + ">" + esc(v) + "</option>";
    }
    if (current && opts.indexOf(String(current)) < 0) {
      html = '<option value="' + esc(current) + '" selected>' + esc(current) + "</option>" + html;
    }
    sel.innerHTML = html;
  }

  function hideSingleEditor() {
    var panel = $("layout-single-editor");
    if (!panel) return;
    panel.classList.remove("active");
    var input = $("layout-single-label");
    if (input) input.value = "";
  }

  function cancelNodeEditor() {
    if (!nodeEdit) return;
    nodeEdit = null;
    hideNodeEditor();
  }

  function cancelSingleEditor() {
    if (!singleEdit) return;
    singleEdit = null;
    hideSingleEditor();
  }

  function cancelInlineEditors() {
    cancelNodeEditor();
    cancelSingleEditor();
  }

  function positionEditorPanel(panel, anchorEl, minWidth) {
    var wrap = $("layout-wrap");
    if (!panel || !wrap || !anchorEl) return;
    var wrapRect = wrap.getBoundingClientRect();
    var handleRect = handleScreenRect(anchorEl);
    var left = handleRect.left - wrapRect.left;
    var top = handleRect.top - wrapRect.top;
    var width = Math.max(handleRect.width, minWidth || 180);
    panel.style.left = left + "px";
    panel.style.top = top + "px";
    panel.style.width = width + "px";
  }

  function isObjectBrand(brand) {
    return brand != null && typeof brand === "object" && !Array.isArray(brand);
  }

  function snapNodeTextFields(node) {
    return {
      type: node.type,
      hasType: Object.prototype.hasOwnProperty.call(node, "type"),
      label: node.label,
      hasSublabel: Object.prototype.hasOwnProperty.call(node, "sublabel"),
      sublabel: node.sublabel,
      hasTag: Object.prototype.hasOwnProperty.call(node, "tag"),
      tag: node.tag,
      hasBrand: Object.prototype.hasOwnProperty.call(node, "brand"),
      brand: isObjectBrand(node.brand) ? clone(node.brand) : node.brand,
    };
  }

  function restoreNodeTextFields(node, snap) {
    if (!node || !snap) return;
    if (snap.hasType) node.type = snap.type;
    else delete node.type;
    node.label = snap.label;
    if (snap.hasSublabel) node.sublabel = snap.sublabel;
    else delete node.sublabel;
    if (snap.hasTag) node.tag = snap.tag;
    else delete node.tag;
    if (snap.hasBrand) node.brand = isObjectBrand(snap.brand) ? clone(snap.brand) : snap.brand;
    else delete node.brand;
  }

  function handleScreenRect(handle) {
    var iframe = $("layout-frame");
    var iframeRect = iframe.getBoundingClientRect();
    var hr = handle.getBoundingClientRect();
    // Some engines report iframe-content rects in the iframe viewport; offset if needed.
    if (hr.top >= iframeRect.top - 0.5 && hr.left >= iframeRect.left - 0.5) {
      return { left: hr.left, top: hr.top, width: hr.width, height: hr.height };
    }
    return {
      left: iframeRect.left + hr.left,
      top: iframeRect.top + hr.top,
      width: hr.width,
      height: hr.height,
    };
  }

  function openNodeEditor(handle) {
    cancelInlineEditors();
    if (state.layoutBusy || !handle) return;
    var id = handle.getAttribute("data-node-id");
    var node = findDocNode(id);
    if (!node) return;
    var panel = $("layout-node-editor");
    var wrap = $("layout-wrap");
    var labelEl = $("layout-edit-label");
    var subEl = $("layout-edit-sublabel");
    var tagEl = $("layout-edit-tag");
    var brandEl = $("layout-edit-brand");
    var brandHint = $("layout-edit-brand-hint");
    var typeEl = $("layout-edit-type");
    if (!panel || !wrap || !labelEl || !subEl || !tagEl || !brandEl || !typeEl) return;
    positionEditorPanel(panel, handle, 220);
    fillNodeTypeSelect(node.type || "backend");
    labelEl.value = node.label != null ? String(node.label) : "";
    subEl.value = node.sublabel != null ? String(node.sublabel) : "";
    tagEl.value = node.tag != null ? String(node.tag) : "";
    var brandLocked = isObjectBrand(node.brand);
    if (brandLocked) {
      brandEl.value = node.brand.url != null ? String(node.brand.url) : "(object brand)";
      brandEl.disabled = true;
      if (brandHint) {
        brandHint.hidden = false;
        brandHint.textContent = "Object brand — edit in Raw JSON";
      }
    } else {
      brandEl.value = node.brand != null ? String(node.brand) : "";
      brandEl.disabled = false;
      if (brandHint) {
        brandHint.hidden = true;
        brandHint.textContent = "";
      }
    }
    nodeEdit = {
      nodeId: id,
      brandLocked: brandLocked,
      snap: snapNodeTextFields(node),
      handle: handle,
    };
    var dupBtn = $("layout-edit-duplicate");
    if (dupBtn) dupBtn.disabled = false;
    var delBtn = $("layout-edit-delete");
    if (delBtn) {
      var nodeCount = (state.doc.nodes || []).length;
      delBtn.disabled = nodeCount <= 1;
      delBtn.title = nodeCount <= 1
        ? "Cannot delete the last node"
        : "Delete node and its edges";
    }
    panel.classList.add("active");
    labelEl.focus();
    labelEl.select();
    setStatus("Editing " + id + " (Enter=save, Esc=cancel)", "");
  }

  function openEdgeLabelEditor(hitEl) {
    cancelInlineEditors();
    if (state.layoutBusy || !hitEl) return;
    var idx = parseInt(hitEl.getAttribute("data-doc-index"), 10);
    if (isNaN(idx) || idx < 0 || !(state.doc.edges && state.doc.edges[idx])) return;
    var edge = state.doc.edges[idx];
    var panel = $("layout-single-editor");
    var input = $("layout-single-label");
    var caption = $("layout-single-label-caption");
    if (!panel || !input) return;
    if (caption) caption.textContent = "Edge label";
    panel.setAttribute("aria-label", "Edit edge label");
    positionEditorPanel(panel, hitEl, 200);
    input.value = edge.label != null ? String(edge.label) : "";
    singleEdit = {
      kind: "edge",
      edgeIndex: idx,
      hasLabel: Object.prototype.hasOwnProperty.call(edge, "label"),
      snapLabel: edge.label,
      anchor: hitEl,
    };
    panel.classList.add("active");
    input.focus();
    input.select();
    setStatus("Editing edge " + edge.from + " → " + edge.to + " label (empty clears)", "");
  }

  function openLaneLabelEditor(hitEl) {
    cancelInlineEditors();
    if (state.layoutBusy || !hitEl) return;
    var idx = parseInt(hitEl.getAttribute("data-lane-index"), 10);
    if (isNaN(idx) || idx < 0 || !(state.doc.lanes && state.doc.lanes[idx])) return;
    var lane = state.doc.lanes[idx];
    var panel = $("layout-single-editor");
    var input = $("layout-single-label");
    var caption = $("layout-single-label-caption");
    if (!panel || !input) return;
    if (caption) caption.textContent = "Lane label";
    panel.setAttribute("aria-label", "Edit lane label");
    positionEditorPanel(panel, hitEl, 200);
    input.value = lane.label != null ? String(lane.label) : "";
    singleEdit = {
      kind: "lane",
      laneIndex: idx,
      snapLabel: lane.label,
      anchor: hitEl,
    };
    panel.classList.add("active");
    input.focus();
    input.select();
    setStatus("Editing lane " + (lane.id || idx) + " label (required)", "");
  }

  function restoreEdgeLabel(edge, edit) {
    if (!edge || !edit) return;
    if (edit.hasLabel) edge.label = edit.snapLabel;
    else delete edge.label;
  }

  function commitSingleEditor() {
    if (!singleEdit) return;
    var edit = singleEdit;
    var input = $("layout-single-label");
    var value = String((input && input.value) || "").trim();
    singleEdit = null;
    hideSingleEditor();

    if (edit.kind === "edge") {
      var edge = state.doc.edges && state.doc.edges[edit.edgeIndex];
      if (!edge) return;
      var cur = edge.label != null ? String(edge.label) : "";
      var next = value;
      var curHas = Object.prototype.hasOwnProperty.call(edge, "label");
      if ((!curHas && !next) || (curHas && cur === next)) {
        setStatus("Edge label unchanged", "");
        return;
      }
      pushHistory();
      if (next) edge.label = next;
      else delete edge.label;
      state.rawDirty = false;
      renderLists();
      setLayoutBusy(true);
      setStatus("previewing edge label…", "");
      postPreviewDoc()
        .then(function (receipt) {
          if (receipt.ok) {
            markDirty();
            var msg = "Updated edge label " + edge.from + " → " + edge.to + " (unsaved)";
            if (receipt.note) msg += "\nNote: " + receipt.note;
            setStatus(msg, "ok");
            return loadLayoutPane(true).then(function () {
              setLayoutBusy(false);
              renderAll();
            });
          }
          restoreEdgeLabel(edge, edit);
          revertHistoryPush();
          setLayoutBusy(false);
          var errs = receipt.errors || [receipt.error || "preview failed"];
          setStatus("Edge label not updated (reverted):\n- " + errs.join("\n- "), "err");
          renderLists();
        })
        .catch(function (e) {
          restoreEdgeLabel(edge, edit);
          revertHistoryPush();
          setLayoutBusy(false);
          setStatus("Edge label preview failed (reverted): " + e, "err");
          renderLists();
        });
      return;
    }

    if (edit.kind === "lane") {
      var lane = state.doc.lanes && state.doc.lanes[edit.laneIndex];
      if (!lane) return;
      if (!value) {
        setStatus("Lane label required (minLength 1); kept previous", "");
        return;
      }
      if (String(lane.label || "") === value) {
        setStatus("Lane label unchanged", "");
        return;
      }
      var prevLabel = lane.label;
      pushHistory();
      lane.label = value;
      state.rawDirty = false;
      renderLists();
      setLayoutBusy(true);
      setStatus("previewing lane label…", "");
      postPreviewDoc()
        .then(function (receipt) {
          if (receipt.ok) {
            markDirty();
            var msg = "Updated lane " + (lane.id || edit.laneIndex) + " label (unsaved)";
            if (receipt.note) msg += "\nNote: " + receipt.note;
            setStatus(msg, "ok");
            return loadLayoutPane(true).then(function () {
              setLayoutBusy(false);
              renderAll();
            });
          }
          lane.label = prevLabel;
          revertHistoryPush();
          setLayoutBusy(false);
          var errs2 = receipt.errors || [receipt.error || "preview failed"];
          setStatus("Lane label not updated (reverted):\n- " + errs2.join("\n- "), "err");
          renderLists();
        })
        .catch(function (e) {
          lane.label = prevLabel;
          revertHistoryPush();
          setLayoutBusy(false);
          setStatus("Lane label preview failed (reverted): " + e, "err");
          renderLists();
        });
    }
  }

  function readNodeEditorValues() {
    return {
      type: String(($("layout-edit-type") && $("layout-edit-type").value) || "").trim(),
      label: String(($("layout-edit-label") && $("layout-edit-label").value) || "").trim(),
      sublabel: String(($("layout-edit-sublabel") && $("layout-edit-sublabel").value) || "").trim(),
      tag: String(($("layout-edit-tag") && $("layout-edit-tag").value) || "").trim(),
      brand: String(($("layout-edit-brand") && $("layout-edit-brand").value) || "").trim(),
    };
  }

  function nodeTextUnchanged(node, values, brandLocked) {
    if (String(node.type || "") !== values.type) return false;
    if (String(node.label || "") !== values.label) return false;
    var curSub = node.sublabel != null ? String(node.sublabel) : "";
    var curTag = node.tag != null ? String(node.tag) : "";
    if (curSub !== values.sublabel) return false;
    if (curTag !== values.tag) return false;
    if (brandLocked) return true;
    var curBrand = node.brand != null ? String(node.brand) : "";
    return curBrand === values.brand;
  }

  function applyNodeEditorValues(node, values, brandLocked) {
    if (values.type) node.type = values.type;
    node.label = values.label;
    if (values.sublabel) node.sublabel = values.sublabel;
    else delete node.sublabel;
    if (values.tag) node.tag = values.tag;
    else delete node.tag;
    if (!brandLocked) {
      if (values.brand) node.brand = values.brand;
      else delete node.brand;
    }
  }

  function commitNodeEditor() {
    if (!nodeEdit) return;
    var edit = nodeEdit;
    var values = readNodeEditorValues();
    // Clear session before hide so outside-click cannot re-enter.
    nodeEdit = null;
    hideNodeEditor();
    if (!values.label) {
      setStatus("Label required (minLength 1); kept previous", "");
      return;
    }
    if (!values.type) {
      setStatus("Type required; kept previous", "");
      return;
    }
    var node = findDocNode(edit.nodeId);
    if (!node) return;
    if (nodeTextUnchanged(node, values, edit.brandLocked)) {
      setStatus("Node unchanged", "");
      return;
    }
    pushHistory();
    applyNodeEditorValues(node, values, edit.brandLocked);
    state.rawDirty = false;
    renderLists();
    setLayoutBusy(true);
    setStatus("previewing node… " + edit.nodeId, "");
    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "Updated " + edit.nodeId + " (unsaved)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
          });
        }
        restoreNodeTextFields(node, edit.snap);
        revertHistoryPush();
        setLayoutBusy(false);
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("Node not updated (reverted):\n- " + errs.join("\n- "), "err");
        renderLists();
      })
      .catch(function (e) {
        restoreNodeTextFields(node, edit.snap);
        revertHistoryPush();
        setLayoutBusy(false);
        setStatus("Node preview failed (reverted): " + e, "err");
        renderLists();
      });
  }

  function occupiedCells() {
    var set = {};
    var nodes = (state.doc && state.doc.nodes) || [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!n || n.lane == null || n.col == null) continue;
      set[String(n.lane) + "\0" + String(n.col)] = true;
    }
    return set;
  }

  function firstFreeCell() {
    var lanes = (state.doc && state.doc.lanes) || [];
    var occupied = occupiedCells();
    var maxCol = 5;
    var cols = (state.layout && state.layout.columns) || [];
    if (cols.length) maxCol = Math.max(maxCol, cols.length - 1);
    var nodes = (state.doc && state.doc.nodes) || [];
    for (var ni = 0; ni < nodes.length; ni++) {
      var c = nodes[ni] && nodes[ni].col;
      if (typeof c === "number" && c > maxCol) maxCol = c;
    }
    for (var li = 0; li < lanes.length; li++) {
      var lid = lanes[li].id;
      if (lid == null || lid === "") continue;
      for (var col = 0; col <= maxCol; col++) {
        if (!occupied[String(lid) + "\0" + String(col)]) {
          return { lane: lid, col: col };
        }
      }
    }
    var lane0 = (lanes[0] && lanes[0].id) || "lane1";
    return { lane: lane0, col: maxCol + 1 };
  }

  function adjacentFreeCell(node) {
    var occupied = occupiedCells();
    var preferLane = node && node.lane != null ? node.lane : ((state.doc.lanes[0] && state.doc.lanes[0].id) || "lane1");
    var preferCol = (node && typeof node.col === "number" ? node.col : 0) + 1;
    if (!occupied[String(preferLane) + "\0" + String(preferCol)]) {
      return { lane: preferLane, col: preferCol };
    }
    return firstFreeCell();
  }

  function uniqueNodeId(prefix) {
    var ids = {};
    var nodes = (state.doc && state.doc.nodes) || [];
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i] && nodes[i].id) ids[String(nodes[i].id)] = true;
    }
    var n = nodes.length + 1;
    var cand = prefix + n;
    while (ids[cand]) {
      n += 1;
      cand = prefix + n;
    }
    return cand;
  }

  function uniqueCopyId(baseId) {
    var ids = {};
    var nodes = (state.doc && state.doc.nodes) || [];
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i] && nodes[i].id) ids[String(nodes[i].id)] = true;
    }
    var root = String(baseId || "node");
    var cand = root + "-copy";
    if (!ids[cand]) return cand;
    var n = 2;
    while (ids[root + "-copy" + n]) n += 1;
    cand = root + "-copy" + n;
    if (!ids[cand]) return cand;
    return uniqueNodeId("node");
  }

  function openNodeEditorById(nodeId) {
    var iframe = $("layout-frame");
    var doc = iframe && iframe.contentDocument;
    if (!doc) return;
    var handle = doc.querySelector('rect.bw-handle[data-node-id="' + nodeId + '"]');
    if (handle) openNodeEditor(handle);
  }

  function addLayoutNode() {
    if (!state.file || !state.doc || state.layoutBusy) return;
    ensureArrays();
    if (!(state.doc.lanes && state.doc.lanes.length)) {
      setStatus("Add node: diagram needs at least one lane", "err");
      return;
    }
    var cell = firstFreeCell();
    var id = uniqueNodeId("node");
    var item = {
      id: id,
      lane: cell.lane,
      col: cell.col,
      type: "backend",
      label: "New node",
    };
    pushHistory();
    state.doc.nodes.push(item);
    state.rawDirty = false;
    renderLists();
    setLayoutBusy(true);
    setStatus("previewing new node…", "");
    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "Added " + id + " (unsaved)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
            openNodeEditorById(id);
          });
        }
        state.doc.nodes.pop();
        revertHistoryPush();
        setLayoutBusy(false);
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("Add node failed (reverted):\n- " + errs.join("\n- "), "err");
        renderLists();
      })
      .catch(function (e) {
        state.doc.nodes.pop();
        revertHistoryPush();
        setLayoutBusy(false);
        setStatus("Add node preview failed (reverted): " + e, "err");
        renderLists();
      });
  }

  function duplicateLayoutNode() {
    if (!nodeEdit || !state.file || !state.doc || state.layoutBusy) return;
    var sourceId = nodeEdit.nodeId;
    var source = findDocNode(sourceId);
    if (!source) {
      setStatus("Duplicate: source node not found", "err");
      return;
    }
    // Commit editor session without saving field edits (duplicate uses current doc node).
    nodeEdit = null;
    hideNodeEditor();
    ensureArrays();
    var cell = adjacentFreeCell(source);
    var newId = uniqueCopyId(source.id || "node");
    var cloneNode = clone(source);
    cloneNode.id = newId;
    cloneNode.lane = cell.lane;
    cloneNode.col = cell.col;
    pushHistory();
    state.doc.nodes.push(cloneNode);
    state.rawDirty = false;
    renderLists();
    setLayoutBusy(true);
    setStatus("previewing duplicate…", "");
    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "Duplicated " + sourceId + " → " + newId + " (unsaved)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
            openNodeEditorById(newId);
          });
        }
        state.doc.nodes.pop();
        revertHistoryPush();
        setLayoutBusy(false);
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("Duplicate failed (reverted):\n- " + errs.join("\n- "), "err");
        renderLists();
      })
      .catch(function (e) {
        state.doc.nodes.pop();
        revertHistoryPush();
        setLayoutBusy(false);
        setStatus("Duplicate preview failed (reverted): " + e, "err");
        renderLists();
      });
  }

  function stripNodeIdRefs(doc, nodeId) {
    if (!doc || !nodeId) return;
    var id = String(nodeId);
    if (Array.isArray(doc.mainPath)) {
      doc.mainPath = doc.mainPath.filter(function (x) { return String(x) !== id; });
    }
    var sc = doc.semanticChecks;
    if (!sc || typeof sc !== "object") return;
    if (Array.isArray(sc.allowedRoots)) {
      sc.allowedRoots = sc.allowedRoots.filter(function (x) { return String(x) !== id; });
    }
    if (Array.isArray(sc.allowedTerminals)) {
      sc.allowedTerminals = sc.allowedTerminals.filter(function (x) { return String(x) !== id; });
    }
    if (Array.isArray(sc.requiredEdges)) {
      sc.requiredEdges = sc.requiredEdges.filter(function (pair) {
        if (!pair || typeof pair !== "object") return true;
        return String(pair.from) !== id && String(pair.to) !== id;
      });
    }
    if (Array.isArray(sc.requiredPaths)) {
      sc.requiredPaths = sc.requiredPaths.filter(function (pair) {
        if (!pair || typeof pair !== "object") return true;
        return String(pair.from) !== id && String(pair.to) !== id;
      });
    }
  }

  function deleteLayoutNode() {
    if (!nodeEdit || !state.file || !state.doc || state.layoutBusy) return;
    ensureArrays();
    var nodes = state.doc.nodes || [];
    if (nodes.length <= 1) {
      setStatus("Cannot delete the last node (min 1 required)", "err");
      return;
    }
    var nodeId = nodeEdit.nodeId;
    var nodeIdx = -1;
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].id === nodeId) { nodeIdx = i; break; }
    }
    if (nodeIdx < 0) {
      setStatus("Delete: node not found", "err");
      return;
    }
    nodeEdit = null;
    hideNodeEditor();
    pushHistory();
    state.doc.nodes.splice(nodeIdx, 1);
    var keptEdges = [];
    var removedEdgeCount = 0;
    var edges = state.doc.edges || [];
    for (var e = 0; e < edges.length; e++) {
      if (String(edges[e].from) === String(nodeId) || String(edges[e].to) === String(nodeId)) {
        removedEdgeCount++;
      } else {
        keptEdges.push(edges[e]);
      }
    }
    state.doc.edges = keptEdges;
    stripNodeIdRefs(state.doc, nodeId);
    if (state.connectFrom && String(state.connectFrom) === String(nodeId)) {
      state.connectFrom = null;
    }
    state.selectedEdgeIndex = null;
    state.rawDirty = false;
    updateDeleteEdgeButton();
    updateConnectHighlight();
    updateLayoutHint();
    renderLists();
    setLayoutBusy(true);
    setStatus("previewing delete node " + nodeId + "…", "");
    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "Deleted " + nodeId;
          if (removedEdgeCount) msg += " (+" + removedEdgeCount + " edge" + (removedEdgeCount === 1 ? "" : "s") + ")";
          msg += " (unsaved)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
          });
        }
        if (state.undo.length) {
          state.doc = state.undo.pop();
          updateHistoryButtons();
        }
        setLayoutBusy(false);
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("Delete node failed (reverted):\n- " + errs.join("\n- "), "err");
        renderLists();
        return loadLayoutPane(true).then(function () { renderAll(); });
      })
      .catch(function (e) {
        if (state.undo.length) {
          state.doc = state.undo.pop();
          updateHistoryButtons();
        }
        setLayoutBusy(false);
        setStatus("Delete node preview failed (reverted): " + e, "err");
        renderLists();
        return loadLayoutPane(true).then(function () { renderAll(); });
      });
  }

  function onLayoutDblClick(ev) {
    if (state.layoutBusy || !state.layout) return;
    if (state.layoutMode !== "move") return;
    var t = ev.target;
    if (!t || !t.classList) return;
    if (layoutDrag) {
      layoutDrag.rect.setAttribute("x", String(layoutDrag.origX));
      layoutDrag.rect.setAttribute("y", String(layoutDrag.origY));
      layoutDrag.rect.style.cursor = handleCursor();
      try { layoutDrag.rect.releasePointerCapture(ev.pointerId); } catch (e) {}
      layoutDrag = null;
    }
    if (t.classList.contains("bw-edge-hit")) {
      ev.preventDefault();
      openEdgeLabelEditor(t);
      return;
    }
    if (t.classList.contains("bw-lane-hit")) {
      ev.preventDefault();
      openLaneLabelEditor(t);
      return;
    }
    if (!t.classList.contains("bw-handle")) return;
    ev.preventDefault();
    openNodeEditor(t);
  }

  function endpointSnapThreshold() {
    var nodes = (state.layout && state.layout.nodes) || [];
    var mins = [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!n) continue;
      var m = Math.min(Number(n.width) || 0, Number(n.height) || 0);
      if (m > 0) mins.push(m);
    }
    if (!mins.length) return 48;
    mins.sort(function (a, b) { return a - b; });
    var med = mins[Math.floor(mins.length / 2)];
    return Math.max(48, 0.5 * med);
  }

  function findDropNodeAt(x, y) {
    var nodes = (state.layout && state.layout.nodes) || [];
    var i, n, nx, ny, nw, nh;
    for (i = 0; i < nodes.length; i++) {
      n = nodes[i];
      if (!n || !n.id) continue;
      nx = Number(n.x); ny = Number(n.y);
      nw = Number(n.width); nh = Number(n.height);
      if (!(nw > 0) || !(nh > 0)) continue;
      if (x >= nx && x <= nx + nw && y >= ny && y <= ny + nh) return n.id;
    }
    var threshold = endpointSnapThreshold();
    var bestId = null, bestD = Infinity;
    for (i = 0; i < nodes.length; i++) {
      n = nodes[i];
      if (!n || !n.id) continue;
      var cx = Number(n.x) + Number(n.width) / 2;
      var cy = Number(n.y) + Number(n.height) / 2;
      var dx = x - cx, dy = y - cy;
      var d = Math.sqrt(dx * dx + dy * dy);
      if (d < bestD) { bestD = d; bestId = n.id; }
    }
    if (bestId != null && bestD <= threshold) return bestId;
    return null;
  }

  function rerouteEdgeEnd(docIdx, end, newNodeId) {
    if (!state.doc.edges || !state.doc.edges[docIdx]) {
      mountLayoutOverlays();
      return;
    }
    if (end !== "from" && end !== "to") {
      mountLayoutOverlays();
      return;
    }
    var edge = state.doc.edges[docIdx];
    var prev = edge[end];
    if (String(prev) === String(newNodeId)) {
      mountLayoutOverlays();
      setStatus("No reroute (same node)", "");
      return;
    }
    var newFrom = end === "from" ? newNodeId : edge.from;
    var newTo = end === "to" ? newNodeId : edge.to;
    if (String(newFrom) === String(newTo)) {
      mountLayoutOverlays();
      setStatus("Self-loop refused (from === to)", "err");
      return;
    }
    if (edgePairExistsExcluding(newFrom, newTo, docIdx)) {
      mountLayoutOverlays();
      setStatus("Duplicate edge refused: " + newFrom + " → " + newTo, "err");
      return;
    }
    pushHistory();
    edge[end] = newNodeId;
    state.rawDirty = false;
    state.selectedEdgeIndex = null;
    updateDeleteEdgeButton();
    renderLists();
    setLayoutBusy(true);
    setStatus("previewing reroute → " + newFrom + " → " + newTo + "…", "");
    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "Rerouted edge " + newFrom + " → " + newTo + " (" + end + ") (unsaved)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
          });
        }
        edge[end] = prev;
        revertHistoryPush();
        setLayoutBusy(false);
        mountLayoutOverlays();
        var errs = receipt.errors || [receipt.error || "preview failed"];
        setStatus("Reroute not updated (reverted):\n- " + errs.join("\n- "), "err");
        renderLists();
      })
      .catch(function (e) {
        edge[end] = prev;
        revertHistoryPush();
        setLayoutBusy(false);
        mountLayoutOverlays();
        setStatus("Reroute preview failed (reverted): " + e, "err");
        renderLists();
      });
  }

  function onLayoutPointerDown(ev) {
    if (state.layoutBusy || !state.layout) return;
    if (nodeEdit || singleEdit) cancelInlineEditors();
    var t = ev.target;

    // Endpoint handles first — never trigger add-edge / edge-select.
    if (t && t.classList && t.classList.contains("bw-endpoint")) {
      ev.preventDefault();
      clearConnectFrom();
      var iframeEp = $("layout-frame");
      var svgEp = iframeEp.contentDocument && iframeEp.contentDocument.querySelector("svg");
      if (!svgEp) return;
      var docIdxEp = parseInt(t.getAttribute("data-doc-index"), 10);
      var endEp = t.getAttribute("data-end");
      if (isNaN(docIdxEp) || (endEp !== "from" && endEp !== "to")) return;
      if (!(state.doc.edges && state.doc.edges[docIdxEp])) return;
      try { t.setPointerCapture(ev.pointerId); } catch (eEp) {}
      var ptEp = clientToSvg(svgEp, ev.clientX, ev.clientY);
      endpointDrag = {
        docIdx: docIdxEp,
        end: endEp,
        circle: t,
        svg: svgEp,
        origCx: Number(t.getAttribute("cx")),
        origCy: Number(t.getAttribute("cy")),
        lastX: ptEp.x,
        lastY: ptEp.y,
        startClientX: ev.clientX,
        startClientY: ev.clientY,
        moved: false,
      };
      return;
    }

    if (t && t.classList && t.classList.contains("bw-edge-hit")) {
      // Do not preventDefault — that suppresses dblclick (edge label edit).
      var edgeIdx = parseInt(t.getAttribute("data-doc-index"), 10);
      selectLayoutEdge(edgeIdx);
      return;
    }

    if (t && t.classList && t.classList.contains("bw-lane-hit")) {
      if (state.selectedEdgeIndex != null) {
        clearEdgeSelection(true);
        setStatus("Edge selection cleared", "");
      }
      return;
    }

    if (!t || !t.classList || !t.classList.contains("bw-handle")) {
      if (state.selectedEdgeIndex != null) {
        clearEdgeSelection(true);
        setStatus("Edge selection cleared", "");
      }
      return;
    }

    if (state.layoutMode === "connect") {
      ev.preventDefault();
      handleConnectNodeClick(t.getAttribute("data-node-id"));
      return;
    }

    var iframe = $("layout-frame");
    var svg = iframe.contentDocument && iframe.contentDocument.querySelector("svg");
    if (!svg) return;
    var id = t.getAttribute("data-node-id");
    var node = findDocNode(id);
    if (!node) return;
    // Do not preventDefault here — that suppresses dblclick (inline label edit).
    try { t.setPointerCapture(ev.pointerId); } catch (e) {}
    var pt = clientToSvg(svg, ev.clientX, ev.clientY);
    layoutDrag = {
      id: id,
      rect: t,
      svg: svg,
      ox: pt.x - Number(t.getAttribute("x")),
      oy: pt.y - Number(t.getAttribute("y")),
      origX: Number(t.getAttribute("x")),
      origY: Number(t.getAttribute("y")),
      origLane: node.lane,
      origCol: node.col,
      lastX: pt.x,
      lastY: pt.y,
      startClientX: ev.clientX,
      startClientY: ev.clientY,
      moved: false,
    };
  }

  function onLayoutPointerMove(ev) {
    if (endpointDrag) {
      var edx = ev.clientX - endpointDrag.startClientX;
      var edy = ev.clientY - endpointDrag.startClientY;
      if (!endpointDrag.moved) {
        if ((edx * edx + edy * edy) < (LAYOUT_DRAG_THRESHOLD_PX * LAYOUT_DRAG_THRESHOLD_PX)) return;
        endpointDrag.moved = true;
        ev.preventDefault();
        endpointDrag.circle.style.cursor = "grabbing";
        setStatus("Rerouting " + endpointDrag.end + " endpoint…", "");
      }
      var ept = clientToSvg(endpointDrag.svg, ev.clientX, ev.clientY);
      endpointDrag.lastX = ept.x;
      endpointDrag.lastY = ept.y;
      endpointDrag.circle.setAttribute("cx", String(ept.x));
      endpointDrag.circle.setAttribute("cy", String(ept.y));
      return;
    }
    if (!layoutDrag) return;
    var dx = ev.clientX - layoutDrag.startClientX;
    var dy = ev.clientY - layoutDrag.startClientY;
    if (!layoutDrag.moved) {
      if ((dx * dx + dy * dy) < (LAYOUT_DRAG_THRESHOLD_PX * LAYOUT_DRAG_THRESHOLD_PX)) return;
      layoutDrag.moved = true;
      ev.preventDefault();
      layoutDrag.rect.style.cursor = "grabbing";
      setStatus("Dragging " + layoutDrag.id + "…", "");
    }
    var pt = clientToSvg(layoutDrag.svg, ev.clientX, ev.clientY);
    layoutDrag.lastX = pt.x;
    layoutDrag.lastY = pt.y;
    layoutDrag.rect.setAttribute("x", String(pt.x - layoutDrag.ox));
    layoutDrag.rect.setAttribute("y", String(pt.y - layoutDrag.oy));
  }

  function onLayoutPointerUp(ev) {
    if (endpointDrag) {
      var epDrag = endpointDrag;
      endpointDrag = null;
      epDrag.circle.style.cursor = "grab";
      try { epDrag.circle.releasePointerCapture(ev.pointerId); } catch (eEpUp) {}
      if (!epDrag.moved) {
        epDrag.circle.setAttribute("cx", String(epDrag.origCx));
        epDrag.circle.setAttribute("cy", String(epDrag.origCy));
        return;
      }
      var targetId = findDropNodeAt(epDrag.lastX, epDrag.lastY);
      if (!targetId) {
        mountLayoutOverlays();
        setStatus("Reroute cancelled (drop on empty space)", "");
        return;
      }
      rerouteEdgeEnd(epDrag.docIdx, epDrag.end, targetId);
      return;
    }
    if (!layoutDrag) return;
    var drag = layoutDrag;
    layoutDrag = null;
    drag.rect.style.cursor = handleCursor();
    try { drag.rect.releasePointerCapture(ev.pointerId); } catch (e) {}
    if (!drag.moved) {
      drag.rect.setAttribute("x", String(drag.origX));
      drag.rect.setAttribute("y", String(drag.origY));
      return;
    }
    var snap = snapLaneCol(drag.lastX, drag.lastY, state.layout);
    var node = findDocNode(drag.id);
    if (!node) return;
    if (String(node.lane) === String(snap.lane) && Number(node.col) === Number(snap.col)) {
      drag.rect.setAttribute("x", String(drag.origX));
      drag.rect.setAttribute("y", String(drag.origY));
      setStatus("No move (same lane/col)", "");
      return;
    }
    pushHistory();
    node.lane = snap.lane;
    node.col = snap.col;
    state.rawDirty = false;
    renderLists();
    saveLayoutDrop(drag, snap);
  }

  function setLayoutBusy(busy) {
    state.layoutBusy = busy;
    applyOverlayInteractionStyles();
    syncQualityToggle();
    updateDirtyUI();
  }

  function saveLayoutDrop(drag, snap) {
    setLayoutBusy(true);
    setStatus("previewing… " + drag.id + " → lane " + snap.lane + " col " + snap.col, "");
    postPreviewDoc()
      .then(function (receipt) {
        if (receipt.ok) {
          markDirty();
          var msg = "Moved " + drag.id + " → lane " + snap.lane + " col " + snap.col + " (unsaved)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return loadLayoutPane(true).then(function () {
            setLayoutBusy(false);
            renderAll();
          });
        }
        var errs = receipt.errors || [receipt.error || "preview failed"];
        var node = findDocNode(drag.id);
        if (node) {
          node.lane = drag.origLane;
          node.col = drag.origCol;
        }
        revertHistoryPush();
        drag.rect.setAttribute("x", String(drag.origX));
        drag.rect.setAttribute("y", String(drag.origY));
        setLayoutBusy(false);
        setStatus("Move not updated (reverted):\n- " + errs.join("\n- "), "err");
        renderLists();
      })
      .catch(function (e) {
        var node = findDocNode(drag.id);
        if (node) {
          node.lane = drag.origLane;
          node.col = drag.origCol;
        }
        revertHistoryPush();
        drag.rect.setAttribute("x", String(drag.origX));
        drag.rect.setAttribute("y", String(drag.origY));
        setLayoutBusy(false);
        setStatus("Move preview failed (reverted): " + e, "err");
        renderLists();
      });
  }

  function loadLayoutPane(force) {
    if (!state.archify) {
      $("tab-layout").classList.add("hidden");
      return Promise.resolve();
    }
    $("tab-layout").classList.remove("hidden");
    if (state.layoutLoaded && !force && state.tab !== "layout") {
      return Promise.resolve();
    }
    cancelInlineEditors();
    var stash = null;
    var iframePre = $("layout-frame");
    if (
      iframePre.contentDocument &&
      iframePre.contentDocument.getElementById("bw-scroll") &&
      state.layoutZoom != null
    ) {
      stash = stashLayoutViewport();
    }
    var hintLoading = $("layout-hint");
    if (hintLoading) {
      hintLoading.textContent = "Loading diagram…";
      hintLoading.title = "Loading diagram…";
    }
    return Promise.all([
      apiFetch("/api/diagram").then(function (r) {
        if (r.status === 404) return r.json().then(function (j) { throw new Error(j.error || "diagram unavailable"); });
        if (!r.ok) throw new Error("diagram HTTP " + r.status);
        return r.text();
      }),
      apiFetch("/api/layout").then(function (r) {
        return r.json().then(function (j) {
          if (r.status === 404 || j.archify === false) throw new Error("archify unavailable");
          if (j.ok === false) throw new Error(j.error || "layout failed");
          return j;
        });
      }),
    ])
      .then(function (pair) {
        var html = pair[0];
        state.layout = pair[1];
        state.layoutLoaded = true;
        var iframe = $("layout-frame");
        // Resolve only after iframe onload + overlays mount so callers
        // (setLayoutBusy false, subsequent dblclick) are not racing remount.
        return new Promise(function (resolve, reject) {
          iframe.onload = function () {
            try {
              prepareDiagramViewport(iframe.contentDocument);
              restoreLayoutViewport(stash);
              // Selection is layout-index based; clear across remount so stale indexes cannot delete wrong edges.
              state.selectedEdgeIndex = null;
              state.connectFrom = null;
              mountLayoutOverlays();
              updateModeButtons();
              updateDeleteEdgeButton();
              updateLayoutHint();
              resolve();
            } catch (err) {
              reject(err);
            }
          };
          iframe.srcdoc = html;
        });
      })
      .catch(function (e) {
        var hintErr = $("layout-hint");
        var offline = !!(e && (e.bendwrightOffline || /Failed to fetch/i.test(String(e.message || e))));
        var msg = offline ? SERVER_GONE_MSG : ("Layout unavailable: " + e.message);
        if (hintErr) {
          hintErr.textContent = msg;
          hintErr.title = msg;
        }
        setStatus(msg, "err");
      });
  }

  function addItem(kind) {
    if (!state.file || !state.doc) {
      setStatus("Open a diagram first", "err");
      return;
    }
    pushHistory();
    ensureArrays();
    var item;
    if (kind === "nodes") {
      var lane0 = (state.doc.lanes[0] && state.doc.lanes[0].id) || "lane1";
      item = { id: "node" + (state.doc.nodes.length + 1), lane: lane0, col: 0, type: "backend", label: "New node" };
    } else if (kind === "edges") {
      var a = (state.doc.nodes[0] && state.doc.nodes[0].id) || "";
      var b = (state.doc.nodes[1] && state.doc.nodes[1].id) || a;
      item = { from: a, to: b, role: "main" };
    } else {
      item = { id: "lane" + (state.doc.lanes.length + 1), label: "New lane" };
    }
    state.doc[kind].push(item);
    state.selected[kind] = state.doc[kind].length - 1;
    state.rawDirty = false;
    markDirty();
    renderAll();
  }

  function removeSelected(kind) {
    var idx = state.selected[kind];
    if (idx < 0) return;
    pushHistory();
    state.doc[kind].splice(idx, 1);
    state.selected[kind] = Math.min(idx, state.doc[kind].length - 1);
    state.rawDirty = false;
    markDirty();
    renderAll();
  }

  function onFieldChange(kind, fieldEl) {
    var idx = state.selected[kind];
    if (idx < 0) return;
    var field = fieldEl.getAttribute("data-field");
    if (!field) return;
    pushHistory();
    var item = state.doc[kind][idx];
    var val = fieldEl.value;
    if (field === "col") {
      var n = parseInt(val, 10);
      if (isNaN(n)) n = 0;
      if (n < 0) n = 0;
      if (n > 5) n = 5;
      item.col = n;
      fieldEl.value = String(n);
    } else if (val === "" && (field === "role" || field === "variant" || field === "route" ||
                              field === "fromSide" || field === "toSide" || field === "label")) {
      delete item[field];
    } else {
      item[field] = val;
    }
    state.rawDirty = false;
    markDirty();
    renderLists();
    // keep form focus-friendly: re-render list only; refresh raw later
    if (field === "id" || field === "from" || field === "to" || field === "lane" || field === "label") {
      renderForm(kind);
      var again = $("form-" + kind).querySelector('[data-field="' + field + '"]');
      if (again) again.focus();
    }
    updateDirtyUI();
  }

  function applyRaw(showStatus) {
    var text = $("raw-editor").value;
    try {
      var parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setStatus("Raw JSON root must be an object", "err");
        return false;
      }
      pushHistory();
      state.doc = parsed;
      ensureArrays();
      state.rawDirty = false;
      state.selected = { nodes: -1, edges: -1, lanes: -1 };
      syncDirtyFromDoc();
      renderAll();
      if (showStatus) setStatus(state.dirty ? "Applied raw JSON (unsaved)" : "Applied raw JSON", "ok");
      return true;
    } catch (e) {
      setStatus("Invalid JSON: " + e.message, "err");
      return false;
    }
  }

  function save() {
    if (!state.file || !state.doc) {
      setStatus("Save: no file open", "err");
      return Promise.resolve(false);
    }
    if (state.tab === "raw" && state.rawDirty) {
      if (!applyRaw(false)) return Promise.resolve(false);
    }
    setStatus("Saving…", "");
    return apiFetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.doc),
    })
      .then(function (r) { return r.json(); })
      .then(function (receipt) {
        if (receipt.ok && receipt.saved) {
          clearDirty();
          var msg = receipt.preview ? "Saved (validated + preview updated)" : "Saved (structural check passed)";
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          if (state.archify) {
            state.layoutLoaded = false;
            if (state.tab === "layout") loadLayoutPane(true);
          }
          return true;
        }
        var errs = receipt.errors || [receipt.error || "save failed"];
        setStatus("Not saved:\n- " + errs.join("\n- "), "err");
        return false;
      })
      .catch(function (e) {
        setStatus("Save request failed: " + e, "err");
        return false;
      });
  }

  function exportHtml() {
    if (!state.file || !state.doc) {
      setStatus("Export: open a file first", "err");
      return Promise.resolve(false);
    }
    if (!state.archify) {
      setStatus("archify not available; cannot render HTML", "err");
      return Promise.resolve(false);
    }
    if (state.tab === "raw" && state.rawDirty) {
      if (!applyRaw(false)) return Promise.resolve(false);
    }
    setStatus("Exporting…", "");
    var needSave = !!(state.dirty || state.rawDirty);
    var opts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: needSave ? JSON.stringify(state.doc) : "{}",
    };
    return apiFetch("/api/export", opts)
      .then(function (r) { return r.json(); })
      .then(function (receipt) {
        if (receipt.ok && receipt.output) {
          if (needSave) clearDirty();
          var msg = "Exported " + receipt.output;
          if (receipt.note) msg += "\nNote: " + receipt.note;
          setStatus(msg, "ok");
          return true;
        }
        var errs = receipt.errors || [receipt.error || "export failed"];
        setStatus(errs.join("\n"), "err");
        return false;
      })
      .catch(function (e) {
        setStatus("Export request failed: " + e, "err");
        return false;
      });
  }

  function loadState() {
    return apiFetch("/api/state")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.file = data.file || null;
        state.diagram_type = data.diagram_type;
        state.doc = data.doc || null;
        state.enums = data.enums || {};
        state.archify = data.archify || null;
        if (!state.file || !state.doc) {
          state.file = null;
          state.doc = null;
          clearDirty();
          showLayoutTabIfReady();
          renderAll();
          preferLayoutTab();
          setStatus("No file open — use Open / Browse", "");
          openOpenPanel();
          return;
        }
        ensureArrays();
        clearDirty();
        showLayoutTabIfReady();
        renderAll();
        preferLayoutTab();
        setStatus("Loaded " + state.file, "ok");
      });
  }

  function isOpenPanelActive() {
    var panel = $("open-panel");
    return !!(panel && panel.classList.contains("active"));
  }

  function closeOpenPanel() {
    var panel = $("open-panel");
    if (!panel) return;
    panel.classList.remove("active");
    panel.setAttribute("aria-hidden", "true");
  }

  function openOpenPanel() {
    var panel = $("open-panel");
    if (!panel) return;
    panel.classList.add("active");
    panel.setAttribute("aria-hidden", "false");
    $("open-path-input").value = state.file || "";
    $("open-path-input").focus();
    $("open-path-input").select();
  }

  function pickAndOpenDiagram() {
    var runPick = function () {
      var browseBtn = $("btn-open-browse");
      if (browseBtn) browseBtn.disabled = true;
      setStatus("Opening file picker…", "");
      return apiFetch("/api/pick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
        .then(function (r) {
          return r.json().then(function (j) {
            if (!r.ok) throw new Error(j.error || ("pick HTTP " + r.status));
            return j;
          });
        })
        .then(function (data) {
          if (!data || data.ok === false) {
            setStatus(
              (data && data.error) || "Native picker unavailable — paste a path",
              "err"
            );
            $("open-path-input").focus();
            return;
          }
          if (data.cancelled) {
            setStatus("Open cancelled", "");
            return;
          }
          if (!data.path) {
            setStatus("Native picker unavailable — paste a path", "err");
            $("open-path-input").focus();
            return;
          }
          $("open-path-input").value = data.path;
          return openDiagramPath(data.path, { skipDirtyCheck: true });
        })
        .catch(function (e) {
          setStatus("Native picker unavailable — paste a path (" + e.message + ")", "err");
          $("open-path-input").focus();
        })
        .then(function () {
          if (browseBtn) browseBtn.disabled = false;
        });
    };
    whenClean(runPick);
  }

  function applyOpenedState(data) {
    cancelInlineEditors();
    layoutDrag = null;
    endpointDrag = null;
    state.file = data.file || null;
    state.diagram_type = data.diagram_type;
    state.doc = data.doc || null;
    state.enums = data.enums || {};
    state.archify = data.archify || null;
    state.undo = [];
    state.redo = [];
    state.rawDirty = false;
    state.selected = { nodes: -1, edges: -1, lanes: -1 };
    state.selectedEdgeIndex = null;
    state.connectFrom = null;
    state.layoutMode = "move";
    state.layout = null;
    state.layoutLoaded = false;
    state.layoutZoom = null;
    state.layoutBusy = false;
    if (state.doc) ensureArrays();
    clearDirty();
    showLayoutTabIfReady();
    updateModeButtons();
    updateDeleteEdgeButton();
    updateLayoutHint();
    syncQualityToggle();
    renderAll();
    var iframe = $("layout-frame");
    if (iframe) iframe.srcdoc = "";
    preferLayoutTab();
    setStatus(state.file ? ("Opened " + state.file) : "No file open", "ok");
  }

  function isDirtyPanelActive() {
    var panel = $("dirty-panel");
    return !!(panel && panel.classList.contains("active"));
  }

  function closeDirtyPanel() {
    var panel = $("dirty-panel");
    if (!panel) return;
    panel.classList.remove("active");
    panel.setAttribute("aria-hidden", "true");
    pendingDirtyAction = null;
  }

  function openDirtyPanel(nextAction) {
    pendingDirtyAction = nextAction || null;
    var panel = $("dirty-panel");
    if (!panel) return;
    panel.classList.add("active");
    panel.setAttribute("aria-hidden", "false");
  }

  function whenClean(nextAction) {
    if (!state.dirty) {
      nextAction();
      return;
    }
    openDirtyPanel(nextAction);
  }

  function discardChanges() {
    if (!state.file) {
      setStatus("Discard: no file loaded", "err");
      return Promise.resolve(false);
    }
    setStatus("Discarding unsaved changes…", "");
    return apiFetch("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: String(state.file) }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok || j.ok === false || !j.doc) {
            throw new Error(j.error || ("open HTTP " + r.status));
          }
          return j;
        });
      })
      .then(function (data) {
        applyOpenedState(data);
        setStatus("Discarded — reloaded " + state.file, "ok");
        return true;
      })
      .catch(function (e) {
        setStatus("Discard failed: " + e.message, "err");
        return false;
      });
  }

  function openDiagramPath(path, opts) {
    opts = opts || {};
    if (!path || !String(path).trim()) {
      setStatus("Open: path required", "err");
      return Promise.resolve();
    }
    var trimmed = String(path).trim();
    var doOpen = function () {
      $("btn-open-go").disabled = true;
      return apiFetch("/api/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: trimmed }),
      })
        .then(function (r) {
          return r.json().then(function (j) {
            if (!r.ok || j.ok === false || !j.doc) {
              throw new Error(j.error || ("open HTTP " + r.status));
            }
            return j;
          });
        })
        .then(function (data) {
          applyOpenedState(data);
          closeOpenPanel();
        })
        .catch(function (e) {
          setStatus("Open failed: " + e.message, "err");
        })
        .then(function () {
          $("btn-open-go").disabled = false;
        });
    };
    if (!opts.skipDirtyCheck && state.dirty) {
      whenClean(doOpen);
      return Promise.resolve();
    }
    return doOpen();
  }

  // Events
  document.querySelector(".tabs").addEventListener("click", function (ev) {
    var btn = ev.target.closest("button[data-tab]");
    if (!btn) return;
    switchTab(btn.getAttribute("data-tab"));
  });

  ["nodes", "edges", "lanes"].forEach(function (kind) {
    $("list-" + kind).addEventListener("click", function (ev) {
      var item = ev.target.closest(".list-item");
      if (!item) return;
      var idx = parseInt(item.getAttribute("data-index"), 10);
      if (idx === -1) {
        addItem(kind);
        return;
      }
      state.selected[kind] = idx;
      renderLists();
      renderForm(kind);
    });

    $("form-" + kind).addEventListener("change", function (ev) {
      var el = ev.target;
      if (el && el.getAttribute("data-field")) onFieldChange(kind, el);
    });
    $("form-" + kind).addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-action='remove']");
      if (btn) removeSelected(kind);
    });
  });

  $("btn-open").addEventListener("click", function () {
    if (isOpenPanelActive()) closeOpenPanel();
    else whenClean(openOpenPanel);
  });
  $("btn-open-close").addEventListener("click", closeOpenPanel);
  $("btn-open-browse").addEventListener("click", function () {
    pickAndOpenDiagram();
  });
  $("btn-open-go").addEventListener("click", function () {
    openDiagramPath($("open-path-input").value);
  });
  $("open-path-input").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      openDiagramPath($("open-path-input").value);
    } else if (ev.key === "Escape" || ev.key === "Esc") {
      ev.preventDefault();
      closeOpenPanel();
    }
  });
  document.addEventListener("mousedown", function (ev) {
    if (isStatusOverlayActive()) {
      var statusPanel = $("status-overlay");
      var statusBar = $("status-bar");
      if (statusPanel && !statusPanel.contains(ev.target) &&
          !(statusBar && statusBar.contains(ev.target))) {
        closeStatusOverlay();
      }
    }
    if (isDirtyPanelActive()) {
      var dirtyPanel = $("dirty-panel");
      if (dirtyPanel && !dirtyPanel.contains(ev.target)) return;
    }
    if (!isOpenPanelActive()) return;
    var panel = $("open-panel");
    var openBtn = $("btn-open");
    if (panel.contains(ev.target) || (openBtn && openBtn.contains(ev.target))) return;
    closeOpenPanel();
  });

  $("btn-status-close").addEventListener("click", closeStatusOverlay);
  $("status").addEventListener("click", function () {
    if ($("status-bar") && $("status-bar").classList.contains("status-clipped")) {
      openStatusOverlay();
    }
  });
  $("status-more").addEventListener("click", function (ev) {
    ev.stopPropagation();
    openStatusOverlay();
  });
  window.addEventListener("resize", function () {
    if ($("status") && ($("status").textContent || "")) measureStatusClip();
  });

  $("btn-dirty-close").addEventListener("click", closeDirtyPanel);
  $("btn-dirty-cancel").addEventListener("click", closeDirtyPanel);
  $("btn-dirty-save").addEventListener("click", function () {
    var next = pendingDirtyAction;
    save().then(function (ok) {
      if (!ok) return;
      pendingDirtyAction = null;
      closeDirtyPanel();
      if (typeof next === "function") next();
    });
  });
  $("btn-dirty-discard").addEventListener("click", function () {
    var next = pendingDirtyAction;
    discardChanges().then(function (ok) {
      if (!ok) return;
      pendingDirtyAction = null;
      closeDirtyPanel();
      if (typeof next === "function") next();
    });
  });

  $("btn-save").addEventListener("click", save);
  $("btn-export").addEventListener("click", function () {
    if ($("btn-export").disabled) return;
    exportHtml();
  });
  $("btn-discard").addEventListener("click", function () {
    if (!state.dirty || state.layoutBusy) return;
    discardChanges();
  });
  $("btn-undo").addEventListener("click", undo);
  $("btn-redo").addEventListener("click", redo);
  $("btn-raw-apply").addEventListener("click", function () { applyRaw(true); });

  window.addEventListener("beforeunload", function (ev) {
    if (!state.dirty || !state.file) return;
    ev.preventDefault();
    ev.returnValue = "";
  });

  // M22: one presence stream. Stays up while the tab exists (even hidden); auto-reconnects.
  try {
    window.__bendwrightAlive = new EventSource("/api/alive");
  } catch (e) {}

  $("btn-mode-move").addEventListener("click", function () {
    setLayoutMode("move");
    setStatus("Layout mode: Move", "");
  });
  $("btn-mode-connect").addEventListener("click", function () {
    setLayoutMode("connect");
    setStatus("Layout mode: Connect", "");
  });
  $("btn-quality-standard").addEventListener("click", function () {
    setQualityProfile("standard");
  });
  $("btn-quality-showcase").addEventListener("click", function () {
    setQualityProfile("showcase");
  });
  $("btn-delete-edge").addEventListener("click", function () {
    deleteSelectedEdge();
  });
  $("btn-add-node").addEventListener("click", function () {
    addLayoutNode();
  });

  $("btn-zoom-fit").addEventListener("click", function () {
    if (!$("layout-frame").contentDocument || !$("layout-frame").contentDocument.querySelector("svg")) return;
    fitLayoutViewport();
  });
  $("btn-zoom-out").addEventListener("click", function () {
    if (state.layoutZoom == null) return;
    applyLayoutZoom(state.layoutZoom / LAYOUT_ZOOM_STEP);
  });
  $("btn-zoom-in").addEventListener("click", function () {
    if (state.layoutZoom == null) return;
    applyLayoutZoom(state.layoutZoom * LAYOUT_ZOOM_STEP);
  });
  $("btn-zoom-100").addEventListener("click", function () {
    if (!$("layout-frame").contentDocument || !$("layout-frame").contentDocument.querySelector("svg")) return;
    applyLayoutZoom(1);
  });

  function onNodeEditorKeydown(ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      commitNodeEditor();
    } else if (ev.key === "Escape" || ev.key === "Esc") {
      ev.preventDefault();
      cancelNodeEditor();
      setStatus("Node edit cancelled", "");
    }
  }
  ["layout-edit-label", "layout-edit-sublabel", "layout-edit-tag", "layout-edit-brand", "layout-edit-type"].forEach(function (id) {
    var el = $(id);
    if (el) el.addEventListener("keydown", onNodeEditorKeydown);
  });
  $("layout-edit-save").addEventListener("click", function (ev) {
    ev.preventDefault();
    commitNodeEditor();
  });
  $("layout-edit-duplicate").addEventListener("click", function (ev) {
    ev.preventDefault();
    duplicateLayoutNode();
  });
  $("layout-edit-delete").addEventListener("click", function (ev) {
    ev.preventDefault();
    deleteLayoutNode();
  });

  function onSingleEditorKeydown(ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      commitSingleEditor();
    } else if (ev.key === "Escape" || ev.key === "Esc") {
      ev.preventDefault();
      cancelSingleEditor();
      setStatus("Label edit cancelled", "");
    }
  }
  var singleInput = $("layout-single-label");
  if (singleInput) singleInput.addEventListener("keydown", onSingleEditorKeydown);
  $("layout-single-save").addEventListener("click", function (ev) {
    ev.preventDefault();
    commitSingleEditor();
  });

  // Outside click cancels; do NOT commit-on-blur (Tab between fields would save early).
  document.addEventListener("mousedown", function (ev) {
    if (nodeEdit) {
      var panel = $("layout-node-editor");
      if (panel && panel.classList.contains("active") && !panel.contains(ev.target)) {
        cancelNodeEditor();
        setStatus("Node edit cancelled", "");
      }
    }
    if (singleEdit) {
      var spanel = $("layout-single-editor");
      if (spanel && spanel.classList.contains("active") && !spanel.contains(ev.target)) {
        cancelSingleEditor();
        setStatus("Label edit cancelled", "");
      }
    }
  });

  $("raw-editor").addEventListener("input", function () {
    state.rawDirty = true;
  });
  $("raw-editor").addEventListener("blur", function () {
    if (state.rawDirty) applyRaw(true);
  });

  // Capture-phase: status overlay Esc wins over connect/reroute/node-edit handlers.
  document.addEventListener("keydown", function (ev) {
    if ((ev.key === "Escape" || ev.key === "Esc") && isStatusOverlayActive()) {
      ev.preventDefault();
      ev.stopPropagation();
      closeStatusOverlay();
    }
  }, true);

  document.addEventListener("keydown", function (ev) {
    var tag = (ev.target && ev.target.tagName) ? ev.target.tagName.toLowerCase() : "";
    var typing = tag === "input" || tag === "textarea" || tag === "select" || (ev.target && ev.target.isContentEditable);

    if (!typing && (ev.key === "Escape" || ev.key === "Esc")) {
      if (isDirtyPanelActive()) {
        ev.preventDefault();
        closeDirtyPanel();
        return;
      }
      if (isOpenPanelActive()) {
        ev.preventDefault();
        closeOpenPanel();
        return;
      }
      if (endpointDrag) {
        ev.preventDefault();
        endpointDrag = null;
        mountLayoutOverlays();
        setStatus("Reroute cancelled", "");
        return;
      }
      if (state.connectFrom) {
        ev.preventDefault();
        clearConnectFrom();
        setStatus("Connect cancelled", "");
        return;
      }
    }
    if (!typing && (ev.key === "Delete" || ev.key === "Backspace")) {
      // Prefer nodeEdit when the editor is open; skip when focus is in an input/select
      // so Backspace still edits Label text (typing already filtered above).
      if (state.tab === "layout" && nodeEdit) {
        ev.preventDefault();
        deleteLayoutNode();
        return;
      }
      if (state.tab === "layout" && state.selectedEdgeIndex != null) {
        ev.preventDefault();
        deleteSelectedEdge();
        return;
      }
    }

    var mod = ev.ctrlKey || ev.metaKey;
    if (!mod) return;
    var key = ev.key.toLowerCase();
    if (key === "s") {
      ev.preventDefault();
      save();
    } else if (key === "z" && !ev.shiftKey) {
      ev.preventDefault();
      undo();
    } else if (key === "y" || (key === "z" && ev.shiftKey)) {
      ev.preventDefault();
      redo();
    }
  });

  loadState().catch(function (e) {
    setStatus("Failed to load /api/state: " + e, "err");
  });
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
SUPPORTED_TYPES = {
    "workflow": "workflow",
    # future: "architecture": "architecture",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=APP,
        description="Local offline editor for Archify diagram IR JSON.",
    )
    p.add_argument(
        "type_or_path",
        nargs="?",
        default=None,
        help='diagram type ("workflow") or path to IR JSON; omit both for empty start',
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="path to the IR JSON file (when type is given first)",
    )
    p.add_argument(
        "--type",
        dest="type_override",
        default=None,
        help="diagram type override (otherwise inferred from the file)",
    )
    p.add_argument(
        "--archify",
        default=None,
        help="path to archify.mjs (explicit override; auto-detect otherwise)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8770,
        help="loopback port (default 8770)",
    )
    p.add_argument(
        "--keep-alive",
        action="store_true",
        help="disable auto-exit when the browser disconnects (SSE presence still served)",
    )
    return p.parse_args(argv)


def _presence_watchdog() -> None:
    """Shut down only after every /api/alive connection has been gone for GRACE.

    Does not arm on GET / and never takes _state_lock. --keep-alive disables this.
    """
    global _httpd
    while True:
        time.sleep(1.0)
        if not _auto_exit:
            continue
        with _alive_lock:
            seen = _alive_seen
            active = _alive
            zero_ts = _last_zero_ts
        if not seen or active != 0 or zero_ts is None:
            continue
        if (time.monotonic() - zero_ts) > ALIVE_GRACE_S:
            httpd = _httpd
            if httpd is not None:
                httpd.shutdown()
            return


def main(argv: list[str] | None = None) -> int:
    global _file_path, _diagram_type, _doc, _had_trailing_newline, _archify_path, _httpd, _auto_exit

    args = parse_args(argv)
    known = ", ".join(sorted(SUPPORTED_TYPES))
    file_path: Path | None = None
    dtype: str | None = None

    if args.path is not None:
        # Legacy: python bendwright.py workflow path.json
        if args.type_or_path not in SUPPORTED_TYPES:
            print(
                f"[{APP}] unknown type {args.type_or_path!r}; known: {known}",
                file=sys.stderr,
            )
            return 2
        dtype = SUPPORTED_TYPES[args.type_or_path]
        file_path = Path(args.path).resolve()
    elif args.type_or_path is not None:
        # One positional: path.json (or a bare type without path → error)
        token = args.type_or_path
        as_path = Path(token)
        if token in SUPPORTED_TYPES and not as_path.is_file():
            print(
                f"[{APP}] path required when type is given (got type {token!r} only)",
                file=sys.stderr,
            )
            return 2
        file_path = as_path.resolve()
    # else: no-arg empty start

    if args.type_override is not None and args.type_override not in SUPPORTED_TYPES:
        print(
            f"[{APP}] unknown --type {args.type_override!r}; known: {known}",
            file=sys.stderr,
        )
        return 2

    _archify_path, archify_msg = detect_archify(args.archify)
    set_preview_html(None)
    print(f"[{APP}] archify: {archify_msg}", flush=True)

    if file_path is not None:
        if not file_path.is_file():
            print(f"[{APP}] file not found: {file_path}", file=sys.stderr)
            return 2
        doc, trailing = load_doc(file_path)
        if args.type_override is not None:
            dtype = SUPPORTED_TYPES[args.type_override]
        elif dtype is None:
            inferred = doc.get("diagram_type")
            if not isinstance(inferred, str) or not inferred.strip():
                print(
                    f"[{APP}] missing diagram_type in {file_path}",
                    file=sys.stderr,
                )
                return 2
            if inferred not in SUPPORTED_TYPES:
                print(
                    f"[{APP}] unsupported diagram_type {inferred!r}; known: {known}",
                    file=sys.stderr,
                )
                return 2
            dtype = SUPPORTED_TYPES[inferred]
        _file_path = file_path
        _diagram_type = dtype
        _doc = doc
        _had_trailing_newline = trailing

        if _archify_path:
            html, note = deliver_preview(_archify_path, _diagram_type, file_path)
            if html is not None:
                set_preview_html(html)
                print(f"[{APP}] initial preview rendered", flush=True)
            else:
                print(
                    f"[{APP}] initial preview unavailable: {note or 'deliver failed'}",
                    flush=True,
                )
        print(
            f"[{APP}] http://127.0.0.1:{args.port}/  editing {file_path}",
            flush=True,
        )
    else:
        _file_path = None
        if args.type_override is not None:
            _diagram_type = SUPPORTED_TYPES[args.type_override]
        else:
            _diagram_type = "workflow"
        _doc = {}
        _had_trailing_newline = True
        print(
            f"[{APP}] http://127.0.0.1:{args.port}/  no file open (use Open / Browse)",
            flush=True,
        )

    url = f"http://127.0.0.1:{args.port}/"
    _auto_exit = not args.keep_alive
    if args.keep_alive:
        print(f"[{APP}] auto-exit off (--keep-alive)", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    _httpd = server
    threading.Thread(target=_presence_watchdog, name="presence-watchdog", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{APP}] stopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
