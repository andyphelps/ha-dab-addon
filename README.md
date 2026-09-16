# ha-dab-addon

A Home Assistant Add-on that runs `welle-cli` against a **remote** RTL-SDR
dongle (via `rtl_tcp`) and exposes a small REST API for station listing and
tuning. Pairs with the `dab_radio` custom component in `../ha-dab-integration/`.

## Topology

```
 macOS host (Sequoia)                 HAOS VM
 ┌─────────────────────┐             ┌────────────────────────────┐
 │ RTL-SDR dongle       │  rtl_tcp    │ dab_radio Add-on            │
 │  -> rtl_tcp -a 0.0.0.0 -p 1234 ───>│  welle-cli -F rtl_tcp,...   │
 │     (just I/Q samples,             │  controller.py (port 9000)  │
 │      no DAB decoding here)         │   -> :8080 stream URLs      │
 └─────────────────────┘             └────────────────────────────┘
                                                    ^
                                                    │ REST (port 9000)
                                       ┌────────────────────────────┐
                                       │ dab_radio custom component  │
                                       │  media_player.dab_radio     │
                                       └────────────────────────────┘
```

welle-cli never touches USB in this setup -- `rtl_tcp.cpp` is an
unconditional input backend in welle.io, not gated behind the `RTLSDR`
(local libusb) build option, so the container only ever makes an outbound
TCP connection. No `devices:`/`privileged:` needed in `config.yaml`.

## Before building the container: verify the network path

DAB needs a continuous ~2.048 MS/s I/Q feed over `rtl_tcp` -- **about 33
Mbit/s, sustained, with no gaps**, across whatever network sits between the
Mac and the HAOS VM (a VM virtual NIC is usually fine, but a flaky Wi-Fi hop
in between would not be). If it can't keep up, welle-cli will simply never
hold sync -- no crash, no clear error, just an empty service list forever.
This is worth testing in two minutes *before* building the Docker image, no
container or VM required:

```bash
# On the Mac hosting the dongle:
brew install librtlsdr    # if not already installed -- ships rtl_tcp
rtl_tcp -a 0.0.0.0 -p 1234

# From any machine that can already build/run welle-cli (e.g. this repo's
# ../welle.io-official/build/welle-cli):
welle-cli -F rtl_tcp,<mac-ip>:1234 -c 10B -w 8080
curl localhost:8080/mux.json | python3 -m json.tool   # look for services: [...]
```

If `services` populates within ~10-15s and stays populated, the container
build below is just packaging a known-good pipeline. If it's marginal or
flaky, skip the Add-on entirely and run `controller.py` bare-metal right
next to the dongle instead (it reads config from environment variables when
`/data/options.json` doesn't exist) -- the `dab_radio` custom component
doesn't care where the REST API is hosted, only that it's reachable.

## Building / installing the Add-on

Home Assistant Add-ons are installed from a git repository added under
*Settings -> Add-ons -> Add-on Store -> ⋮ -> Repositories*. Push this
`ha-dab-addon/` directory to a git repo (or a subdirectory of one) and add
its URL there; the Supervisor will find `dab_radio/config.yaml` and offer
it for install. (Alternatively, if you have the Samba or SSH & Terminal
add-on installed, drop this folder under `/addons/local/` on the HAOS host
directly -- HAOS ships neither Samba nor SSH access by default, so one of
those has to be installed from the official add-on store first, the normal
chicken-and-egg for getting *any* files onto HAOS.)

### Add-on configuration

| Option             | Example              | Meaning                                                             |
|---------------------|------------------------|-----------------------------------------------------------------------|
| `rtltcp_host`        | `192.168.1.50`         | IP of the machine running `rtl_tcp` (the Mac)                        |
| `rtltcp_port`         | `1234`                 | Port `rtl_tcp` is listening on                                       |
| `start_channel`       | `10B`                   | Channel to tune at startup                                            |
| `channels`             | `10B,11A,11D,12B`       | Channels the controller scans, in order                               |
| `public_base_url`     | `http://192.168.1.60:8080` | **Required.** The add-on's own LAN-reachable address. Station stream URLs are built from this, and they're handed to real playback devices (Chromecast, Sonos, the frontend) -- those fetch the URL themselves, on the LAN, not through Home Assistant. Whatever the HAOS VM's LAN-visible IP and the add-on's published port 8080 resolve to, from your other devices' point of view. |
| `gain`                | `22`                    | RTL-SDR tuner gain, passed straight through to welle-cli's `-g`. Defaults to `22` -- **auto-gain (`-1`) never held sync at all** on the dongle this was tested with (FC0013 tuner; `ofdm-processor: SyncOnPhase failed`, repeating forever), `22` is the manual value confirmed working after repositioning the antenna. If you swap dongles/antenna/location, re-verify with a manual `welle-cli -F rtl_tcp,<host>:<port> -c <channel> -g <value> -w 8080` run and watch `/mux.json` rather than trusting this default blindly. |

Note the default output codec is **MP3**, not FLAC -- there's no PCM
decode step in this design (unlike the Pi/FIFO version), so the codec just
has to be something Chromecast/Sonos/browsers play directly over HTTP, and
MP3 is the safer bet there. This also means the container doesn't need
`libflac++-dev` or `-DFLAC=ON` at all.

## API

- `GET /health` -- `{"ok": true, "welle_running": true}`
- `POST /scan` -- kicks off a scan of all configured channels (~30-90s total)
- `GET /stations` -- cached results, grouped by channel
- `GET /status` -- current channel/station, whether welle-cli is alive
- `POST /tune` `{"station": "<label substring, or sid like 0xf00d>"}` --
  retunes if needed, **and blocks until it has confirmed the station is
  actually streaming** (opening welle-cli's `/stream/<sid>` is what kicks
  on-demand decoding off in the first place, and there's a window right
  after a retune where welle-cli's programme-handler map hasn't caught up
  yet -- a naive 200-immediately response would hand a cast device a URL
  that 503s), before returning the station's `stream_url`.
