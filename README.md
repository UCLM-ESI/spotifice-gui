# Spotifice GUI

GTK4 graphical media control for Spotifice, compatible with its `spotifice_v1.ice`.

Features: track selector (from `MediaProvider.get_all_tracks`), Play / Pause / Stop / Previous, repeat toggle,
scrolling title and periodic status polling (`get_status`) so the UI follows the render state.


## Install

```bash
sudo apt install python3-zeroc-ice python3-gi gir1.2-gtk-4.0
make run   # or: python3 media_control_gui.py control.config
```

`control.config` holds the `Spotifice.MediaProvider.Proxy` and `Spotifice.MediaRender.Proxy` proxies.
