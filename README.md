# csdr_server

`csdr_server` is a network RTL-SDR server written in Python. It reads IQ data
from an RTL-SDR dongle with `pyrtlsdr`, uses `csdr` for shifting and
decimation, and serves IQ or audio streams to one or more clients over TCP.

## What Users Will Care About

- One RTL-SDR dongle can serve multiple clients at once.
- Clients can tune anywhere inside the currently sampled RF window.
- Clients can request decimated IQ at nearly any sample rate up to the RTL sample rate.
- Clients can also request demodulated audio instead of raw IQ.
- Two output formats are supported:
  - `f32`
  - `s16`
- Repeated requests are shared where possible to save CPU:
  - same frequency: shared shift stage
  - same frequency and sample rate: shared decimation stage
  - same frequency, sample rate, and format: shared final output stage
- Several settings can be changed live with `SIGHUP` instead of restarting the server.

## Install

Install the Python package from a local checkout:

```bash
python3 -m pip install .
```

This installs two commands:

- `csdr_server`
- `csdr_server_client`

Python dependencies are installed automatically:

- `sounddevice`

On Linux, server dependencies are also installed automatically:

- `pyrtlsdr`
- `pyrtlsdrlib`

On Windows and macOS, those Linux-only server dependencies are not installed,
so `csdr_server_client` can still be installed without pulling in RTL-SDR
server components. If `csdr_server` is launched on a non-Linux platform, it
will exit immediately with a clear runtime error.

`csdr_server_client` uses `sounddevice` for local audio playback. That means
PortAudio support must exist on the system and the PortAudio build must have a
usable backend for the platform.

### Install for system-wide use

If you want `csdr_server` to run like a normal system service, the safest
approach is to install it into a Python virtual environment , as `pip` will complain if you try to install the package as root.

Create a place for the program and its virtual environment:

```bash
sudo mkdir -p /opt/csdr_server
sudo python3 -m venv /opt/csdr_server/venv
```

Install the project into that virtual environment from your local checkout:

```bash
sudo /opt/csdr_server/venv/bin/pip install /path/to/csdr_server
```

`/path/to/csdr_server` is the directory where you cloned this repository.

Create a place for the server config and copy the example config there:

```bash
sudo mkdir -p /etc/csdr_server
sudo cp config.example.json5 /etc/csdr_server/config.json5
```

At this point, you should be able to start the server manually with:

```bash
sudo /opt/csdr_server/venv/bin/csdr_server --config /etc/csdr_server/config.json5
```

### Make the commands available to all users

If you want `csdr_server` and `csdr_server_client` to be available from anywhere
on the system, including other users, create symlinks under `/usr/local/bin`:

```bash
sudo ln -sf /opt/csdr_server/venv/bin/csdr_server /usr/local/bin/csdr_server
sudo ln -sf /opt/csdr_server/venv/bin/csdr_server_client /usr/local/bin/csdr_server_client
```

After that, any user can run:

```bash
csdr_server --help
csdr_server_client --help
```

### Running the server with systemd

Create a service file:

```bash
sudo tee /etc/systemd/system/csdr_server.service >/dev/null <<'EOF'
[Unit]
Description=csdr_server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
Group=YOUR_USERNAME
WorkingDirectory=/opt/csdr_server
RuntimeDirectory=csdr_server
RuntimeDirectoryMode=0700
Environment=XDG_RUNTIME_DIR=/run/csdr_server
ExecStart=/opt/csdr_server/venv/bin/csdr_server --config /etc/csdr_server/config.json5
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
Environment=PATH=/opt/csdr_server/venv/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF
```

Replace `YOUR_USERNAME` with the name of the user account that already has
confirmed RTL-SDR access on your system.

Then load the unit and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now csdr_server
```

Useful commands:

```bash
sudo systemctl status csdr_server
sudo systemctl restart csdr_server
sudo systemctl reload csdr_server
journalctl -u csdr_server -f
```

`systemctl reload csdr_server` sends `SIGHUP` to the running server, which
makes it reload the config from disk.

The `RuntimeDirectory=` line tells systemd to create `/run/csdr_server` with
the correct ownership before the service starts. `csdr_server` uses that
directory for FIFOs, which `csdr` relys upon for certain tasks, and to store other temporary runtime files.

### Why the service should usually run as your existing SDR user

Linux distributions do not grant RTL-SDR access the same way, so we can't give instructions on how to create a dedicated user account that will work across all distros, since the user account needs USB access to the RTL-SDR.  Some distributions, such as Fedora, use `rtlsdr`, others, like Debian, use `plugdev`, and some use different `udev` or
ACL rules. Therefore, we recommend using the user account you already have set up for RTL dongles. This is most likely your user account that you use on your system to perform everyday tasks.

If you want a more locked-down setup later, you can create a dedicated service
useruser for csdr_server, however you'll need to give that user whatever RTL-SDR device
access your distribution expects.

### External dependencies

The SDR server requires my fork of [csdr](https://github.com/maxbaykowski/csdr) to be installed. My fork is based on the version of `csdr` that was made by [Jakob Ketterl](https://github.com/jketterl/csdr), however this version contains bugs that affect performance of `csdr_server`. I have also added additional features to my `csdr` fork that `csdr_server` makes use of.

If you enable server side WFM stereo demodulation, you must also install [Stereo Demux](https://github.com/windytan/stereodemux) and have the `demux` binary available on `PATH`.

If you enable WFM RDS support, you must also install [redsea](https://github.com/windytan/redsea) and have the `redsea` binary available on `PATH` as well.

## Quick Start

Copy the example config:

```bash
cp config.example.json5 config.json5
```

Start the server:

```bash
csdr_server --config config.json5
```

Connect a client:

```bash
csdr_server_client -a 127.0.0.1 -p 7355 -f 162.475M -s 16K > iq.f32
```

Request signed 16-bit IQ instead:

```bash
csdr_server_client -a 127.0.0.1 -p 7355 -f 162.475M -s 16K -F s16 > iq.s16
```

Request AM audio instead of IQ:

```bash
csdr_server_client -a 127.0.0.1 -p 7355 -f 1000K -m audio -M am
```

Request WFM stereo:

```bash
csdr_server_client -a 127.0.0.1 -p 7355 -f 101.1M -m audio -M wfm-stereo
```

*Note*: WFM stereo will only be available if `Stereo Demux` is available on the server.
`-f` and `-s` accept plain integers or `K`, `M`, and `G` suffixes.

In IQ mode, `csdr_server_client` writes binary samples to stdout by default.
In audio mode, it plays audio through the default sound device by default. Use
`--stdout` in audio mode if you want raw `s16` audio samples on stdout instead.
You can tune playback smoothing with `-B` / `--audio-prebuffer` and `-L` /
`--audio-latency`, both in seconds.
If the client is running with an interactive stdin, you can type
`frequency <value>` to retune the active stream without reconnecting. In audio
mode, you can also type `demod <mode>` to switch demodulators live. In IQ
mode, `frequency` is the only accepted interactive command. Multiple commands
can be entered on one line by separating them with semicolons, for example:
`frequency 95.7M; demod wfm-stereo; rds start`.

## Configuration

Copy `config.example.json5` and adjust it for your system.

The server configuration now uses JSON5, so comments and trailing commas are
allowed. Plain JSON also remains valid because JSON is a subset of JSON5.

The config is grouped into three sections:

- `rtl`
  - hardware device selection and radio settings
- `audio`
  - audio-mode-specific demodulator settings
- `server`
  - listener and buffering behavior

### Important settings

#### RTL specific settings

- `rtl_serial`
  - preferred way to select a dongle
- `rtl_device_index`
  - fallback if you do not want to use serial numbers
- `center_frequency`
  - the RF center frequency captured by the dongle
- `automatic_tuning`
  - when `true`, the server automatically retunes the SDR based on connected clients
  - when enabled, manual `rtl.center_frequency` changes are ignored
- `rtl_sample_rate`
  - the hardware sample rate
- `automatic_gain_control`
  - `true` enables automatic gain control
  - `false` means `rtl_gain` is used
- `rtl_gain`
  - manual gain in dB when AGC is off
- `ppm_correction`
  - frequency correction in PPM
- `dc_block`
  - enables or disables IQ-level DC blocking
- `rtl.transition_bandwidth`
  - alias filter width used during IQ decimation

#### Audio

- `audio_support`
  - enables or disables audio mode entirely

##### AM

- `enabled`
  - enables or disables AM support

##### LSB

- `enabled`
  - enables or disables LSB support

##### USB

- `enabled`
  - enables or disables USB support

##### NFM

- `enabled`
  - enables or disables NFM support
- `deemphasis_tau`
  - NFM deemphasis time constant in microseconds, or `null` to disable NFM deemphasis
- `lowpass_frequency`
  - optional post-deemphasis NFM audio lowpass frequency in Hz, or `null` to disable the lowpass stage
- `lowpass_curve`
  - NFM audio lowpass filter curve/steepness

##### WFM

- `enabled`
  - enables or disables WFM support
- `stereo_support`
  - enables or disables WFM stereo support
- `rds_support`
  - enables or disables WFM RDS support
- `region`
  - WFM regional setting, either `us` or `europe`
  - affects WFM deemphasis and whether RBDS callsign decoding is enabled for RDS

#### Server

- `listen_host`
  - address to bind the TCP listener
- `listen_port`
  - TCP port for stream connections
  - the control socket uses `server.listen_port + 1`

### Config Limits

- `rtl.rtl_sample_rate` must be between `225001` and `300000` S/s, or between
  `900001` and `3200000` S/s
- `rtl.rtl_gain` must be between `1.0` and `49.6` dB when AGC is off
- `rtl.ppm_correction` must be between `-500` and `500`
- `rtl.dc_block` must be `true` or `false`
- `rtl.transition_bandwidth` must be between `0.005` and `0.5`
- `rtl.automatic_tuning` must be `true` or `false`
- `audio.audio_support` must be `true` or `false`
- `audio.am.enabled`, `audio.lsb.enabled`, `audio.usb.enabled`, `audio.nfm.enabled`, and `audio.wfm.enabled` must be `true` or `false`
- `audio.nfm.deemphasis_tau` must be `null` or between `32` and `530`
- `audio.nfm.lowpass_frequency` must be `null` or between `3000` and `8000`
- `audio.nfm.lowpass_curve` must be between `0.005` and `0.5` when `audio.nfm.lowpass_frequency` is set
- `audio.wfm.stereo_support` must be `true` or `false`
- `audio.wfm.rds_support` must be `true` or `false`
- `audio.wfm.region` must be either `us` or `europe`

## Live Reload

Edit the config file, then reload it:

```bash
kill -HUP <server-pid>
```

Find the server PID with:

```bash
ps -e | grep csdr_server
```

These settings can be changed live:

- `rtl.center_frequency`
- `rtl.rtl_sample_rate`
- `rtl.automatic_gain_control`
- `rtl.rtl_gain`
- `rtl.ppm_correction`
- `rtl.dc_block`
- `rtl.transition_bandwidth`
- `audio.nfm.deemphasis_tau`
- `audio.nfm.lowpass_frequency`
- `audio.nfm.lowpass_curve`

What live reload does:

- `rtl.center_frequency`
  - retunes the hardware while preserving each connected client's requested RF frequency
  - ignored when `rtl.automatic_tuning` is enabled
- `rtl.rtl_sample_rate`
  - is only applied if every connected client still remains valid
- `rtl.automatic_gain_control`
  - switches between tuner AGC and manual gain
- `rtl.rtl_gain`
  - updates manual gain when AGC is off
- `rtl.ppm_correction`
  - updates frequency correction
- `rtl.dc_block`
  - rebuilds the shared full-band IQ path so both IQ and audio clients pick up the new IQ-level DC blocker
- `rtl.transition_bandwidth`
  - rebuilds decimation stages
- `audio.nfm.deemphasis_tau`
  - rebuilds active audio demodulation stages so NFM clients pick up the new deemphasis value
- `audio.nfm.lowpass_frequency`
  - rebuilds active NFM audio stages so clients pick up the new lowpass setting
- `audio.nfm.lowpass_curve`
  - rebuilds active NFM audio stages so clients pick up the new lowpass curve

If `audio.audio_support`, any `audio.<demod>.enabled` setting, or
`audio.wfm.stereo_support`, `audio.wfm.rds_support`, or `audio.wfm.region`
changes, restart the server. Those settings are not applied live.

If `rtl.automatic_tuning` changes, restart the server. That setting is not
applied live.

If a live `rtl.center_frequency` or `rtl.rtl_sample_rate` change would put an existing
client out of band, or make its requested sample rate exceed the new RTL sample
rate, the server keeps the old setting and logs that a restart is required for
that change.

All other config changes still require a restart.

## Output Formats

The server supports two IQ output formats:

- `f32`
  - complex float32
  - little-endian
  - layout: `I0, Q0, I1, Q1, ...`
- `s16`
  - complex signed 16-bit integer
  - little-endian
  - layout: `I0, Q0, I1, Q1, ...`

For IQ resampling, the server will use integer decimate when the requested sample
rate is an integer ratio of the RTL sample rate. If not, it falls back to
fractional decimation.

## Audio Mode

This program has the ability to stream already demodulated audio to clients rather than IQ data. This is useful, for example, if your LAN isn't fast enough to transport IQ, or you're using a VPN to access the server.

Supported audio modes are:

- AM (Amplitude modulation)
- USB (upper sideband modulation)
- LSB (lower sideband modulation)
- NFM (narrowband frequency modulation)
- WFM (wideband frequency modulation)


WFM supports both mono and stereo, though the server needs to be configured for WFM stereo (see above).

AM, SSB, and NFM demodulation modes will send 16 KHZ 16 bit PCM mono samples to the client. WFM uses 32 KHZ 16 bit mono or stereo, depending on whether stereo is being used or not.

### WFM RDS

RDS is only available in WFM mode and is delivered over the control socket, not
the main stream socket.

It must be enabled by the server administrator by setting `audio.wfm.rds_support=true` in the config. If it is enabled, the server uses `redsea` to decode RDS data.

Interactive audio-mode clients can start and stop RDS decoding with:

```text
rds start
rds stop
```

They can also start RDS decoding immediately on connect with:

```bash
csdr_server_client -a 127.0.0.1 -p 7355 -f 95.7M -m audio -M wfm --rds
```

If the server does not support RDS, or if the selected mode is not `wfm` or
`wfm_stereo`, the client prints an error message sent by the server, and exits.

## Operational Notes

### Device Selection

If `rtl.rtl_serial` is set, the server prefers that device and waits for it to
appear. This is the recommended setup.

If `rtl.rtl_serial` is not set, the server uses `rtl.rtl_device_index`.

If duplicate serial numbers are detected, startup fails. In that case, either:

- set `rtl.rtl_serial` to `null` and use `rtl.rtl_device_index`
- or assign unique serial numbers with `rtl_eeprom`

Example:

```bash
rtl_eprom -d 0 -s "19264217"
```

### Recovery Behavior

If the dongle disappears, stalls, or comes back on a different index, the
server will try to recover automatically. If you are using serial numbers, it
will re-resolve the device again during recovery.

### Backpressure

If a client cannot keep up, the server disconnects that client instead of
letting memory usage grow without bound.

## Client Exit Codes

- `255`
  - connect failure
- `1`
  - out of band, including requests whose needed passband would extend beyond the RTL capture edges
- `2`
  - bad sample rate
- `3`
  - malformed request or handshake error

## Protocol Reference

This section is only needed if you want to write your own client. If you just want to use the server, you can stop here. Otherwise, keep reading.

### Transport

- TCP
- one stream connection plus one control connection per request
- stream socket on `server.listen_port`
- control socket on `server.listen_port + 1`
- client sends a `stream_token` line on the stream socket
- client sends one UTF-8 JSON request line on the control socket
- server sends one UTF-8 JSON handshake line on the control socket
- the control socket stays open after a successful handshake for optional live commands
- raw binary stream is sent on the stream socket after a successful control handshake

### Request

A client sends one JSON line:

```json
{"stream_token": "abc123", "frequency": 162475000, "sample_rate": 16000, "format": "s16"}
```

Request fields:

- `stream_token`
  - required
  - opaque string used to pair the control request with the already-open stream socket
- `frequency`
  - required
  - integer Hz
- `mode`
  - optional
  - `iq` or `audio`
  - defaults to `iq`
- `sample_rate`
  - required in `iq` mode unless `bandwidth` is used
  - integer S/s
- `bandwidth`
  - optional alias for `sample_rate` in `iq` mode
- `format`
  - optional in `iq` mode
  - `f32` or `s16`
  - defaults to `f32`
- `modulation`
  - required in `audio` mode
  - `am`, `usb`, `lsb`, `nfm`, `wfm`, or `wfm_stereo`

IQ request example:

```json
{"stream_token": "abc123", "frequency": 162475000, "mode": "iq", "sample_rate": 16000, "format": "s16"}
```

Audio request example:

```json
{"stream_token": "abc123", "frequency": 1000000, "mode": "audio", "modulation": "am"}
```

```json
{"stream_token": "abc123", "frequency": 7200000, "mode": "audio", "modulation": "usb"}
```

```json
{"stream_token": "abc123", "frequency": 162550000, "mode": "audio", "modulation": "nfm"}
```

```json
{"stream_token": "abc123", "frequency": 101100000, "mode": "audio", "modulation": "wfm"}
```

```json
{"stream_token": "abc123", "frequency": 101100000, "mode": "audio", "modulation": "wfm_stereo"}
```

### Handshake

IQ success:

```json
{"status": "ok", "mode": "iq", "format": "s16"}
```

Audio success:

```json
{"status": "ok", "mode": "audio", "format": "s16", "modulation": "am", "sample_rate": 16000, "channels": 1}
```

Error:

```json
{"status": "error", "code": 1, "error": "requested frequency is out of band for the current RTL capture window"}
```

Handshake fields:

- `status`
  - `"ok"` or `"error"`
- `format`
  - present on success
- `modulation`
  - present on audio success
- `sample_rate`
  - present on audio success
- `channels`
  - present on audio success
- `code`
  - present on error
- `error`
  - present on error

### Live Control Commands

After a successful handshake, a client may continue sending one JSON command
per line on the control socket.

Retune command:

```json
{"command": "retune", "frequency": 162550000}
```

Retune success:

```json
{"status": "ok", "command": "retune", "frequency": 162550000}
```

Retune errors use the same `status=error`, `code`, and `error` fields as the
initial handshake.

Audio clients may also switch demodulators live:

```json
{"command": "demod", "modulation": "wfm_stereo"}
```

Example success response:

```json
{"status": "ok", "command": "demod", "frequency": 95700000, "mode": "audio", "format": "s16", "modulation": "wfm_stereo", "sample_rate": 32000, "channels": 2}
```

WFM audio clients may also toggle RDS subscriptions live:

```json
{"command": "rds", "action": "start"}
```

```json
{"command": "rds", "action": "stop"}
```

Example success response:

```json
{"status": "ok", "command": "rds", "frequency": 95700000, "mode": "audio", "format": "s16", "modulation": "wfm", "sample_rate": 32000, "channels": 1, "rds_active": true}
```

RDS event example:

```json
{"event": "rds", "frequency": 95700000, "fields": {"callsign": "WLHT", "program_service": "FEATHER", "radiotext": "Billie Eilish - Birds Of A Feather"}}
```

### Stream Payload

After an `ok` handshake, the server sends either:

- raw IQ samples for `iq` mode
- demodulated audio samples for `audio` mode

### Request Rules

- requested frequency must stay within the current sampled RF window
- in `iq` mode, requested sample rate must be less than or equal to `rtl.rtl_sample_rate`
- in `iq` mode, decimation must be an integer ratio
- in `audio` mode, the server must be able to decimate cleanly to `16000` S/s

Example:

- valid: `2400000 / 16000 = 150`
- invalid: `300000 / 16000 = 18.75`
