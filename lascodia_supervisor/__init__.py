"""lascodia-mt5-supervisor — Phase-12 sidecar daemon for the Lascodia Trading Engine.

Manages MT5 terminal lifecycles per broker account.  Each daemon runs alongside
one or more pre-installed MT5 terminals (one terminal install per broker
account), registers with the engine on startup, and accepts launch / list /
close requests over HTTP from the engine's admin endpoints.

The daemon is intentionally thin — it does not manage broker credentials
(those live inside each MT5 install's saved-login state), does not parse
broker server responses, and does not directly know about EA instances
(those are tracked by the engine via the existing /ea/register surface
once the terminal boots).  Its job is exactly "start / stop / report on
MT5 processes."
"""

__version__ = "0.1.0"
