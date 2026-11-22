"""Read GStreamer pipelines through the installed GStreamer runtime."""

import ctypes as c
import platform
import re
from functools import lru_cache
from pathlib import Path

import numpy as np


class _Error(c.Structure):
    _fields_ = [("domain", c.c_uint), ("code", c.c_int), ("message", c.c_char_p)]


@lru_cache(maxsize=1)
def _load_runtime():
    directories = (
        "/Library/Frameworks/GStreamer.framework/Versions/Current/lib",
        "/opt/homebrew/lib",
        "/usr/local/lib",
        "/opt/local/lib",
    )
    incompatible_architecture = False
    for directory in (*directories, ""):
        suffix = ".0.dylib" if directory else ".so.0"
        try:
            gst = c.CDLL(str(Path(directory) / f"libgstreamer-1.0{suffix}"))
            app = c.CDLL(str(Path(directory) / f"libgstapp-1.0{suffix}"))
        except OSError as error:
            incompatible_architecture |= "incompatible architecture" in str(error)
            continue
        break
    else:
        if incompatible_architecture:
            raise RuntimeError(f"이 Linker에 맞는 {platform.machine()}용 GStreamer 런타임이 필요합니다.")
        raise RuntimeError("GStreamer 런타임을 설치한 뒤 다시 연결하세요.")

    ptr, integer, string = c.c_void_p, c.c_int, c.c_char_p
    int_out, error_out = c.POINTER(integer), c.POINTER(c.POINTER(_Error))
    for lib, name, result, arguments in (
        (gst, "gst_init", None, (ptr, ptr)),
        (gst, "gst_parse_launch", ptr, (string, error_out)),
        (gst, "g_error_free", None, (ptr,)),
        (gst, "gst_bin_get_by_name", ptr, (ptr, string)),
        (gst, "gst_element_set_state", integer, (ptr, integer)),
        (gst, "gst_element_get_bus", ptr, (ptr,)),
        (gst, "gst_bus_pop_filtered", ptr, (ptr, c.c_uint)),
        (gst, "gst_message_parse_error", None, (ptr, error_out, ptr)),
        (gst, "gst_object_unref", None, (ptr,)),
        (gst, "gst_mini_object_unref", None, (ptr,)),
        (gst, "gst_sample_get_caps", ptr, (ptr,)),
        (gst, "gst_sample_get_buffer", ptr, (ptr,)),
        (gst, "gst_caps_get_structure", ptr, (ptr, c.c_uint)),
        (gst, "gst_structure_get_int", integer, (ptr, string, int_out)),
        (gst, "gst_structure_get_fraction", integer, (ptr, string, int_out, int_out)),
        (gst, "gst_buffer_extract", c.c_size_t, (ptr, c.c_size_t, ptr, c.c_size_t)),
        (app, "gst_app_sink_try_pull_sample", ptr, (ptr, c.c_uint64)),
    ):
        function = getattr(lib, name)
        function.restype = result
        function.argtypes = arguments
    gst.gst_init(None, None)
    return gst, app


GST_STATE_NULL = 1
GST_STATE_PLAYING = 4
GST_MESSAGE_ERROR = 1 << 1


class GStreamerCapture:
    def __init__(self, description):
        self.gst, self.app = _load_runtime()
        self.pipeline = self.sink = self.bus = None
        self.fps = 0.0
        # Accept a conventional terminal appsink and replace it with our own sink.
        source = re.sub(r"\s*!\s*appsink\b[^!]*$", "", description).strip()
        pipeline = (f"{source} ! videoconvert ! video/x-raw,format=BGR "
                    "! appsink name=linker_sink sync=false max-buffers=1 drop=true wait-on-eos=false")
        error = c.POINTER(_Error)()
        self.pipeline = self.gst.gst_parse_launch(pipeline.encode(), c.byref(error))
        try:
            if error:
                raise RuntimeError(self._error_text(error))
            if not self.pipeline:
                raise RuntimeError("GStreamer 파이프라인을 열 수 없습니다.")
            self.sink = self.gst.gst_bin_get_by_name(self.pipeline, b"linker_sink")
            self.bus = self.gst.gst_element_get_bus(self.pipeline)
            if not self.sink or not self.bus:
                raise RuntimeError("GStreamer 영상 출력을 열 수 없습니다.")
            if not self.gst.gst_element_set_state(self.pipeline, GST_STATE_PLAYING):
                self._check_error()
                raise RuntimeError("GStreamer 파이프라인을 시작할 수 없습니다.")
        except Exception:
            self.release()
            raise

    def _error_text(self, error):
        try:
            return error.contents.message.decode(errors="replace")
        finally:
            self.gst.g_error_free(error)

    def _check_error(self):
        message = self.gst.gst_bus_pop_filtered(self.bus, GST_MESSAGE_ERROR)
        if message:
            error = c.POINTER(_Error)()
            try:
                self.gst.gst_message_parse_error(message, c.byref(error), None)
                raise RuntimeError(self._error_text(error) if error else "GStreamer 입력 오류")
            finally:
                self.gst.gst_mini_object_unref(message)

    def read(self):
        sample = self.app.gst_app_sink_try_pull_sample(self.sink, 250_000_000)
        if not sample:
            self._check_error()
            return False, None
        try:
            caps = self.gst.gst_sample_get_caps(sample)
            buffer = self.gst.gst_sample_get_buffer(sample)
            if not caps or not buffer:
                return False, None
            info = self.gst.gst_caps_get_structure(caps, 0)
            width, height = c.c_int(), c.c_int()
            if not (self.gst.gst_structure_get_int(info, b"width", c.byref(width)) and
                    self.gst.gst_structure_get_int(info, b"height", c.byref(height))):
                return False, None
            numerator, denominator = c.c_int(), c.c_int()
            if self.gst.gst_structure_get_fraction(info, b"framerate", c.byref(numerator),
                                                   c.byref(denominator)) and denominator.value:
                self.fps = numerator.value / denominator.value
            # Packed BGR rows are padded to a four-byte boundary by GStreamer.
            row_bytes = width.value * 3
            stride = (row_bytes + 3) & ~3
            rows = np.empty((height.value, stride), dtype=np.uint8)
            size = self.gst.gst_buffer_extract(buffer, 0, rows.ctypes.data, rows.nbytes)
            if size != rows.nbytes:
                return False, None
            return True, rows[:, :row_bytes].reshape(height.value, width.value, 3)
        finally:
            self.gst.gst_mini_object_unref(sample)

    def isOpened(self):
        return self.pipeline is not None

    def release(self):
        if self.pipeline:
            self.gst.gst_element_set_state(self.pipeline, GST_STATE_NULL)
        for obj in (self.bus, self.sink, self.pipeline):
            if obj:
                self.gst.gst_object_unref(obj)
        self.pipeline = self.sink = self.bus = None
