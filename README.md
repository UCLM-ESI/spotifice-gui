# Spotifice GUI

GTK4 graphical media control for Spotifice, compatible with its `spotifice_v1.ice`.
It also drives a `spotifice_v0.ice` server, which it recognises on its own.

![Screenshot](screenshot.png)

Features:

- Track list from `MediaProvider.get_all_tracks`: a click, or Enter, loads a track into the render.
- Play / Pause / Stop / Previous and a repeat toggle. The button matching the current state is highlighted.
- Periodic `get_status` polling, so the UI follows the render even when another client drives it.
- The servers do not have to be running first: the window opens waiting for them and the status bar names
  the one it needs, `media render` or `media provider`, telling a render that is not there from a render
  that cannot reach the provider. It binds and fills itself as soon as they answer, waits again if they go
  away (retrying every few seconds), and the terminal log says why it is waiting.
- A v0 render is detected and driven too. Both versions share their Ice type ids, so the version is told
  apart by invoking: a render without `get_status` answers `OperationNotExistException`. The GUI then hides
  pause, previous and repeat, which v0 does not define, follows the loaded track with `get_current_track`,
  and shows no playback state, because that interface cannot report one. The probe is repeated on every
  reconnection, so restarting the servers with the other version is enough for the window to adapt.
- Every invocation is asynchronous, so a slow or dead render never blocks the window.
- No control is ever disabled and nothing is checked locally first: every button always provokes its remote
  call, so what you see is how the remote objects answer, errors included. The status bar names the failed
  operation, and the GUI's own calls never bury the error of what you just pressed.


## Install

```bash
sudo apt install python3-zeroc-ice python3-gi gir1.2-gtk-4.0
make run   # or: python3 media_control_gui.py control.config
```

`control.config` holds the `Spotifice.MediaProvider.Proxy` and `Spotifice.MediaRender.Proxy` proxies.


## Tests

`make test` runs the tests, which need neither a display nor a running server: they cover the
asynchronous client (including the operation names generated from the slice), the error messages
and the state table.


## Authorship

This program was generated 100% with AI: Claude Code (Claude Opus 5), prompted and reviewed by
its author. Every change was checked
against a running `MediaProvider` and `MediaRender`.
