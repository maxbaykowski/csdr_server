from __future__ import annotations

import argparse
import os
import sys


READ_SIZE = 4_096
INT16_SCALE = 32767.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize mono float audio to stereo s16 PCM")
    parser.add_argument("--input-rate", type=int, required=True)
    parser.add_argument("--output-rate", type=int, required=True)
    parser.add_argument("--output-channels", type=int, default=2)
    return parser.parse_args()


def _load_modules():
    try:
        import numpy
        import soxr
    except ModuleNotFoundError as exc:
        print(f"csdr_server audio PCM converter missing dependency: {exc.name}", file=sys.stderr)
        raise SystemExit(1) from exc
    return numpy, soxr


def _float_to_s16_stereo(numpy, samples, output_channels: int) -> bytes:
    clipped = numpy.clip(samples, -1.0, 1.0)
    pcm = (clipped * INT16_SCALE).astype(numpy.int16)
    if output_channels == 1:
        return numpy.ascontiguousarray(pcm).tobytes()
    output = numpy.zeros((pcm.shape[0], output_channels), dtype=numpy.int16)
    output[:, 0] = pcm
    output[:, 1] = pcm
    return numpy.ascontiguousarray(output).tobytes()


def main() -> int:
    args = _parse_args()
    if args.input_rate <= 0 or args.output_rate <= 0:
        print("input and output rates must be positive", file=sys.stderr)
        return 1
    if args.output_channels <= 0:
        print("output channel count must be positive", file=sys.stderr)
        return 1

    numpy, soxr = _load_modules()
    resampler = None
    if args.input_rate != args.output_rate:
        resampler = soxr.ResampleStream(
            args.input_rate,
            args.output_rate,
            1,
            dtype="float32",
            quality="MQ",
        )

    remainder = b""
    while True:
        chunk = sys.stdin.buffer.read(READ_SIZE)
        if not chunk:
            break
        payload = remainder + chunk
        usable_size = len(payload) - (len(payload) % 4)
        if usable_size <= 0:
            remainder = payload
            continue
        remainder = payload[usable_size:]
        samples = numpy.frombuffer(payload[:usable_size], dtype=numpy.float32)
        if resampler is not None:
            samples = resampler.resample_chunk(samples, last=False)
        output = _float_to_s16_stereo(numpy, samples, args.output_channels)
        if output:
            os.write(sys.stdout.fileno(), output)

    if resampler is not None:
        samples = resampler.resample_chunk(numpy.array([], dtype=numpy.float32), last=True)
        output = _float_to_s16_stereo(numpy, samples, args.output_channels)
        if output:
            os.write(sys.stdout.fileno(), output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
