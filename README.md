# csdr_server

Minimal network RTL-SDR server in Python, using `rtl_sdr` as the IQ source and
`csdr` for per-client DSP stages.

## Scope

This implementation is intentionally narrow:

- One shared RTL-SDR capture process provides unsigned 8-bit IQ samples.
- Each client gets its own `csdr` pipeline.
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
- `rtl_sdr` available in `PATH`
- `csdr` available in `PATH`

## Configuration

Copy [config.example.json](/home/max/git/csdr_server/config.example.json) to
`config.json` and adjust it.

`center_frequency` is required even though it was not in the original minimal
list, because client frequency shifting only makes sense relative to a shared
capture center frequency.

Configuration is loaded once when the server starts and remains in memory until
the process exits.

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

After that line, the server streams raw `complex float32` IQ samples back to
the client.

The shift value passed to `csdr shift` is computed as:

```text
(center_frequency - requested_frequency) / rtl_sample_rate
```

## Constraints

- Requested `sample_rate` must be less than or equal to `rtl_sample_rate`.
- Decimation currently requires an integer ratio:
  - `rtl_sample_rate % sample_rate == 0`
- Requested frequency and output bandwidth must fit inside the captured RTL
  window around `center_frequency`.
- If a client cannot keep up and its queue fills, that client is disconnected
  instead of letting memory usage grow without bound.

## Device Selection

- If `rtl_serial` is not set, the server uses `rtl_device_index` directly.
- If `rtl_serial` is set, the server probes `rtl_sdr -d 9999 -` and parses the
  device list to find a matching `SN: ...` entry.
- If that serial is found, the resolved device index is used when starting
  `rtl_sdr`.
- If that serial is not found, the server falls back to `rtl_device_index`.

## Resilience

- If `rtl_sdr` exits with a non-zero code or stops producing data, the server
  waits 0.5 seconds and starts it again.
- Device serial detection is repeated on each restart, so a replugged dongle can
  come back on a different device index.
