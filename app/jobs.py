# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
from __future__ import annotations

import re
from dataclasses import dataclass


QUANTITY_PREFIX = re.compile(r"^\s*(?P<quantity>\d+)\s*[xX×]\s*(?P<text>.+?)\s*$")


@dataclass(frozen=True)
class BomInputLine:
    position: int
    menge: int
    originaltext: str
    suchtext: str


def parse_bom(text: str) -> list[BomInputLine]:
    lines: list[BomInputLine] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        match = QUANTITY_PREFIX.match(stripped)
        if match:
            quantity = int(match.group("quantity"))
            search_text = match.group("text").strip()
        else:
            quantity = 1
            search_text = stripped
        if quantity < 1 or not search_text:
            continue
        lines.append(
            BomInputLine(
                position=len(lines) + 1,
                menge=quantity,
                originaltext=stripped,
                suchtext=search_text,
            )
        )
    return lines
