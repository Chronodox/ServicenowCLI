"""ServiceNow Smart Locator for Playwright."""
from .smart_locator import (
    BUTTON_ACTIONS,
    CONTENT_FRAME_NAMES,
    SmartLocator,
    SmartLocatorError,
    SmartMatch,
)

__all__ = ["SmartLocator", "SmartLocatorError", "SmartMatch", "BUTTON_ACTIONS", "CONTENT_FRAME_NAMES"]
__version__ = "0.1.0"
