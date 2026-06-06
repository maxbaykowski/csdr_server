# Protocol Reference

This document is for people writing their own clients. If you are using the
packaged `csdr_server_client`, the README is enough.

## Transport

- TCP is used for both sockets.
- Each client opens one stream socket and one control socket.
- The stream socket connects to `server.listen_port`.
- The control socket connects to `server.listen_port + 1`.
- The client sends a `stream_token` line on the stream socket.
- The client sends one UTF-8 JSON request line on the control socket.
- The server pairs both sockets by `stream_token`.
- The server sends one UTF-8 JSON handshake line on the control socket.
- The binary stream starts after a successful control handshake.
- The control socket stays open for live commands and asynchronous events.

The control socket is line-delimited JSON. Every control message is one JSON
object followed by `\n`.

## Initial Request

The first control message is the client request:

```json
{"stream_token": "abc123", "frequency": 162475000, "mode": "iq", "sample_rate": 16000, "format": "s16"}
```

Request fields:

- `stream_token`
  - required
  - non-empty string
  - must match the token already sent on the stream socket
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
  - must be greater than `0`
  - must be less than or equal to the server RTL sample rate
- `bandwidth`
  - optional alias for `sample_rate` in `iq` mode
- `format`
  - optional in `iq` mode
  - `f32` or `s16`
  - defaults to `f32`
- `modulation`
  - required in `audio` mode
  - `am`, `usb`, `lsb`, `nfm`, `wfm`, or `wfm_stereo`
  - `wfm-stereo` should be normalized to `wfm_stereo` by clients before sending JSON
- `squelch`
  - optional in `audio` mode
  - integer from `0` to `100`
  - `0` disables squelch and is the default
- `audio_codec`
  - optional in `audio` mode
  - `pcm` or `opus`
  - defaults to `pcm`
  - `codec` is accepted as a legacy alias when `audio_codec` is absent
- `opus_bitrate`
  - optional in `audio` mode when `audio_codec` is `opus`
  - integer bits per second
  - defaults to `24000`
  - must be between the server-supported Opus minimum and maximum

In `audio` mode, `sample_rate`, `bandwidth`, and `format` are ignored if sent.
The server returns warnings in the handshake for ignored audio-only fields.
Audio output is always 48 kHz, stereo, signed 16-bit PCM before optional Opus
encoding.

## Request Examples

IQ as complex float32:

```json
{"stream_token": "abc123", "frequency": 162475000, "mode": "iq", "sample_rate": 48000, "format": "f32"}
```

IQ as signed 16-bit:

```json
{"stream_token": "abc123", "frequency": 162475000, "mode": "iq", "sample_rate": 16000, "format": "s16"}
```

AM audio:

```json
{"stream_token": "abc123", "frequency": 1000000, "mode": "audio", "modulation": "am"}
```

USB audio:

```json
{"stream_token": "abc123", "frequency": 7200000, "mode": "audio", "modulation": "usb"}
```

NFM audio with squelch:

```json
{"stream_token": "abc123", "frequency": 162550000, "mode": "audio", "modulation": "nfm", "squelch": 25}
```

WFM mono audio:

```json
{"stream_token": "abc123", "frequency": 101100000, "mode": "audio", "modulation": "wfm"}
```

WFM stereo audio:

```json
{"stream_token": "abc123", "frequency": 101100000, "mode": "audio", "modulation": "wfm_stereo"}
```

WFM audio with Opus transport:

```json
{"stream_token": "abc123", "frequency": 101100000, "mode": "audio", "modulation": "wfm", "audio_codec": "opus", "opus_bitrate": 24000}
```

## Handshake

IQ success:

```json
{"status": "ok", "mode": "iq", "format": "s16"}
```

Audio success with PCM transport:

```json
{"status": "ok", "mode": "audio", "format": "s16", "modulation": "am", "sample_rate": 48000, "channels": 2, "squelch": 0, "audio_codec": "pcm"}
```

Audio success with Opus transport:

```json
{"status": "ok", "mode": "audio", "format": "s16", "modulation": "wfm", "sample_rate": 48000, "channels": 2, "squelch": 0, "audio_codec": "opus", "opus_bitrate": 24000, "opus_frame_ms": 20}
```

Success with warnings:

```json
{"status": "ok", "mode": "audio", "format": "s16", "modulation": "nfm", "sample_rate": 48000, "channels": 2, "squelch": 0, "audio_codec": "pcm", "warnings": ["sample rate is fixed in audio mode and will be ignored"]}
```

Error:

```json
{"status": "error", "code": 1, "error": "requested frequency is out of band for the current RTL capture window"}
```

Handshake fields:

- `status`
  - `ok` or `error`
- `mode`
  - present on success
  - `iq` or `audio`
- `format`
  - present on success
  - `f32` or `s16` for IQ
  - always `s16` for audio
- `modulation`
  - present on audio success
- `sample_rate`
  - present on audio success
  - always `48000`
- `channels`
  - present on audio success
  - always `2`
- `squelch`
  - present on audio success
- `audio_codec`
  - present on audio success
  - clients must treat this as authoritative because the server may fall back from `opus` to `pcm`
- `opus_bitrate`
  - present on audio success when `audio_codec` is `opus`
- `opus_frame_ms`
  - present on audio success when `audio_codec` is `opus`
  - currently `20`
- `warnings`
  - optional array of strings
- `code`
  - present on error
- `error`
  - present on error

## Stream Payload

After an `ok` handshake, the stream socket carries one of these payloads:

- IQ mode with `format=f32`: complex float32 IQ, interleaved I/Q samples.
- IQ mode with `format=s16`: signed 16-bit IQ, interleaved I/Q samples.
- Audio mode with `audio_codec=pcm`: 48 kHz stereo signed 16-bit PCM.
- Audio mode with `audio_codec=opus`: length-framed Opus packets.

Opus framing is intentionally simple. Each Opus packet is prefixed by a 2-byte
unsigned big-endian packet length, followed by that many Opus bytes. Opus
packets are encoded as 20 ms frames from 48 kHz stereo signed 16-bit PCM.

## Sample Rate And Passband Rules

IQ sample rates do not need to be integer divisors of the RTL sample rate. The
server uses integer decimation when the ratio is clean and fractional decimation
when it is not.

Rules:

- IQ `sample_rate` or `bandwidth` must be greater than `0`.
- IQ `sample_rate` or `bandwidth` must be less than or equal to `rtl.rtl_sample_rate`.
- The requested RF passband must fit inside the current RTL capture window.
- For IQ mode, required RF bandwidth is the requested IQ sample rate.
- For AM, USB, LSB, and NFM audio, required RF bandwidth is `16000` S/s.
- For WFM and WFM stereo audio, required RF bandwidth is `240000` S/s.
- If automatic tuning is enabled, the server chooses a center frequency that fits all connected clients.
- If automatic tuning is disabled, the requested passband must fit around the configured center frequency.

Out-of-band checks include the requested bandwidth edges, not only the tuning
offset. For example, a 240 kS/s WFM request needs roughly 120 kHz of room on
both sides of the requested frequency.

## Live Control Commands

After a successful handshake, a client may continue sending JSON commands on
the control socket. Commands and asynchronous events share the same socket, so
clients must distinguish command responses from event messages.

Command responses have `status`. RDS events have `event`.

## Retune

Retune changes the current frequency for the existing client session.

```json
{"command": "retune", "frequency": 162550000}
```

Success:

```json
{"status": "ok", "command": "retune", "frequency": 162550000, "mode": "audio", "rds_active": false, "squelch": 25, "format": "s16", "modulation": "nfm", "sample_rate": 48000, "channels": 2, "audio_codec": "pcm"}
```

Retune errors use the same `status=error`, `code`, and `error` fields as the
initial handshake. Retuning clears any active RDS subscription.

## Demodulator Change

Audio clients may switch demodulators live:

```json
{"command": "demod", "modulation": "wfm_stereo"}
```

Success:

```json
{"status": "ok", "command": "demod", "frequency": 95700000, "mode": "audio", "rds_active": false, "squelch": 0, "format": "s16", "modulation": "wfm_stereo", "sample_rate": 48000, "channels": 2, "audio_codec": "pcm"}
```

This command is only valid in audio mode. Switching demodulators clears any
active RDS subscription.

## Squelch

Audio clients may adjust squelch live:

```json
{"command": "squelch", "level": 25}
```

Success:

```json
{"status": "ok", "command": "squelch", "frequency": 162550000, "mode": "audio", "rds_active": false, "squelch": 25, "format": "s16", "modulation": "nfm", "sample_rate": 48000, "channels": 2, "audio_codec": "pcm"}
```

This command is only valid in audio mode. `level` must be from `0` to `100`.
`0` disables squelch.

## Opus Bitrate

Opus audio clients may change bitrate live:

```json
{"command": "bitrate", "bitrate": 128000}
```

Success:

```json
{"status": "ok", "command": "bitrate", "frequency": 95700000, "mode": "audio", "rds_active": false, "squelch": 0, "format": "s16", "modulation": "wfm", "sample_rate": 48000, "channels": 2, "audio_codec": "opus", "opus_bitrate": 128000, "opus_frame_ms": 20}
```

This command is only valid in audio mode when the session is using
`audio_codec=opus`. If the session is using PCM, the server rejects it with:

```json
{"status": "error", "code": 3, "error": "bitrate only applies when using the opus codec"}
```

## RDS

WFM audio clients may subscribe to RDS events when server-side WFM RDS support
is enabled:

```json
{"command": "rds", "action": "start"}
```

```json
{"command": "rds", "action": "stop"}
```

Start success:

```json
{"status": "ok", "command": "rds", "frequency": 95700000, "mode": "audio", "rds_active": true, "squelch": 0, "format": "s16", "modulation": "wfm", "sample_rate": 48000, "channels": 2, "audio_codec": "pcm"}
```

Stop success:

```json
{"status": "ok", "command": "rds", "frequency": 95700000, "mode": "audio", "rds_active": false, "squelch": 0, "format": "s16", "modulation": "wfm", "sample_rate": 48000, "channels": 2, "audio_codec": "pcm"}
```

RDS is only valid for `wfm` and `wfm_stereo`. If the server rejects an RDS
request because the current mode is not WFM, the error message is `RDS is only
available in WFM mode`. If a client changes frequency or changes demodulation
mode, RDS is unsubscribed and the client must subscribe again.

RDS events are delivered asynchronously on the control socket. They are not sent
on the stream socket. An RDS event looks like this:

```json
{"event": "rds", "frequency": 95700000, "fields": {"program_service": "FEATHER", "radiotext": "Billie Eilish - Birds Of A Feather"}}
```

The `fields` object contains only fields that changed since the last event for
that RDS decoder. Clients should keep their own display state and update it with
new fields instead of clearing fields that are absent from an event.

When a new subscriber joins an already-running RDS decoder, the server sends the
current RDS snapshot if one exists.

Possible RDS fields:

- `callsign`
- `program_service`
- `radiotext`
- `program_type`
- `artist`
- `title`

The `callsign` field is only available when the server WFM region is configured
for US-style RDS callsign decoding.

## Error Codes

- `1`
  - out of band
- `2`
  - bad sample rate
- `3`
  - malformed request, unsupported option, or control command error

The stock client also uses `255` locally for connection failures.
