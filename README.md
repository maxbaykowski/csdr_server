# csdr_server

`csdr_server` is a network RTL-SDR server written in Python. It reads IQ data
from an RTL-SDR dongle with `pyrtlsdr`, uses `csdr` for shifting and
decimation, and serves IQ streams to one or more clients over TCP.

The main goal is to be boring and reliable:

- multiple clients can connect at the same time
- each client can request its own frequency and sample rate within the captured bandwidth
- the server can retune and apply several radio settings live without a restart
- if the dongle disappears and comes back, the server will try to recover

## What Users Will Care About

- One RTL-SDR dongle can serve multiple clients at once.
- Clients can tune anywhere inside the currently sampled RF window.
- Clients can request decimated IQ at the sample rate they actually need.
- Clients can also request demodulated audio instead of raw IQ.
- Two output formats are supported:
  - `f32`
  - `s16`
- Repeated requests are shared where possible:
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

- `pyrtlsdr`
- `pyrtlsdrlib`

You must still install the external DSP dependency yourself:

- [jketterl/csdr](https://github.com/jketterl/csdr)

This project is written against the `jketterl/csdr` fork, not the original
András Retzler version.

## Quick Start

Copy the example config:

```bash
cp config.example.json config.json
```

Start the server:

```bash
csdr_server --config config.json
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
csdr_server_client -a 127.0.0.1 -p 7355 -f 1000K -m audio -M am > audio.s16
```

`-f` and `-s` accept plain integers or `K`, `M`, and `G` suffixes.

## Configuration

Copy `config.example.json` and adjust it for your system.

Important settings:

- `rtl_serial`
  - preferred way to select a dongle
- `rtl_device_index`
  - fallback if you do not want to use serial numbers
- `center_frequency`
  - the RF center frequency captured by the dongle
- `rtl_sample_rate`
  - the hardware sample rate
- `automatic_gain_control`
  - `true` enables automatic gain control
  - `false` means `rtl_gain` is used
- `rtl_gain`
  - manual gain in dB when AGC is off
- `ppm_correction`
  - frequency correction in PPM
- `transition_bandwidth`
  - alias filter width used during decimation

### Config Limits

- `rtl_sample_rate` must be between `225001` and `300000` S/s, or between
  `900001` and `3200000` S/s
- `rtl_gain` must be between `1.0` and `49.6` dB when AGC is off
- `ppm_correction` must be between `-500` and `500`
- `transition_bandwidth` must be between `0.005` and `0.05`

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

- `center_frequency`
- `rtl_sample_rate`
- `automatic_gain_control`
- `rtl_gain`
- `ppm_correction`
- `transition_bandwidth`

What live reload does:

- `center_frequency`
  - retunes the hardware while preserving each connected client's requested RF frequency
- `rtl_sample_rate`
  - is only applied if every connected client still remains valid
- `automatic_gain_control`
  - switches between tuner AGC and manual gain
- `rtl_gain`
  - updates manual gain when AGC is off
- `ppm_correction`
  - updates frequency correction
- `transition_bandwidth`
  - rebuilds decimation stages

If a live `center_frequency` or `rtl_sample_rate` change would put an existing
client out of band, or make its requested sample rate impossible to decimate
cleanly, the server keeps the old setting and logs that a restart is required
for that change.

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

## Audio Mode

Audio mode is separate from IQ mode. Instead of returning IQ data, the server
demodulates the signal and sends audio.

The first supported audio mode is:

- `mode=audio`
- `modulation=am`

AM audio currently uses a fixed internal pipeline:

- shift to the requested frequency
- decimate to `16000` S/s
- transition bandwidth `0.005`
- `amdemod`
- `dcblock`
- `agc -r 0.2`
- convert to `s16`

So AM audio clients always receive 16 kHz signed 16-bit mono audio.

## Operational Notes

### Device Selection

If `rtl_serial` is set, the server prefers that device and waits for it to
appear. This is the recommended setup.

If `rtl_serial` is not set, the server uses `rtl_device_index`.

If duplicate serial numbers are detected, startup fails. In that case, either:

- set `rtl_serial` to `null` and use `rtl_device_index`
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
  - out of band
- `2`
  - bad sample rate
- `3`
  - malformed request or handshake error

## Protocol Reference

This section is only needed if you want to write your own client.

### Transport

- TCP
- one request per connection
- UTF-8 JSON request line terminated by `\n`
- UTF-8 JSON handshake line terminated by `\n`
- raw binary IQ stream after a successful handshake

### Request

A client sends one JSON line:

```json
{"frequency": 162475000, "sample_rate": 16000, "format": "s16"}
```

Request fields:

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
  - currently only `am`

IQ request example:

```json
{"frequency": 162475000, "mode": "iq", "sample_rate": 16000, "format": "s16"}
```

Audio request example:

```json
{"frequency": 1000000, "mode": "audio", "modulation": "am"}
```

### Handshake

IQ success:

```json
{"status": "ok", "mode": "iq", "format": "s16"}
```

Audio success:

```json
{"status": "ok", "mode": "audio", "format": "s16", "modulation": "am", "sample_rate": 16000}
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
- `code`
  - present on error
- `error`
  - present on error

### Stream Payload

After an `ok` handshake, the server sends either:

- raw IQ samples for `iq` mode
- demodulated audio samples for `audio` mode

### Request Rules

- requested frequency must stay within the current sampled RF window
- in `iq` mode, requested sample rate must be less than or equal to `rtl_sample_rate`
- in `iq` mode, decimation must be an integer ratio
- in `audio` mode, the server must be able to decimate cleanly to `16000` S/s

Example:

- valid: `2400000 / 16000 = 150`
- invalid: `300000 / 16000 = 18.75`
