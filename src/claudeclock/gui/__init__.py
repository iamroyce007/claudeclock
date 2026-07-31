"""Graphical front-ends: the macOS menu bar item, the Windows tray icon, and
the shared detail panel.

All of them are *readers* of `live.json`; none of them polls Anthropic. That
keeps a single owner of the network and lets each toolkit have its own process
and main thread.
"""
