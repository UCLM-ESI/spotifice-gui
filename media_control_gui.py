#!/usr/bin/env python3

import logging
import signal
import sys
from pathlib import Path
from time import sleep

import gi

gi.require_version('Gdk', '4.0')
gi.require_version('Gtk', '4.0')
from gi.repository import Gdk, Gtk, GLib  # noqa: E402

import Ice  # noqa: E402

SLICE = Path(__file__).resolve().parent / 'spotifice_v1.ice'
Ice.loadSlice(f'-I{Ice.getSliceDir()} {SLICE}')
import Spotifice  # type: ignore # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TITLE_WIDTH = 45
POLL_MS = 1000

STATE_NAMES = {
    Spotifice.PlaybackState.PLAYING: "Playing",
    Spotifice.PlaybackState.PAUSED: "Paused",
    Spotifice.PlaybackState.STOPPED: "Stopped",
}


def get_proxy(ic, property, cls):
    proxy = ic.propertyToProxy(property)

    for _ in range(5):
        try:
            proxy.ice_ping()
            break
        except Ice.ConnectionRefusedException:
            sleep(0.5)

    object = cls.checkedCast(proxy)
    if object is None:
        raise RuntimeError(f'Invalid proxy for {property}')

    return object


def describe_error(e):
    "Readable text for Ice user exceptions (Spotifice.Error) and others"
    if isinstance(e, Spotifice.Error):
        item = f" ({e.item})" if e.item else ""
        return f"{type(e).__name__}: {e.reason}{item}"
    return str(e)


def handle_action_error(func):
    "Decorator to handle exceptions in action methods"
    action_name = func.__name__.replace('on_', '').replace('_', ' ')

    def wrapper(self, *args):
        try:
            return func(self, *args)
        except Exception as e:
            self.update_status(f"Error in {action_name}(): {describe_error(e)}")
    return wrapper


class SpotificeControlWindow(Gtk.ApplicationWindow):
    def __init__(self, app, communicator):
        super().__init__(application=app, title="Spotifice Control")
        self.set_resizable(False)

        self.communicator = communicator
        self.provider, self.render = self.init_ice_proxies()

        self.track_ids = []
        self.track_full_text = ""
        self.track_scroll_offset = 0
        self.track_animation_timeout = None
        self._updating_ui = False  # avoid handling signals we trigger ourselves

        self.load_css()
        self.create_ui()
        self.load_tracks()
        self.load_first_track()
        self.refresh_status()
        GLib.timeout_add(POLL_MS, self.poll_status)

    def init_ice_proxies(self):
        try:
            provider = get_proxy(
                self.communicator, 'Spotifice.MediaProvider.Proxy', Spotifice.MediaProviderPrx)
            render = get_proxy(
                self.communicator, 'Spotifice.MediaRender.Proxy', Spotifice.MediaRenderPrx)
            render.bind_media_provider(provider)
        except Exception as e:
            logger.error(f"Error initializing Ice proxies: {describe_error(e)}")
            sys.exit(1)

        return provider, render

    # ---- UI ----------------------------------------------------------

    @staticmethod
    def load_css():
        "Highlight active buttons: theme accent for repeat, green/amber/red for play/pause/stop"
        provider = Gtk.CssProvider()
        provider.load_from_string("""
            button.active-state, button.active-state:hover {
                background-image: none;
                background-color: alpha(@theme_selected_bg_color, 0.35);
                box-shadow: inset 0 0 0 1px @theme_selected_bg_color;
            }
            button.state-play, button.state-play:hover {
                background-image: none;
                background-color: alpha(#2ec27e, 0.35);
                box-shadow: inset 0 0 0 1px #2ec27e;
            }
            button.state-pause, button.state-pause:hover {
                background-image: none;
                background-color: alpha(#e5a50a, 0.35);
                box-shadow: inset 0 0 0 1px #e5a50a;
            }
            button.state-stop, button.state-stop:hover {
                background-image: none;
                background-color: alpha(#e01b24, 0.35);
                box-shadow: inset 0 0 0 1px #e01b24;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def create_ui(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(box, f'set_margin_{side}')(15)

        # track selector
        selector = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label="Track:", xalign=0)
        label.set_size_request(70, -1)
        self.track_model = Gtk.StringList()
        self.track_dropdown = Gtk.DropDown(model=self.track_model)
        self.track_dropdown.set_hexpand(True)
        self.track_dropdown.connect("notify::selected", self.on_track_selected)
        selector.append(label)
        selector.append(self.track_dropdown)

        # playback controls
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_halign(Gtk.Align.CENTER)
        controls.set_homogeneous(True)
        self.previous_button = self._button("media-skip-backward", "Previous", self.on_previous)
        self.play_button = self._button("media-playback-start", "Play", self.on_play)
        self.pause_button = self._button("media-playback-pause", "Pause", self.on_pause)
        self.stop_button = self._button("media-playback-stop", "Stop", self.on_stop)
        self.repeat_button = self._button(
            "media-playlist-repeat", "Repeat", self.on_repeat, toggle=True)
        for b in (self.previous_button, self.play_button, self.pause_button,
                  self.stop_button, self.repeat_button):
            controls.append(b)

        # current track
        self.track_label = Gtk.Label(label="No track loaded")
        self.track_label.set_ellipsize(3)
        self.track_label.set_margin_top(10)
        self.track_label.set_selectable(True)
        self.track_label.add_css_class("title-3")
        self.track_label.set_width_chars(TITLE_WIDTH)
        self.track_label.set_max_width_chars(TITLE_WIDTH)
        self.track_label.set_size_request(400, -1)

        # status bar
        statusbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        statusbar.add_css_class("statusbar")
        self.status_label = Gtk.Label(label="Ready", xalign=0)
        self.status_label.set_margin_start(10)
        self.status_label.set_margin_end(10)
        self.status_label.set_margin_top(5)
        self.status_label.set_margin_bottom(5)
        statusbar.append(self.status_label)

        for w in (selector, controls, self.track_label, statusbar):
            box.append(w)
        self.set_child(box)

    @staticmethod
    def _button(icon_name, tooltip, callback, toggle=False):
        button = Gtk.ToggleButton() if toggle else Gtk.Button()
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_icon_size(Gtk.IconSize.LARGE)
        button.set_child(image)
        button.set_tooltip_text(tooltip)
        button.connect("toggled" if toggle else "clicked", callback)
        return button

    def update_status(self, message):
        self.status_label.set_text(message)

    def update_button_states(self, state):
        buttons = {
            Spotifice.PlaybackState.PLAYING: (self.play_button, "state-play"),
            Spotifice.PlaybackState.PAUSED: (self.pause_button, "state-pause"),
            Spotifice.PlaybackState.STOPPED: (self.stop_button, "state-stop"),
        }
        for button, css_class in buttons.values():
            button.remove_css_class(css_class)

        if state in buttons:
            button, css_class = buttons[state]
            button.add_css_class(css_class)

    def update_repeat_style(self):
        "Highlight the repeat button with the theme accent colour while active"
        if self.repeat_button.get_active():
            self.repeat_button.add_css_class("active-state")
        else:
            self.repeat_button.remove_css_class("active-state")

    def set_track_title(self, title):
        "Show the title, scrolling it if it does not fit"
        if title == self.track_full_text:
            return

        if self.track_animation_timeout is not None:
            GLib.source_remove(self.track_animation_timeout)
            self.track_animation_timeout = None

        self.track_full_text = title
        self.track_scroll_offset = 0
        self.track_label.set_text(title)

        if len(title) > TITLE_WIDTH:
            self.track_animation_timeout = GLib.timeout_add(200, self.animate_track_title)

    def animate_track_title(self):
        text = self.track_full_text
        if len(text) <= TITLE_WIDTH:
            self.track_animation_timeout = None
            return False

        display = text[self.track_scroll_offset:self.track_scroll_offset + TITLE_WIDTH]
        if len(display) < TITLE_WIDTH:
            display += " ... " + text[:TITLE_WIDTH - len(display) - 5]
        self.track_label.set_text(display)

        self.track_scroll_offset = (self.track_scroll_offset + 1) % (len(text) + 5)
        return True

    # ---- Ice state ---------------------------------------------------

    def load_tracks(self):
        try:
            tracks = self.provider.get_all_tracks()
        except Exception as e:
            logger.error(f"Error loading tracks: {describe_error(e)}")
            self.update_status(f"Error loading tracks: {describe_error(e)}")
            return

        self._updating_ui = True
        try:
            for track in tracks:
                self.track_model.append(track.title)
                self.track_ids.append(track.id)
            if not tracks:
                self.update_status("The provider has no tracks")
            else:
                self.track_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
        finally:
            self._updating_ui = False

    def load_first_track(self):
        "Load the first track if the render has none loaded yet"
        if not self.track_ids:
            return
        try:
            if self.render.get_status().current_track is None:
                self.render.load_track(self.track_ids[0])
        except Exception as e:
            self.update_status(f"Error loading first track: {describe_error(e)}")

    def refresh_status(self):
        "Synchronise the UI with the render state. Returns False on failure."
        try:
            status = self.render.get_status()
        except Exception as e:
            self.update_status(f"Render unavailable: {describe_error(e)}")
            return False

        track = status.current_track
        self.set_track_title(track.title if track and track.title else "No track loaded")
        self.update_button_states(status.state)

        self._updating_ui = True
        try:
            self.repeat_button.set_active(bool(status.is_repeating))
            self.update_repeat_style()
            index = Gtk.INVALID_LIST_POSITION
            if track and track.id in self.track_ids:
                index = self.track_ids.index(track.id)
            if self.track_dropdown.get_selected() != index:
                self.track_dropdown.set_selected(index)
        finally:
            self._updating_ui = False

        self.update_status(STATE_NAMES.get(status.state, "Ready"))
        return True

    def poll_status(self):
        self.refresh_status()
        return True

    # ---- actions -----------------------------------------------------

    def on_track_selected(self, dropdown, _pspec):
        if self._updating_ui:
            return
        index = dropdown.get_selected()
        if index == Gtk.INVALID_LIST_POSITION or index >= len(self.track_ids):
            return

        try:
            self.render.load_track(self.track_ids[index])
        except Exception as e:
            self.update_status(f"Error loading track: {describe_error(e)}")
            return
        self.refresh_status()

    @handle_action_error
    def on_play(self, button):
        self.render.play()
        self.refresh_status()

    @handle_action_error
    def on_pause(self, button):
        self.render.pause()
        self.refresh_status()

    @handle_action_error
    def on_stop(self, button):
        self.render.stop()
        self.refresh_status()

    @handle_action_error
    def on_previous(self, button):
        self.render.previous()
        self.refresh_status()

    @handle_action_error
    def on_repeat(self, button):
        if self._updating_ui:
            return
        self.update_repeat_style()
        self.render.set_repeat(bool(button.get_active()))
        self.refresh_status()


class SpotificeApp(Gtk.Application):
    def __init__(self, communicator):
        super().__init__(application_id='es.uclm.spotifice')
        self.communicator = communicator
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = SpotificeControlWindow(self, self.communicator)
        self.window.present()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit("Usage: media_control_gui.py <config-file>")

    with Ice.initialize(sys.argv[1]) as communicator:
        app = SpotificeApp(communicator)
        signal.signal(signal.SIGINT, lambda sig, frame: app.quit())
        app.run(None)
