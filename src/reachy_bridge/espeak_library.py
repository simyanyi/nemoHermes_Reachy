"""Generate PCM WAV using the installed eSpeak library, without host playback.

Run in a child process to isolate eSpeak's global state and support cancellation.
Uses the public espeak-ng speak_lib.h ABI; no additional Python package needed.
"""

import ctypes as c
import ctypes.util
import io
import sys
import wave


def library_name():
    return ctypes.util.find_library("espeak-ng") or ctypes.util.find_library("espeak")


def synthesize(text: str, voice: str, rate: int, pitch: int = 65, pitch_range: int = 75) -> bytes:
    name = library_name()
    if not name:
        raise RuntimeError("No installed eSpeak speech library found")
    lib = c.CDLL(name)
    callback_type = c.CFUNCTYPE(c.c_int, c.POINTER(c.c_short), c.c_int, c.c_void_p)
    lib.espeak_Initialize.argtypes = [c.c_int, c.c_int, c.c_char_p, c.c_int]
    lib.espeak_Initialize.restype = c.c_int
    lib.espeak_SetSynthCallback.argtypes = [callback_type]
    lib.espeak_SetSynthCallback.restype = None
    lib.espeak_SetVoiceByName.argtypes = [c.c_char_p]
    lib.espeak_SetVoiceByName.restype = c.c_int
    lib.espeak_SetParameter.argtypes = [c.c_int, c.c_int, c.c_int]
    lib.espeak_SetParameter.restype = c.c_int
    lib.espeak_Synth.argtypes = [
        c.c_void_p,
        c.c_size_t,
        c.c_uint,
        c.c_int,
        c.c_uint,
        c.c_uint,
        c.POINTER(c.c_uint),
        c.c_void_p,
    ]
    lib.espeak_Synth.restype = c.c_int
    lib.espeak_Terminate.argtypes = []
    lib.espeak_Terminate.restype = c.c_int
    # AUDIO_OUTPUT_SYNCHRONOUS=2 retrieves samples, never opens the host speaker.
    # DONT_EXIT=0x8000 turns missing voice data into an error instead of process exit.
    sample_rate = lib.espeak_Initialize(2, 0, None, 0x8000)
    if sample_rate <= 0:
        raise RuntimeError("Installed eSpeak voice data could not be initialized")
    pcm = bytearray()
    too_long = False

    @callback_type
    def collect(samples, count, events):
        nonlocal too_long
        if samples and count > 0:
            if len(pcm) + count * 2 > sample_rate * 2 * 60:
                too_long = True
                return 1
            pcm.extend(c.string_at(samples, count * 2))
        return 0

    try:
        lib.espeak_SetSynthCallback(collect)
        if lib.espeak_SetVoiceByName(voice.encode()) != 0:
            raise RuntimeError("Configured eSpeak voice is unavailable")
        if lib.espeak_SetParameter(1, rate, 0) != 0:  # espeakRATE=1
            raise RuntimeError("Could not configure speech rate")
        for parameter, value in [(3, pitch), (4, pitch_range)]:
            if lib.espeak_SetParameter(parameter, value, 0) != 0:
                raise RuntimeError("Could not configure voice expression")
        data = text.encode("utf-8") + b"\0"
        # UTF8 + ENDPAUSE; no SSML/phoneme interpretation or external audio tags.
        if lib.espeak_Synth(data, len(data), 0, 1, 0, 0x1001, None, None) != 0:
            raise RuntimeError("Speech synthesis failed")
        if too_long or not pcm:
            raise RuntimeError("Speech is empty or exceeds 60 seconds")
    finally:
        lib.espeak_Terminate()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def main():
    text = sys.stdin.buffer.read(8193)
    if len(text) > 8192:
        raise ValueError("Speech text too large")
    sys.stdout.buffer.write(
        synthesize(text.decode(), sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    )


if __name__ == "__main__":
    main()
