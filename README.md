# csdr_server

This is a minimal network RTL-SDR server, written in Python, using `pyrtlsdr` as
the IQ source and `csdr` for per-client DSP stages. It's not the fastest thing in the world, but it works well enough for realtime streamming.

## Install

Python dependencies are installed with `pip`. The external DSP dependency,
`csdr`, must already be installed on the system and available in `PATH`.

Install from a local checkout:

```bash
python3 -m pip install .
```

After installation, these CLI tools are available:

- `csdr_server`
- `csdr_server_client`

Installed Python dependencies:

- `pyrtlsdr`
- `pyrtlsdrlib`

External dependency:

- [csdr](https://github.com/jketterl/csdr)

*Important*! This project will not work with the original `csdr` project developed by András Retzler. It requires the fork made by Jakob Ketterl, which is much more modern and has cleanner syntax. At some point I may experiment with using `numpy` in place of `csdr`, but for now, `csdr` is a dependency.

## Features

- Support for multiple clients
- Per-client frequency shifting and decimation
In other words, clients can request a specific sample rate and frequency within the RTL SDR's sampled bandwidth.
- Apply server-side changes to RTL SDR gain, frequency, sample rate, etc, without restarting the server!

## Configuration

Copy `config.example.json` to
`config.json` or something similar, and adjust the parameters as needed.

`center_frequency` is required. Clients can tune within the sampled bandwidth around this center frequency.

The configuration is loaded when the server starts. The configuration can be reloaded in place by sending `SIGHUP` to the server process.

Configuration limits:

- `rtl_sample_rate` must be between `225001` and `300000` S/s, or between
  `900001` and `3200000` S/s (yes I know the RTL SDR sample rate limitations are weird)
- `rtl_gain` must be between `1.0` and `49.6` dB when set.
- `transition_bandwidth` must be between `0.005` and `0.05`
This controls the alias filter when decimating to the requested bandwidth. If you're planning to use the server primarily for narrowband FM, set it to 0.005. If you're using something like broadcast FM, set it to 0.05.

## Run

```bash
csdr_server --config config.json
```

Reload the live settings after editing `config.json`:

```bash
kill -HUP <server-pid>
```

Find the pid by running:

```bash
ps -e | grep 'csdr_server'
```

*Note*: live reload applies only these settings:

- `center_frequency`
- `rtl_sample_rate`
- `rtl_gain`
- `transition_bandwidth`

`center_frequency` will retune the hardware frequency of the SDR, while clients keep their requested RF
  frequencies. If a requested live `center_frequency` or `rtl_sample_rate` would push any
  connected client out of band, or make its requested sample rate impossible to
  decimate cleanly, the server keeps the old center/rate and logs that a server
  restart is required forthe change to take effect. All other config changes, such as the configured RTL SDR device, are ignored until the server is restarted.

## Client

Use `csdr_server_client` to connect to a running server and print IQ data to stdout:

```bash
csdr_server_client -a 127.0.0.1 -p 7355 -f 162.475M -s 16K > iq.cf32
```

The above example will output 32 bit float IQ at a sample rate of 16000 S/s and a frequency of 162.475 MHz (yes if you haven't figured it out by now I am obsessed with NOAA Weather Radio). It then redirects stdout to a file called `iq.cf32`. A better use might be to pipe the IQ data to another program, like `csdr` if you have it installed on the client machine.

`-f` and `-s` accept plain integers or `K`, `M`, and `G` suffixes, `K`, so entering a frequency of `162.475M` gets translated to `162475000` and entering a sample rate of `16K` gets translated to `16000`.

## IQ format

The server sends interleaved 32-bit floating point little-endian IQ samples to clients. The layout would look something like this:
`I`, `Q`

That means the binary stream is:

```text
I0(float32), Q0(float32), I1(float32), Q1(float32), ...
```
but you should already know that, shouldn't you?

## Constraints

- Requested `sample_rate` must be less than or equal to `rtl_sample_rate`.
- Decimation requires an integer ratio
For example, if the RTL sample rate is `240 KSps`, and you requested a sample rate of `16 KSps`, `240000/16000 = 15`. This is an integer ratio, which means it can be decimated to the requested rate. However, if your sample rate is `300 KS/s`, and you requested a sample rate of `16Ks/s`, `300000/16000 = 18.75`. The bandwidth cannot be decimated cleanly, therefore the server will return an error.

 - If a client cannot keep up and its queue fills, that client is disconnected
  instead of letting memory usage grow without bound.
- Shared stream stages and
  client outputs buffer multiple chunks and wait briefly before a lagging branch
  is dropped.

## Exit Codes

- Client connection failure returns `255`.
- Server-side `out of band` rejection returns `1`.
- Server-side `bad sample rate` rejection returns `2`.
- Other request or handshake errors return `3`.

## Device Selection

The device is declared in the json configuration file. There are two parameters for device selection. The first parameter is `rtl_serial`, which allows you to use the device serial number. The second parameter is `rtl_device_index`. Dont' useit unless you have tohowever, because if you have multiple SDR dongles connected the device index number may change! If `rtl_serial` is not set, the server uses `rtl_device_index` directly. If neither are set, startup
  fails with a configuration error.

If multiple USB devices or multiple `librtlsdr` devices share the configured
  serial, startup fails. In that case, you can either set the `device_serial` parameter to `null` and specify `rtl_device_index`, or assign unique serial numbers to each device with
  `rtl_eeprom`. It is always best practice to set unique serial numbers on your SDR's. By default, most RTL SDR dongles have a preconfigured serial number of `00000001`. This means if you have multiple SDR dongles plugged in, it is very difficult for software to distinguish between them. To set the serial number of a device, use the `rtl_eprom` utility, like this:

```bash
rtl_eprom -d 0 -s "19264217"
```

`-d` is the index number of the device, and `-s` is the flag for changing the serial number. After typing the `-s` flag, the new serial number can be entered. It doesn't have to be anything meaningful, just type 8 random numbers on your keyboard and call it a day!

## Resilience

If the RTL SDR becomes disconnected or stalled, the server will automatically attempt to start reading from it again when it is back. Device serial detection is repeated on each recovery attempt, so a replugged
  dongle can come back on a different device index. That is, assuming you actually bothered to specify the serial number in the config, which I would highly recommend doing.

*Note*: If the SDR becomes locked, you'll need to either reset the port, or manually get up off the couch, go over to the server box, yoink the dongle and plug it back in. I hope that isn't too difficult, but keep in mind that these are cheep Chinese SDR dongles pretending to be fancy radios.

## Client Protocol

If you just want to use the server and client programs, you can stop reading here. If you want to develop your own client program, however, keep reading.

The server protocol is line-delimited JSON for the request and handshake, then
raw binary IQ data for the stream.

Transport:

- TCP
- One request per connection
- UTF-8 JSON, terminated by `\n`
- After a successful handshake, the stream payload is raw binary, not JSON

Request:

Each client opens a TCP connection and sends a single JSON line:

```json
{"frequency": 100100000, "sample_rate": 240000}
```

`bandwidth` may be used as an alias for `sample_rate`.

Request fields:

- `frequency`
  - required
  - integer
  - requested tuned RF frequency in Hz
- `sample_rate`
  - required unless `bandwidth` is used
  - integer
  - requested output IQ sample rate in S/s
- `bandwidth`
  - optional alias for `sample_rate`
  - integer

Valid example:

```json
{"frequency": 162475000, "sample_rate": 16000}
```

Handshake:

After the request line, the server sends a one-line JSON handshake:

```json
{"status": "ok"}
```

or:

```json
{"status": "error", "code": 1, "error": "requested frequency is out of band for the current RTL capture window"}
```

Handshake fields:

- `status`
  - `"ok"` or `"error"`
- `code`
  - present on errors
  - numeric error code
- `error`
  - present on errors
  - human-readable message

Error codes:

- `1`: out of band
- `2`: bad sample rate
- `3`: malformed request or other request/handshake error

Stream payload:

Only after an `ok` handshake does the server stream raw `complex float32` IQ
samples back to the client.


I think that's everything, have fun!

