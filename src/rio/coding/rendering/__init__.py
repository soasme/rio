"""CLI output formats."""

from enum import StrEnum


class PrintOutputMode(StrEnum):
    human = "human"
    json = "json"
