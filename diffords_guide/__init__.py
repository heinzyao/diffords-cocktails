"""Difford's Guide cocktail scraper and query package."""

__version__ = "3.0.0"
__all__ = ["DiffordsGuideScraper", "DiffordsStorage", "DiffordsExtractor", "LineNotifier"]


def __getattr__(name):
    if name == "DiffordsGuideScraper":
        from .scraper import DiffordsGuideScraper

        return DiffordsGuideScraper
    if name == "DiffordsStorage":
        from .storage import DiffordsStorage

        return DiffordsStorage
    if name == "DiffordsExtractor":
        from .selectors import DiffordsExtractor

        return DiffordsExtractor
    if name == "LineNotifier":
        from .notify import LineNotifier

        return LineNotifier
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
