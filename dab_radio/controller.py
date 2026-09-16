#!/usr/bin/env python3
"""
DAB station-list + tune controller for Home Assistant.

Starts welle-cli against a remote rtl_tcp source (the RTL-SDR dongle stays
on whatever machine runs rtl_tcp; this container/script never touches USB),
then scans a configured list of channels one at a time, caching the station
list found on each. Exposes a small REST API that the `dab_radio` custom
component talks to: GET /stations, POST /scan, GET /status, POST /tune.

Unlike the FIFO-based Pi controller, there is no PCM bridge here -- Home
Assistant hands stream URLs to real playback devices (Chromecast, Sonos,
the frontend), so we just point them at welle-cli's own /stream/<sid>
endpoint. That URL has to be reachable from those *player* devices on the
LAN, not just from Home Assistant itself, which is why PUBLIC_BASE_URL is
a required, explicit setting rather than something inferred from the
container's own (internal, Docker-network) view of itself.

Config is read from /data/options.json if present (the standard Home
Assistant Supervisor add-on config file), otherwise from environment
variables -- so this same script also runs standalone, next to the dongle,
without a container, if the rtl_tcp-over-network path ever proves too thin
for DAB's ~33 Mbit/s continuous I/Q rate.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import requests
from flask import Flask, jsonify, request

log = logging.getLogger("dab-controller")

OPTIONS_FILE = Path("/data/options.json")


def _load_options() -> dict:
    if OPTIONS_FILE.exists():
        try:
            return json.loads(OPTIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not parse %s: %s", OPTIONS_FILE, e)
    return {}


_OPTS = _load_options()


def _opt(key: str, env_key: str, default: str = "") -> str:
    if key in _OPTS and str(_OPTS[key]).strip():
        return str(_OPTS[key]).strip()
    return os.environ.get(env_key, default).strip()


@dataclass
class Config:
    rtltcp_host: str = field(default_factory=lambda: _opt("rtltcp_host", "RTLTCP_HOST"))
    rtltcp_port: int = field(default_factory=lambda: int(_opt("rtltcp_port", "RTLTCP_PORT", "1234")))
    start_channel: str = field(default_factory=lambda: _opt("start_channel", "START_CHANNEL", "12B"))
    # RTL-SDR gain to hand welle-cli via -g. "-1" means auto-gain -- which,
    # at least for the FC0013-tuner dongle this was built against, never
    # actually held sync. A manual value (found empirically per antenna/
    # location) was required. Don't assume auto-gain works; make this
    # explicitly configurable rather than hardcoding either choice.
    gain: str = field(default_factory=lambda: _opt("gain", "GAIN", "22"))
    channels: List[str] = field(
        default_factory=lambda: [
            c.strip() for c in _opt("channels", "DAB_CHANNELS", "10B,11A,11D,12B").split(",") if c.strip()
        ]
    )
    # LAN-reachable base URL for welle-cli, e.g. http://192.168.1.50:8080 --
    # this is what gets handed to Chromecast/Sonos/the frontend, so it must
    # resolve from THEIR network view, not the container's internal one.
    public_base_url: str = field(default_factory=lambda: _opt("public_base_url", "PUBLIC_BASE_URL"))
    welle_internal_base: str = "http://127.0.0.1:8080"
    controller_port: int = int(_opt("controller_port", "CONTROLLER_PORT", "9000"))
    controller_host: str = "0.0.0.0"
    sync_timeout_s: float = float(_opt("sync_timeout", "DAB_SYNC_TIMEOUT", "20"))
    settle_s: float = float(_opt("settle", "DAB_SETTLE", "3"))
    stream_probe_timeout_s: float = float(_opt("stream_probe_timeout", "DAB_STREAM_PROBE_TIMEOUT", "15"))
    welle_bin: str = os.environ.get("WELLE_CLI_BIN", "welle-cli")
    # Optional: sync scanned stations into a Music Assistant server via its
    # builtin/add_radio API command (no MA provider plugin needed -- MA has
    # no external-provider loading mechanism, so this is the durable path
    # that survives MA updates). Leave ma_url empty to disable syncing.
    ma_url: str = field(default_factory=lambda: _opt("ma_url", "MA_URL"))
    ma_token: str = field(default_factory=lambda: _opt("ma_token", "MA_TOKEN"))


CFG = Config()


class WelleClient:
    def __init__(self, base_url: str, timeout: float = 5):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def get_channel(self) -> str:
        r = requests.get(f"{self.base}/channel", timeout=self.timeout)
        r.raise_for_status()
        return r.text.strip()

    def post_channel(self, channel: str) -> None:
        r = requests.post(f"{self.base}/channel", data=channel, timeout=self.timeout)
        r.raise_for_status()

    def get_mux(self) -> dict:
        r = requests.get(f"{self.base}/mux.json", timeout=self.timeout)
        r.raise_for_status()
        return r.json()


welle = WelleClient(CFG.welle_internal_base)


# ---------------------------------------------------------------------------
# welle-cli process management (against the remote rtl_tcp source)
# ---------------------------------------------------------------------------


class WelleProcess:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            args = [
                self.cfg.welle_bin,
                "-F", f"rtl_tcp,{self.cfg.rtltcp_host}:{self.cfg.rtltcp_port}",
                "-c", self.cfg.start_channel,
                "-g", self.cfg.gain,
                "-w", "8080",
                "-O", "mp3",
            ]
            log.info("Starting welle-cli: %s", " ".join(args))
            self._proc = subprocess.Popen(args)

    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                self._proc = None


welle_process = WelleProcess(CFG)


def welle_watchdog_loop() -> None:
    while True:
        time.sleep(5)
        if not welle_process.running():
            log.warning("welle-cli is not running; (re)starting it")
            welle_process.start()


def wait_for_sync(timeout: float, settle: float) -> dict:
    """Poll /mux.json until the service list looks stable, then wait `settle`
    seconds more so welle-cli's programme-handler map catches up, and return
    the last mux.json seen (possibly still empty if there's no signal)."""
    start = time.time()
    last_n = -1
    stable_polls = 0
    mux: dict = {}
    while time.time() - start < timeout:
        try:
            mux = welle.get_mux()
        except requests.RequestException as e:
            log.debug("mux.json poll failed: %s", e)
            time.sleep(1)
            continue

        services = mux.get("services", [])
        ensemble_label = (mux.get("ensemble", {}).get("label", {}) or {}).get("label", "").strip()
        n = len(services)

        if n > 0 and ensemble_label:
            stable_polls = stable_polls + 1 if n == last_n else 0
            last_n = n
            if stable_polls >= 2:
                time.sleep(settle)
                try:
                    return welle.get_mux()
                except requests.RequestException:
                    return mux
        time.sleep(1.5)

    return mux


def has_audio(service: dict) -> bool:
    return any(c.get("transportmode") == "audio" for c in service.get("components", []))


def probe_stream(sid: str, timeout: float) -> bool:
    """Open a short connection to welle-cli's own stream endpoint and confirm
    it actually starts producing bytes before we report a tune as successful.
    Opening this connection is also what triggers on-demand decoding to
    start (see webradiointerface.cpp's send_stream/registerSender), so this
    isn't just a check -- it's the thing that kicks decoding off."""
    url = f"{CFG.welle_internal_base}/stream/{sid}"
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                return False
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    return True
        return False
    except requests.RequestException as e:
        log.warning("Stream probe for %s failed: %s", sid, e)
        return False


def sync_stations_to_music_assistant(stations: List[dict]) -> None:
    """Push scanned stations into a Music Assistant server via its builtin
    provider's add_radio API command. This is the durable path -- MA has no
    external-provider loading mechanism (equivalent to Home Assistant's
    custom_components), so a dedicated DAB Radio provider would need to be
    merged upstream into music-assistant/server to be usable. add_radio is
    an officially registered API command that survives MA updates.

    MUST be called while welle-cli is tuned to these stations' channel --
    add_radio validates each URL live via ffprobe (to fetch codec/format
    info), which 404s against any station not on the currently-tuned
    channel. Caller is responsible for syncing per-channel, during the scan.

    Idempotent: add_radio dedupes on the station's stream_url (stable as
    long as public_base_url and the station's sid don't change), so calling
    this on every scan just refreshes existing entries rather than piling
    up duplicates.
    """
    if not CFG.ma_url or not stations:
        return

    headers = {"Authorization": f"Bearer {CFG.ma_token}", "Content-Type": "application/json"}
    ok = 0
    for st in stations:
        try:
            resp = requests.post(
                f"{CFG.ma_url}/api",
                headers=headers,
                json={
                    "command": "builtin/add_radio",
                    "args": {"url": st["stream_url"], "name": st["label"]},
                },
                timeout=10,
            )
            if resp.status_code == 200:
                ok += 1
            else:
                log.warning("Music Assistant add_radio for %r failed: %s %s", st["label"], resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            log.warning("Music Assistant add_radio for %r failed: %s", st["label"], e)

    log.info("Synced %d/%d station(s) on channel %s to Music Assistant", ok, len(stations), stations[0]["channel"])


# ---------------------------------------------------------------------------
# Station cache, built by scanning CFG.channels one at a time
# ---------------------------------------------------------------------------

store_lock = threading.Lock()
station_cache: Dict[str, dict] = {}

scan_lock = threading.Lock()

tune_lock = threading.RLock()
current_channel: Optional[str] = None
current_station: Optional[dict] = None


def scan_all() -> None:
    global current_channel

    if not scan_lock.acquire(blocking=False):
        log.info("Scan already in progress, skipping")
        return

    try:
        results: Dict[str, dict] = {}
        for ch in CFG.channels:
            log.info("Scanning channel %s ...", ch)
            try:
                welle.post_channel(ch)
            except requests.RequestException as e:
                log.error("Could not retune to %s: %s", ch, e)
                results[ch] = {"ensemble": None, "scanned_at": time.time(), "stations": [], "error": str(e)}
                continue

            mux = wait_for_sync(CFG.sync_timeout_s, CFG.settle_s)
            ensemble_label = (mux.get("ensemble", {}).get("label", {}) or {}).get("label", "").strip()
            services = [s for s in mux.get("services", []) if has_audio(s)]

            stations = []
            for s in services:
                sid = s.get("sid")
                label = (s.get("label") or {}).get("label", "").strip()
                stations.append(
                    {
                        "sid": sid,
                        "label": label,
                        "shortlabel": (s.get("label") or {}).get("shortlabel", "").strip(),
                        "ptystring": s.get("ptystring"),
                        "channel": ch,
                        "ensemble": ensemble_label,
                        "stream_url": f"{CFG.public_base_url}/stream/{sid}",
                    }
                )

            log.info("Channel %s: ensemble=%r, %d station(s) with audio", ch, ensemble_label, len(stations))
            results[ch] = {"ensemble": ensemble_label, "scanned_at": time.time(), "stations": stations}

            # Sync THIS channel's stations to MA now, while welle-cli is still
            # tuned to it -- MA's add_radio validates each URL live via
            # ffprobe, which 404s for any station not on the currently-tuned
            # channel. Batching this at the end (after moving on to the last
            # channel) would only ever succeed for that last channel.
            sync_stations_to_music_assistant(stations)

        with store_lock:
            station_cache.clear()
            station_cache.update(results)

        if CFG.channels:
            current_channel = CFG.channels[-1]
    finally:
        scan_lock.release()


def find_station(query: str) -> Optional[dict]:
    q = query.strip().lower()
    hex_query = q if q.startswith("0x") else (f"0x{q}" if re.fullmatch(r"[0-9a-f]{1,4}", q) else None)

    with store_lock:
        all_stations = [st for ch in station_cache.values() for st in ch["stations"]]

    if hex_query:
        for st in all_stations:
            if st["sid"].lower() == hex_query:
                return st

    for st in all_stations:
        if st["label"].lower() == q:
            return st

    for st in all_stations:
        if q in st["label"].lower():
            return st

    return None


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.get("/health")
def api_health():
    return jsonify(ok=True, welle_running=welle_process.running())


@app.get("/stations")
def api_stations():
    with store_lock:
        data = {
            ch: {
                "ensemble": v.get("ensemble"),
                "scanned_at": v.get("scanned_at"),
                "stations": v.get("stations", []),
            }
            for ch, v in station_cache.items()
        }
    return jsonify(channels=data)


@app.post("/scan")
def api_scan():
    if scan_lock.locked():
        return jsonify(error="scan already in progress"), 409
    threading.Thread(target=scan_all, daemon=True, name="scan").start()
    return jsonify(status="scan started", channels=CFG.channels)


@app.get("/status")
def api_status():
    return jsonify(
        current_channel=current_channel,
        current_station=current_station,
        welle_running=welle_process.running(),
        channels_configured=CFG.channels,
        scanning=scan_lock.locked(),
        public_base_url=CFG.public_base_url,
    )


@app.post("/tune")
def api_tune():
    global current_channel, current_station

    body = request.get_json(force=True, silent=True) or {}
    query = body.get("station") or request.args.get("station")
    if not query:
        return jsonify(error="Provide 'station' (service id like 0xf00d, or a label/substring)"), 400

    station = find_station(query)
    if not station:
        return jsonify(error=f"No station matching {query!r}. Run POST /scan first, or check /stations."), 404

    with tune_lock:
        if station["channel"] != current_channel:
            log.info("Retuning from %s to %s for %s", current_channel, station["channel"], station["label"])
            try:
                welle.post_channel(station["channel"])
            except requests.RequestException as e:
                return jsonify(error=f"Failed to retune to {station['channel']}: {e}"), 502
            wait_for_sync(CFG.sync_timeout_s, CFG.settle_s)
            current_channel = station["channel"]

        if not probe_stream(station["sid"], CFG.stream_probe_timeout_s):
            return jsonify(error=f"Tuned to {station['channel']} but {station['label']} never started streaming"), 503

        current_station = station

    return jsonify(tuned=station)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not CFG.rtltcp_host:
        log.error("rtltcp_host is not configured -- set it to the IP running rtl_tcp")
        raise SystemExit(1)
    if not CFG.public_base_url:
        log.error(
            "public_base_url is not configured -- set it to this add-on's LAN-reachable "
            "base URL (e.g. http://<haos-ip>:8080), so Chromecast/Sonos/the frontend can "
            "actually fetch the stream, not just Home Assistant itself"
        )
        raise SystemExit(1)

    welle_process.start()
    threading.Thread(target=welle_watchdog_loop, daemon=True, name="welle-watchdog").start()

    log.info("Waiting for welle-cli to come up...")
    time.sleep(3)

    global current_channel
    try:
        current_channel = welle.get_channel()
        log.info("welle-cli is currently tuned to channel %s", current_channel)
    except requests.RequestException as e:
        log.warning("Could not query welle-cli's current channel at startup (%s)", e)

    log.info("Listening on %s:%d", CFG.controller_host, CFG.controller_port)
    app.run(host=CFG.controller_host, port=CFG.controller_port, threaded=True)


if __name__ == "__main__":
    main()
