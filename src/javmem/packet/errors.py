"""拒收（packet-spec 第 9 节）。错误说明不抄原文，只写字段名和行号。"""

from __future__ import annotations

BAD_HEADER = "bad_header"
UNKNOWN_SOURCE = "unknown_source"
ENV_MISMATCH = "env_mismatch"
NO_RAW_SECTION = "no_raw_section"
TIME_OUT_OF_RANGE = "time_out_of_range"
NOT_UTF8 = "not_utf8"

REJECT_CODES = (BAD_HEADER, UNKNOWN_SOURCE, ENV_MISMATCH, NO_RAW_SECTION, TIME_OUT_OF_RANGE, NOT_UTF8)


class Rejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
