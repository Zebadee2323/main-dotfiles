"""Measured speech spectrum and audio-reactive caption backgrounds with transparent silence."""
from array import array
import base64
import math
import sys
import wave

MAX_LEVELS = 15000  # 20 kB on the wire, even for the longest supported speech.
COLORS = {
    '36': (35, 220, 235), '35': (220, 105, 240), '32': (60, 230, 155),
    '34': (85, 155, 255), '33': (255, 195, 70),
}


BANDS = 8
FFT_SIZE = 256


def spectrum(samples, channels, rate):
    """Small Hann-windowed FFT; no optional numerical dependency required."""
    stride = max(1, round(rate / 12000))
    sample_rate = rate / stride
    frames = len(samples) // channels
    values = [0j] * FFT_SIZE
    for index in range(FFT_SIZE):
        frame = frames - (FFT_SIZE - index) * stride
        if frame >= 0:
            sample = sum(samples[frame * channels:(frame + 1) * channels]) / (channels * 32768)
            values[index] = complex(sample * (0.5 - 0.5 * math.cos(2 * math.pi * index / (FFT_SIZE - 1))))
    # Iterative radix-2 FFT, with bit-reversed input ordering.
    reverse = 0
    for index in range(1, FFT_SIZE):
        bit = FFT_SIZE >> 1
        while reverse & bit:
            reverse ^= bit
            bit >>= 1
        reverse ^= bit
        if index < reverse:
            values[index], values[reverse] = values[reverse], values[index]
    size = 2
    while size <= FFT_SIZE:
        angle = -2 * math.pi / size
        root = complex(math.cos(angle), math.sin(angle))
        for start in range(0, FFT_SIZE, size):
            rotation = 1 + 0j
            for index in range(start, start + size // 2):
                first, second = values[index], rotation * values[index + size // 2]
                values[index], values[index + size // 2] = first + second, first - second
                rotation *= root
        size *= 2
    powers = [0.0] * BANDS
    edges = (125, 250, 500, 1000, 2000, 3000, 4500)
    for index in range(1, FFT_SIZE // 2 + 1):
        frequency = index * sample_rate / FFT_SIZE
        band = sum(frequency >= edge for edge in edges)
        powers[band] += abs(values[index]) ** 2
    return bytes(min(255, round(math.sqrt(power) * 2.3 / FFT_SIZE * 255)) for power in powers)


def analyse(path):
    """Measure eight frequency bands in the final PCM at up to 50 Hz."""
    with wave.open(str(path), 'rb') as source:
        if source.getsampwidth() != 2 or source.getcomptype() != 'NONE':
            return None
        rate, channels = source.getframerate(), source.getnchannels()
        step = max(1, math.ceil(rate / 50), math.ceil(source.getnframes() / (MAX_LEVELS // BANDS)))
        levels = bytearray()
        while frames := source.readframes(step):
            samples = array('h')
            samples.frombytes(frames)
            if sys.byteorder != 'little':
                samples.byteswap()
            levels.extend(spectrum(samples, channels, rate))
    return {'levels': base64.b64encode(levels).decode('ascii'), 'step': step / rate, 'bands': BANDS}


def decode(value):
    """Bound and validate optional metadata before relaying it to watchers."""
    if not isinstance(value, dict):
        return None
    encoded, step = value.get('levels'), value.get('step')
    bands = value.get('bands', 1)
    if (not isinstance(encoded, str) or len(encoded) > MAX_LEVELS * 4 // 3
            or type(bands) is not int or bands not in (1, BANDS)
            or type(step) not in (int, float) or not math.isfinite(step) or not 0.02 <= step <= 1):
        return None
    try:
        levels = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    return (levels, step, bands) if 0 < len(levels) <= MAX_LEVELS and len(levels) % bands == 0 else None


def render(text, width, ink, data, elapsed, volume=100):
    """A two-second audio glow behind fixed text, with transparent quiet cells.

    Only audible columns receive a colored background. Default background resets
    preserve terminal transparency everywhere else; no dark floor or extra glyphs.
    """
    from speech import characters, width as cells
    levels, step, bands = data if data else (b'', 0.02, 1)
    tint = COLORS.get(ink, COLORS['36'])
    output, column = ['\x1b[49m\x1b[38;2;245;245;245m'], 0
    padded = text + ' ' * max(0, width - cells(text))
    cache = {}

    def energy_at(frame):
        if frame not in cache:
            start = frame * bands
            if 0 <= frame < len(levels) // bands:
                # Combine the measured bands into a broadband speech envelope.
                cache[frame] = math.sqrt(sum((value / 255) ** 2 for value in levels[start:start + bands]))
            else:
                cache[frame] = 0
        return cache[frame]

    for char in characters(padded):
        at = elapsed - 2 * (1 - column / max(1, width - 1))
        position = at / step
        index = math.floor(position)
        fraction = position - index
        amplitude = (energy_at(index - 1) * (1 - fraction) + energy_at(index) * fraction) if at >= 0 else 0
        energy = min(1, amplitude * volume / 100 * 3.6)
        if energy > 0.04:
            strength = 0.12 + 0.38 * energy
            bg = tuple(round(component * strength) for component in tint)
            background = f'\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m'
        else:
            background = '\x1b[49m'
        output.append(background + char)
        column += cells(char)
    return ''.join(output) + '\x1b[0m'
