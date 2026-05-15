# csdr_server

Minimal network RTL-SDR server in Python, using `pyrtlsdr` over `librtlsdr` as
the IQ source and `csdr` for per-client DSP stages.

## Scope

This implementation is intentionally narrow:

- One shared RTL-SDR capture path provides unsigned 8-bit IQ samples.
- `csdr convert -i char -o float` is shared instead of run per client.
- If multiple clients request the same frequency, one shared `csdr shift`
  process is reused for that frequency and controlled through a FIFO.
- If multiple clients request the same frequency and sample rate, one shared
  `csdr firdecimate` stage is reused for that `(frequency, sample_rate)` pair.
- The only CSDR stages used are:
  - `convert -i char -o float`
  - `shift`
  - `firdecimate`
- Output sent to clients is raw `complex float32` IQ data.

The server is built around chunked reads from `rtl_sdr` and subprocess-based
CSDR stages so Python is not doing the sample conversion or decimation itself.
This matches the command naming used by `jketterl/csdr`, where RTL-SDR unsigned
8-bit IQ is referred to as `char` for `csdr convert`.

## Requirements

- Python 3.10+
- `pyrtlsdr` importable in Python
- `librtlsdr` available on the system or via `pyrtlsdrlib`
- `csdr` available in `PATH`

## Configuration

Copy [config.example.json](/home/max/git/csdr_server/config.example.json) to
`config.json` and adjust it.

`center_frequency` is required even though it was not in the original minimal
list, because client frequency shifting only makes sense relative to a shared
capture center frequency.

Configuration is loaded once when the server starts and remains in memory until
the process exits.

Configuration limits:

- `rtl_sample_rate` must be between `225001` and `300000` S/s, or between
  `900001` and `3200000` S/s.
- `rtl_gain` must be between `1.0` and `49.6` dB when set.
- `transition_bandwidth` must be between `0.005` and `0.05`.

## Run

```bash
python3 csdr_server.py --config config.json
```

## Client

Use [csdr_server_client.py](/home/max/git/csdr_server/csdr_server_client.py:1) to
request a stream and write the returned IQ data to stdout:

```bash
python3 csdr_server_client.py -a 127.0.0.1 -p 7355 -f 162.475M -s 16K > iq.cf32
```

`-f` and `-s` accept plain integers or `K`, `M`, and `G` suffixes, so
`162.475M` becomes `162475000` and `16K` becomes `16000`.

## Client Protocol

Each client opens a TCP connection and sends a single JSON line:

```json
{"frequency": 100100000, "sample_rate": 240000}
```

`bandwidth` may be used as an alias for `sample_rate`.

After that line, the server sends a one-line JSON handshake:

```json
{"status": "ok"}
```

or:

```json
{"status": "error", "code": 1, "error": "requested frequency is out of band for the current RTL capture window"}
```

Only after an `ok` handshake does the server stream raw `complex float32` IQ
samples back to the client.

The shift value passed to `csdr shift` is computed as:

```text
(center_frequency - requested_frequency) / rtl_sample_rate
```

For each shared shift stage, the server creates a private FIFO under
`/run/user/<uid>` and starts `csdr shift --fifo <path>`. The initial shift rate
is written once by Python, and the FIFO is kept open for the life of the shift
process so CSDR does not receive EOF and exit unexpectedly.

## Constraints

- Requested `sample_rate` must be less than or equal to `rtl_sample_rate`.
- Decimation currently requires an integer ratio:
  - `rtl_sample_rate % sample_rate == 0`
- Requested frequency must produce a `csdr shift` value between `-0.5` and
  `0.5`, inclusive.
- If a client cannot keep up and its queue fills, that client is disconnected
  instead of letting memory usage grow without bound.
- Queueing is bounded but less aggressive than before: shared stream stages and
  client outputs buffer multiple chunks and wait briefly before a lagging branch
  is dropped.

## Exit Codes

- Client connection failure returns `255`.
- Server-side `out of band` rejection returns `1`.
- Server-side `bad sample rate` rejection returns `2`.
- Other request or handshake errors return `3`.

## Device Selection

- If `rtl_serial` is not set, the server uses `rtl_device_index` directly.
- If `rtl_serial` is not set and `rtl_device_index` does not exist, startup
  fails with a configuration error.
- If `rtl_serial` is set, the server first probes `/sys/bus/usb/devices` for
  RTL-SDR-class USB devices with VID:PID `0bda:2832` or `0bda:2838`.
- Once exactly one USB device with the configured serial exists, the server
  asks `librtlsdr` for the current device list and resolves the matching index.
- If `rtl_serial` is set and no matching USB device exists yet, the server keeps
  waiting instead of falling back to `rtl_device_index`.
- If multiple USB devices or multiple `librtlsdr` devices share the configured
  serial, startup fails. In that case, set `rtl_serial` to `null` to use
  `rtl_device_index`, or assign unique serial numbers to the devices with
  `rtl_eeprom`.

## Resilience

- If the `pyrtlsdr` capture path stops unexpectedly, the server goes back to USB
  probing and device-index discovery before starting it again.
- If the capture path produces no data for `rtl_read_timeout_seconds`, the
  server treats that as a hung or disconnected device and also goes back to USB
  probing and device-index discovery.
- Device serial detection is repeated on each recovery attempt, so a replugged
  dongle can come back on a different device index.
- `rtl_sdr` and per-client `csdr` processes are started in separate sessions so
  terminal `Ctrl+C` is handled by the Python server, which then shuts children
  down explicitly.

## Signals

- The server and client handle `SIGINT` and `SIGTERM` gracefully.
- `SIGKILL` cannot be caught or handled by Python or by child processes; that is
  an operating-system limitation.
