from __future__ import annotations

import ctypes


OPUS_SAMPLE_RATE = 48_000
OPUS_CHANNELS = 2
OPUS_FRAME_MS = 20
OPUS_FRAME_SAMPLES = (OPUS_SAMPLE_RATE * OPUS_FRAME_MS) // 1000
OPUS_PCM_BYTES_PER_FRAME = OPUS_FRAME_SAMPLES * OPUS_CHANNELS * 2
OPUS_MAX_PACKET_BYTES = 4_000
OPUS_PACKET_HEADER_BYTES = 2
DEFAULT_OPUS_BITRATE = 24_000
MIN_OPUS_BITRATE = 6_000
MAX_OPUS_BITRATE = 510_000
VALID_AUDIO_CODECS = {"pcm", "opus"}
DEFAULT_AUDIO_CODEC = "pcm"


class OpusCodecError(RuntimeError):
    pass


def validate_audio_codec(codec: str) -> str:
    normalized = str(codec).strip().lower()
    if normalized not in VALID_AUDIO_CODECS:
        raise ValueError(
            f"unsupported audio codec {codec!r}; expected one of {', '.join(sorted(VALID_AUDIO_CODECS))}"
        )
    return normalized


def validate_opus_bitrate(bitrate: int) -> int:
    try:
        value = int(bitrate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Opus bitrate: {bitrate!r}") from exc
    if value < MIN_OPUS_BITRATE or value > MAX_OPUS_BITRATE:
        raise ValueError(
            f"Opus bitrate must be between {MIN_OPUS_BITRATE} and {MAX_OPUS_BITRATE} bps"
        )
    return value


def _load_opus():
    try:
        from pyogg import opus
    except ImportError as exc:
        raise OpusCodecError(
            "Opus audio transport requires the Python package 'PyOgg'"
        ) from exc
    return opus


def ensure_opus_available() -> None:
    _load_opus()


def probe_opus_encoder(bitrate: int = DEFAULT_OPUS_BITRATE) -> None:
    encoder: OpusPacketEncoder | None = None
    try:
        encoder = OpusPacketEncoder(bitrate)
    except OpusCodecError:
        raise
    except Exception as exc:
        raise OpusCodecError(f"failed to initialize Opus encoder: {exc}") from exc
    finally:
        if encoder is not None:
            encoder.close()


def probe_opus_decoder() -> None:
    decoder: OpusPacketDecoder | None = None
    try:
        decoder = OpusPacketDecoder()
    except OpusCodecError:
        raise
    except Exception as exc:
        raise OpusCodecError(f"failed to initialize Opus decoder: {exc}") from exc
    finally:
        if decoder is not None:
            decoder.close()


class OpusPacketEncoder:
    def __init__(self, bitrate: int = DEFAULT_OPUS_BITRATE) -> None:
        self.opus = _load_opus()
        self.bitrate = validate_opus_bitrate(bitrate)
        self.buffer = bytearray()

        error = ctypes.c_int()
        self.encoder = self.opus.opus_encoder_create(
            OPUS_SAMPLE_RATE,
            OPUS_CHANNELS,
            self.opus.OPUS_APPLICATION_AUDIO,
            ctypes.byref(error),
        )
        if error.value != self.opus.OPUS_OK:
            raise OpusCodecError(
                "failed to create Opus encoder: "
                + self.opus.opus_strerror(error.value).decode("utf-8", errors="replace")
            )
        result = self.opus.opus_encoder_ctl(
            self.encoder,
            self.opus.OPUS_SET_BITRATE_REQUEST,
            self.opus.opus_int32(self.bitrate),
        )
        if result != self.opus.OPUS_OK:
            raise OpusCodecError(
                "failed to set Opus bitrate: "
                + self.opus.opus_strerror(result).decode("utf-8", errors="replace")
            )
        self.output = (ctypes.c_ubyte * OPUS_MAX_PACKET_BYTES)()

    def encode(self, pcm: bytes) -> list[bytes]:
        self.buffer.extend(pcm)
        packets: list[bytes] = []
        while len(self.buffer) >= OPUS_PCM_BYTES_PER_FRAME:
            frame = bytes(self.buffer[:OPUS_PCM_BYTES_PER_FRAME])
            del self.buffer[:OPUS_PCM_BYTES_PER_FRAME]
            packet = self._encode_frame(frame)
            packets.append(len(packet).to_bytes(OPUS_PACKET_HEADER_BYTES, "big") + packet)
        return packets

    def _encode_frame(self, frame: bytes) -> bytes:
        pcm_buffer = (self.opus.opus_int16 * (OPUS_FRAME_SAMPLES * OPUS_CHANNELS)).from_buffer_copy(frame)
        result = self.opus.opus_encode(
            self.encoder,
            pcm_buffer,
            OPUS_FRAME_SAMPLES,
            self.output,
            OPUS_MAX_PACKET_BYTES,
        )
        if result < 0:
            raise OpusCodecError(
                "failed to encode Opus packet: "
                + self.opus.opus_strerror(result).decode("utf-8", errors="replace")
            )
        if result >= 2 ** (8 * OPUS_PACKET_HEADER_BYTES):
            raise OpusCodecError(f"Opus packet too large for transport header: {result} bytes")
        return bytes(self.output[:result])

    def close(self) -> None:
        if getattr(self, "encoder", None) is not None:
            self.opus.opus_encoder_destroy(self.encoder)
            self.encoder = None


class OpusPacketDecoder:
    def __init__(self) -> None:
        self.opus = _load_opus()
        self.buffer = bytearray()
        error = ctypes.c_int()
        self.decoder = self.opus.opus_decoder_create(
            OPUS_SAMPLE_RATE,
            OPUS_CHANNELS,
            ctypes.byref(error),
        )
        if error.value != self.opus.OPUS_OK:
            raise OpusCodecError(
                "failed to create Opus decoder: "
                + self.opus.opus_strerror(error.value).decode("utf-8", errors="replace")
            )
        self.pcm = (self.opus.opus_int16 * (OPUS_FRAME_SAMPLES * OPUS_CHANNELS))()

    def decode(self, data: bytes) -> bytes:
        self.buffer.extend(data)
        pcm_chunks: list[bytes] = []
        while len(self.buffer) >= OPUS_PACKET_HEADER_BYTES:
            packet_size = int.from_bytes(self.buffer[:OPUS_PACKET_HEADER_BYTES], "big")
            if packet_size <= 0 or packet_size > OPUS_MAX_PACKET_BYTES:
                raise OpusCodecError(f"invalid Opus packet size: {packet_size}")
            needed = OPUS_PACKET_HEADER_BYTES + packet_size
            if len(self.buffer) < needed:
                break
            packet = bytes(self.buffer[OPUS_PACKET_HEADER_BYTES:needed])
            del self.buffer[:needed]
            pcm_chunks.append(self._decode_packet(packet))
        return b"".join(pcm_chunks)

    def _decode_packet(self, packet: bytes) -> bytes:
        packet_buffer = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        result = self.opus.opus_decode(
            self.decoder,
            packet_buffer,
            self.opus.opus_int32(len(packet)),
            self.pcm,
            OPUS_FRAME_SAMPLES,
            0,
        )
        if result < 0:
            raise OpusCodecError(
                "failed to decode Opus packet: "
                + self.opus.opus_strerror(result).decode("utf-8", errors="replace")
            )
        byte_count = result * OPUS_CHANNELS * ctypes.sizeof(self.opus.opus_int16)
        return memoryview(self.pcm).cast("B")[:byte_count].tobytes()

    def close(self) -> None:
        if getattr(self, "decoder", None) is not None:
            self.opus.opus_decoder_destroy(self.decoder)
            self.decoder = None
