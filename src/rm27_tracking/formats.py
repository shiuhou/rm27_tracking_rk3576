"""Convert linear, single-planar camera payloads to owned uint8 BGR arrays.

YUV conversion uses OpenCV's BT.601 limited-range conversion. Driver colorimetry
must be checked by the acquisition adapter; tiled/multiplanar layouts are excluded.
"""
import cv2
import numpy as np

SUPPORTED_FORMATS = {"NV12", "NV21", "YUYV", "UYVY", "BGR3", "RGB3", "GREY", "MJPG"}


def validate_colorimetry(fmt):
    """Match linear YUV metadata to the converter's BT.601 limited range."""
    if fmt['fourcc'] in {"NV12", "NV21", "YUYV", "UYVY"}:
        colorspace = fmt.get('colorspace', 0)
        encoding = fmt.get('ycbcr_enc', 0) or (2 if colorspace in {3, 12} else
                                              (6 if colorspace == 10 else (8 if colorspace == 2 else 1)))
        quantization = fmt.get('quantization', 0) or (1 if colorspace == 7 else 2)
        if not colorspace or encoding != 1 or quantization != 2:
            raise ValueError('YUV conversion requires driver-declared BT.601 limited-range colorimetry')


def decode_buffer(payload, width: int, height: int, fourcc: str, stride: int = 0):
    """Return owned BGR pixels; input may be a borrowed MMAP buffer.

    stride is bytes per image/luma row. For NV12/NV21 chroma has that same
    stride and follows height luma rows, as defined for contiguous V4L2 NV12.
    """
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("pixel dimensions must be positive integers")
    if fourcc not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported linear single-planar pixel format: {fourcc}")
    raw = active = pixels = None
    try:
        if fourcc == "MJPG":
            raw = np.frombuffer(payload, dtype=np.uint8)
            image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != (height, width):
                raise ValueError("invalid MJPG payload or decoded dimensions differ from negotiated mode")
            return image
        channels = 3 if fourcc in {"BGR3", "RGB3"} else (2 if fourcc in {"YUYV", "UYVY"} else 1)
        if fourcc in {"NV12", "NV21", "YUYV", "UYVY"} and width % 2:
            raise ValueError("YUV width must be even")
        if fourcc in {"NV12", "NV21"} and height % 2:
            raise ValueError("NV12/NV21 height must be even")
        row_bytes = width * channels
        stride = stride or row_bytes
        if type(stride) is not int or stride < row_bytes:
            raise ValueError("stride is smaller than active row")
        rows = height * 3 // 2 if fourcc in {"NV12", "NV21"} else height
        needed = (rows - 1) * stride + row_bytes
        if len(payload) < needed:
            raise ValueError(f"short {fourcc} payload: need {needed}, got {len(payload)} bytes")
        active = np.ndarray((rows, row_bytes), dtype=np.uint8, buffer=payload, strides=(stride, 1))
        if fourcc == "BGR3":
            return active.reshape(height, width, 3).copy()
        if fourcc == "RGB3":
            return cv2.cvtColor(active.reshape(height, width, 3), cv2.COLOR_RGB2BGR)
        if fourcc == "GREY":
            return cv2.cvtColor(active, cv2.COLOR_GRAY2BGR)
        code = {"NV12": cv2.COLOR_YUV2BGR_NV12, "NV21": cv2.COLOR_YUV2BGR_NV21,
                "YUYV": cv2.COLOR_YUV2BGR_YUY2, "UYVY": cv2.COLOR_YUV2BGR_UYVY}[fourcc]
        pixels = active if fourcc in {"NV12", "NV21"} else active.reshape(height, width, 2)
        return cv2.cvtColor(pixels, code)
    finally:
        # Exceptions retain tracebacks: remove borrowed ndarray views before MMAP
        # teardown, including decoder/shape errors. Returned pixels always own data.
        raw = active = pixels = None
