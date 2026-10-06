#!/usr/bin/env python3

import logging
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from time import sleep

import gi

gi.require_version('Gdk', '4.0')
gi.require_version('Gtk', '4.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import Ice  # noqa: E402

SLICE = Path(__file__).resolve().parent / 'spotifice_v1.ice'
Ice.loadSlice(f'-I{Ice.getSliceDir()} {SLICE}')
import Spotifice  # type: ignore # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TITLE_WIDTH = 45
POLL_MS = 1000
ERROR_HOLD_MS = 5000  # keep errors readable in spite of the status polling
NO_TRACK = "No track loaded"

ERROR_COLOUR = "#e01b24"  # also the stop colour

# playback state -> (status text, css class, highlight colour)
STATES = {
    Spotifice.PlaybackState.PLAYING: ("Playing", "state-play", "#2ec27e"),
    Spotifice.PlaybackState.PAUSED: ("Paused", "state-pause", "#e5a50a"),
    Spotifice.PlaybackState.STOPPED: ("Stopped", "state-stop", ERROR_COLOUR),
}

# repeat is not a playback state, so it uses the theme accent instead
REPEAT_STYLE = ("state-repeat", "@theme_selected_bg_color")

HIGHLIGHT_CSS = """
button.{css_class}, button.{css_class}:hover {{
    background-image: none;
    background-color: alpha({colour}, 0.35);
    box-shadow: inset 0 0 0 1px {colour};
}}"""

ERROR_CSS = f"""
label.status-error {{
    color: {ERROR_COLOUR};
}}"""


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
    "Single line description for Ice user exceptions (Spotifice.Error) and others"
    if isinstance(e, Spotifice.Error):
        item = f" ({e.item})" if e.item else ""
        text = f"{type(e).__name__}: {e.reason}{item}"
    else:
        text = str(e) or type(e).__name__

    return " ".join(text.split())  # Ice messages span several lines


def application_css():
    "One highlight rule per playback state, plus repeat and the error text"
    styles = [style for _, *style in STATES.values()] + [REPEAT_STYLE]
    return ERROR_CSS + "".join(
        HIGHLIGHT_CSS.format(css_class=css_class, colour=colour)
        for css_class, colour in styles
    )


def set_css_class(widget, css_class, enabled):
    if enabled:
        widget.add_css_class(css_class)
    else:
        widget.remove_css_class(css_class)


def handle_action_error(func):
    "Decorator to handle exceptions in action methods"
    action_name = func.__name__.replace('on_', '').replace('_', ' ')

    def wrapper(self, *args):
        try:
            return func(self, *args)
        except Exception as e:
            self.show_error(f"Error in {action_name}(): {describe_error(e)}")
    return wrapper


class SpotificeControlWindow(Gtk.ApplicationWindow):
    def __init__(self, app, communicator):
        super().__init__(application=app, title="Spotifice Control")
        self.set_resizable(False)

        self.communicator = communicator
        self.provider, self.render = self.init_ice_proxies()

        self.track_ids = []
        self.updating_ui = False   # ignore signals for changes we make ourselves
        self.status_pending = False  # a get_status() reply is on its way
        self.error_until = 0         # monotonic time while an error holds the status bar

        self.load_css()
        self.create_ui()
        self.load_tracks()
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
        provider = Gtk.CssProvider()
        provider.load_from_string(application_css())
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
        for button in (self.previous_button, self.play_button, self.pause_button,
                       self.stop_button, self.repeat_button):
            controls.append(button)

        self.state_buttons = {
            Spotifice.PlaybackState.PLAYING: self.play_button,
            Spotifice.PlaybackState.PAUSED: self.pause_button,
            Spotifice.PlaybackState.STOPPED: self.stop_button,
        }

        # current track
        self.track_label = Gtk.Label(label=NO_TRACK)
        self.track_label.set_ellipsize(Pango.EllipsizeMode.END)
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

        for widget in (selector, controls, self.track_label, statusbar):
            box.append(widget)
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

    @contextmanager
    def ui_update(self):
        "Suppress the handlers of the widgets we are about to update"
        self.updating_ui = True
        try:
            yield
        finally:
            self.updating_ui = False

    def update_status(self, message):
        "Routine status, which never hides a recent error"
        if GLib.get_monotonic_time() < self.error_until:
            return

        set_css_class(self.status_label, "status-error", False)
        self.status_label.set_text(message)

    def show_error(self, message):
        "Error status, held for a while so that the polling cannot hide it"
        self.error_until = GLib.get_monotonic_time() + ERROR_HOLD_MS * 1000
        set_css_class(self.status_label, "status-error", True)
        self.status_label.set_text(message)

    def update_button_states(self, state):
        "Highlight the button matching the current playback state"
        for candidate, button in self.state_buttons.items():
            set_css_class(button, STATES[candidate][1], candidate == state)

    def update_repeat_style(self):
        set_css_class(self.repeat_button, REPEAT_STYLE[0], self.repeat_button.get_active())

    def set_track_title(self, title):
        if title != self.track_label.get_text():
            self.track_label.set_text(title)
            self.track_label.set_tooltip_text(title)

    # ---- Ice calls (asynchronous, to keep the GTK thread responsive) --

    def ice_call(self, future, action, on_result=None):
        """Run an Ice invocation without blocking the GTK thread. Its outcome
        is applied back in that thread, refreshing the status by default."""
        def done(future):
            try:
                result = future.result()
            except Exception as e:
                GLib.idle_add(self.on_call_failed, action, e)
            else:
                GLib.idle_add(self.on_call_done, result, on_result)

        future.add_done_callback(done)

    def on_call_failed(self, action, error):
        self.status_pending = False
        self.show_error(f"Error in {action}(): {describe_error(error)}")
        return GLib.SOURCE_REMOVE

    def on_call_done(self, result, on_result):
        if on_result is None:
            self.refresh_status()
        else:
            on_result(result)
        return GLib.SOURCE_REMOVE

    # ---- render state ------------------------------------------------

    def load_tracks(self):
        self.ice_call(self.provider.get_all_tracksAsync(), "get_all_tracks", self.apply_tracks)

    def apply_tracks(self, tracks):
        with self.ui_update():
            for track in tracks:
                self.track_model.append(track.title)
                self.track_ids.append(track.id)
            self.track_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)

        if not tracks:
            self.show_error("The provider has no tracks")
            return

        self.load_first_track()

    def load_first_track(self):
        "Load the first track unless the render already has one"
        def on_status(status):
            if status.current_track is None:
                self.ice_call(self.render.load_trackAsync(self.track_ids[0]), "load_track")
            else:
                self.apply_status(status)

        self.ice_call(self.render.get_statusAsync(), "get_status", on_status)

    def refresh_status(self):
        "Ask the render for its state; the UI follows when the reply arrives"
        if self.status_pending:
            return

        self.status_pending = True
        self.ice_call(self.render.get_statusAsync(), "get_status", self.apply_status)

    def apply_status(self, status):
        self.status_pending = False

        track = status.current_track
        self.set_track_title(track.title if track and track.title else NO_TRACK)
        self.update_button_states(status.state)

        with self.ui_update():
            self.repeat_button.set_active(bool(status.is_repeating))
            self.update_repeat_style()

            index = Gtk.INVALID_LIST_POSITION
            if track and track.id in self.track_ids:
                index = self.track_ids.index(track.id)
            if self.track_dropdown.get_selected() != index:
                self.track_dropdown.set_selected(index)

        self.update_status(STATES[status.state][0] if status.state in STATES else "Ready")

    def poll_status(self):
        self.refresh_status()
        return GLib.SOURCE_CONTINUE

    # ---- actions -----------------------------------------------------

    @handle_action_error
    def on_track_selected(self, dropdown, _pspec):
        if self.updating_ui:
            return

        index = dropdown.get_selected()
        if index == Gtk.INVALID_LIST_POSITION or index >= len(self.track_ids):
            return

        self.ice_call(self.render.load_trackAsync(self.track_ids[index]), "load_track")

    @handle_action_error
    def on_play(self, button):
        self.ice_call(self.render.playAsync(), "play")

    @handle_action_error
    def on_pause(self, button):
        self.ice_call(self.render.pauseAsync(), "pause")

    @handle_action_error
    def on_stop(self, button):
        self.ice_call(self.render.stopAsync(), "stop")

    @handle_action_error
    def on_previous(self, button):
        self.ice_call(self.render.previousAsync(), "previous")

    @handle_action_error
    def on_repeat(self, button):
        if self.updating_ui:
            return

        self.update_repeat_style()
        self.ice_call(self.render.set_repeatAsync(button.get_active()), "set_repeat")


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
