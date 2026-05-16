import pytest

try:
    import cv2  # noqa: F401
except ModuleNotFoundError:
    HAS_EXECUTOR_DEPS = False
    _annexb_nal_type = None
    _split_complete_annexb_nals = None
else:
    HAS_EXECUTOR_DEPS = True
    from mhxy_bot.executor.main import _annexb_nal_type, _split_complete_annexb_nals


def test_split_complete_annexb_nals_keeps_last_partial() -> None:
    """半包切分应只返回完整 NAL，并保留最后一个未闭合 NAL。"""
    if not HAS_EXECUTOR_DEPS:
        pytest.skip("executor module imports cv2")
    data = (
        b"\x00\x00\x00\x01\x67sps"
        b"\x00\x00\x01\x68pps"
        b"\x00\x00\x00\x01\x65idr-partial"
    )

    nals, rest = _split_complete_annexb_nals(data)

    assert [_annexb_nal_type(nal) for nal in nals] == [7, 8]
    assert rest == b"\x00\x00\x00\x01\x65idr-partial"


def test_split_complete_annexb_nals_emits_cached_idr_when_next_nal_arrives() -> None:
    """下一帧到达后，上一个 IDR NAL 才成为可缓存的完整 NAL。"""
    if not HAS_EXECUTOR_DEPS:
        pytest.skip("executor module imports cv2")
    first = b"\x00\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x00\x01\x65idr"
    nals, rest = _split_complete_annexb_nals(first)
    assert [_annexb_nal_type(nal) for nal in nals] == [7, 8]

    second = rest + b"\x00\x00\x01\x41delta"
    nals, rest = _split_complete_annexb_nals(second)

    assert [_annexb_nal_type(nal) for nal in nals] == [5]
    assert rest == b"\x00\x00\x01\x41delta"
