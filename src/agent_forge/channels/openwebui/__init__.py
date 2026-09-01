"""OpenWebUI channel.

OpenWebUI connects to the cell through the OpenAI-compatible channel with nothing but
the base URL and an API key, so there is no router here. ``pipe.py`` is an optional
file the operator copies **into** OpenWebUI; it is never imported by the cell.
"""
