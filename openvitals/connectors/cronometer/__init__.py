"""Cronometer connector for OpenVitals — emits nutrition Records.

WARNING: This connector uses the unofficial Cronometer mobile API
(reverse-engineered, session-based — not OAuth). It is intended for personal
use against your own Cronometer account only. A provider-side change may break
it without warning; such breakage is classified as an "unsupported provider
change", not a connector bug. Enable explicitly via the opt-in flag.

See docs/specs/connector-contract.md §7 (Cronometer risk box).
"""

from openvitals.connectors.cronometer.connector import CronometerConnector

__all__ = ["CronometerConnector"]
