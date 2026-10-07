#!/usr/bin/env python3

import logging
import signal
import sys
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path

import gi

gi.require_version('Gdk', '4.0')
gi.require_version('Gtk', '4.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import Ice  # noqa: E402

HERE = Path(__file__).resolve().parent
STYLE = HERE / 'style.css'
SLICE = HERE / 'spotifice_v1.ice'
Ice.loadSlice(f'-I{Ice.getSliceDir()} {SLICE}')
import Spotifice  # type: ignore # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WINDOW_TITLE = "Spotifice Control"
WINDOW_WIDTH = 440
TITLE_WIDTH = 45
LIST_ROWS = 12    # tracks visible without scrolling
ROW_HEIGHT = 18   # rough row height, until there is a real row to measure
POLL_MS = 1000    # how often the render state is followed
RETRY_MS = 3000   # how often the servers are retried while waiting for them
ERROR_HOLD_MS = 5000  # keep errors readable in spite of the status polling
MEDIA_PROVIDER = "media provider"
MEDIA_RENDER = "media render"

# the calls the GUI makes by itself, and the server each one needs
OWN_CALLS = {
    "get_status": MEDIA_RENDER,
    "get_current_track": MEDIA_RENDER,
    "bind_media_provider": MEDIA_RENDER,
    "get_all_tracks": MEDIA_PROVIDER,
}
NO_TRACK = "No track loaded"

ERROR_ICON = "dialog-error-symbolic"
REPEAT_CSS_CLASS = "state-repeat"

# the css classes are the ones defined in style.css
State = namedtuple('State', 'text css_class icon')

STATES = {
    Spotifice.PlaybackState.PLAYING:
        State("Playing", "state-play", "media-playback-start-symbolic"),
    Spotifice.PlaybackState.PAUSED:
        State("Paused", "state-pause", "media-playback-pause-symbolic"),
    Spotifice.PlaybackState.STOPPED:
        State("Stopped", "state-stop", "media-playback-stop-symbolic"),
}

READY = State("Ready", "", None)  # before the render tells us anything
BASIC = State("v0 interface: no playback status", "", "dialog-information-symbolic")



def get_proxy(ic, property, cls):
    """An unchecked proxy, which costs no message: the servers may well not be
    running yet, and the GUI waits for them instead of giving up."""
    proxy = ic.propertyToProxy(property)
    if proxy is None:
        raise RuntimeError(f'Missing property {property}')

    return cls.uncheckedCast(proxy)


def describe_error(e):
    "Single line description for Ice user exceptions (Spotifice.Error) and others"
    if isinstance(e, Spotifice.Error):
        item = f" ({e.item})" if e.item else ""
        text = f"{type(e).__name__}: {e.reason}{item}"
    else:
        text = str(e) or type(e).__name__

    return " ".join(text.split())  # Ice messages span several lines


def waiting_for(server):
    "What the status bar shows while one of the servers is missing"
    return State(f"Waiting for the {server}…", "", "content-loading-symbolic")


def set_css_class(widget, css_class, enabled):
    if enabled:
        widget.add_css_class(css_class)
    else:
        widget.remove_css_class(css_class)


class SpotificeClient:
    """Asynchronous access to the Spotifice services: calls return at once and
    the reply is delivered in the GTK thread, to the given callback for a read,
    to `on_changed` after a successful write, and to `on_error` on failure."""

    def __init__(self, provider, render, on_error, on_changed):
        self.provider = provider
        self.render = render
        self.on_error = on_error
        self.on_changed = on_changed
        self.playback_status = None  # whether the render implements get_status()

    @classmethod
    def from_communicator(cls, communicator, on_error, on_changed):
        provider = get_proxy(
            communicator, 'Spotifice.MediaProvider.Proxy', Spotifice.MediaProviderPrx)
        render = get_proxy(
            communicator, 'Spotifice.MediaRender.Proxy', Spotifice.MediaRenderPrx)

        return cls(provider, render, on_error, on_changed)

    # ---- reads

    def get_all_tracks(self, on_result):
        self.deliver(self.provider.get_all_tracksAsync(), "get_all_tracks", on_result)

    def follow(self, on_status, on_track):
        """Ask the render where it is: with get_status() when it has it, and with
        the plain get_current_track() of the v0 interface when it does not."""
        if self.playback_status is False:
            self.get_current_track(on_track)
        else:
            self.get_status(on_status)

    def get_status(self, on_result):
        def found(status):
            self.playback_status = True  # only v1 answers this one
            on_result(status)

        self.deliver(self.render.get_statusAsync(), "get_status", found)

    def get_current_track(self, on_result):
        self.deliver(self.render.get_current_trackAsync(), "get_current_track", on_result)

    def forget_interface(self):
        "The render is gone; the next one may implement another version"
        self.playback_status = None

    def bind_provider(self, on_result):
        self.deliver(self.render.bind_media_providerAsync(self.provider),
                     "bind_media_provider", on_result)

    # ---- writes

    def load_track(self, track_id):
        self.deliver(self.render.load_trackAsync(track_id), "load_track", self.changed)

    def play(self):
        self.deliver(self.render.playAsync(), "play", self.changed)

    def pause(self):
        self.deliver(self.render.pauseAsync(), "pause", self.changed)

    def stop(self):
        self.deliver(self.render.stopAsync(), "stop", self.changed)

    def previous(self):
        self.deliver(self.render.previousAsync(), "previous", self.changed)

    def set_repeat(self, enabled):
        self.deliver(self.render.set_repeatAsync(enabled), "set_repeat", self.changed)

    # ---- delivery

    def changed(self, _result):
        "A write says nothing but that the render state may have changed"
        self.on_changed()

    def deliver(self, future, action, on_result):
        def done(future):
            try:
                result = future.result()
            except Exception as e:
                if isinstance(e, Ice.OperationNotExistException):
                    self.playback_status = False  # a v0 render lacks this operation

                self.in_gtk_thread(self.on_error, action, e)
            else:
                self.in_gtk_thread(on_result, result)

        future.add_done_callback(done)

    @staticmethod
    def in_gtk_thread(callback, *args):
        "Ice runs the done callbacks in its own threads, where GTK is off limits"
        def apply():
            callback(*args)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(apply)


class SpotificeControlWindow(Gtk.ApplicationWindow):
    def __init__(self, app, communicator):
        super().__init__(application=app, title=WINDOW_TITLE)
        self.set_default_size(WINDOW_WIDTH, -1)  # as tall as its contents need

        self.communicator = communicator
        self.client = self.create_client()

        self.track_ids = []
        self.updating_ui = False   # ignore signals for changes we make ourselves
        self.status_pending = False  # a get_status() reply is on its way
        self.error_until = 0         # monotonic time while an error holds the status bar
        self.ready = False           # the render is bound and the tracks are listed

        self.load_css()
        self.create_ui()
        self.update_status(waiting_for(MEDIA_RENDER))  # the first call goes to it
        self.poll_status()  # without waiting for the first tick

    def create_client(self):
        try:
            return SpotificeClient.from_communicator(
                self.communicator, on_error=self.on_call_failed, on_changed=self.refresh_status)
        except Exception as e:  # a configuration problem, not an absent server
            logger.error(f"Error reading the proxies: {describe_error(e)}")
            sys.exit(1)

    # ---- UI ----------------------------------------------------------

    @staticmethod
    def load_css():
        provider = Gtk.CssProvider()
        provider.load_from_path(str(STYLE))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def create_ui(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(box, f'set_margin_{side}')(15)

        # track list: a click, or Enter, loads the track into the render
        self.track_list = Gtk.ListBox()
        self.track_list.connect("row-activated", self.on_track_activated)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_min_content_height(LIST_ROWS * ROW_HEIGHT)
        self.scroller.set_has_frame(True)
        self.scroller.set_vexpand(True)
        self.scroller.set_child(self.track_list)

        # playback controls
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_halign(Gtk.Align.CENTER)
        controls.set_homogeneous(True)
        self.previous_button = self._button(
            "media-skip-backward", "Previous", lambda _b: self.client.previous())
        self.play_button = self._button(
            "media-playback-start", "Play", lambda _b: self.client.play())
        self.pause_button = self._button(
            "media-playback-pause", "Pause", lambda _b: self.client.pause())
        self.stop_button = self._button(
            "media-playback-stop", "Stop", lambda _b: self.client.stop())
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
        statusbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        statusbar.add_css_class("statusbar")
        statusbar.set_margin_start(10)
        statusbar.set_margin_end(10)
        statusbar.set_margin_top(5)
        statusbar.set_margin_bottom(5)
        self.status_icon = Gtk.Image()
        self.status_label = Gtk.Label(label=READY.text, xalign=0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_max_width_chars(TITLE_WIDTH)  # a long error must not widen the window
        self.status_label.set_hexpand(True)
        statusbar.append(self.status_icon)
        statusbar.append(self.status_label)

        for widget in (self.scroller, controls, self.track_label, statusbar):
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

    def update_status(self, state):
        "Routine status, which never hides a recent error"
        if GLib.get_monotonic_time() < self.error_until:
            return

        self.set_status(state.text, state.icon, error=False)

    def show_error(self, message):
        "Error status, held for a while so that the polling cannot hide it"
        self.error_until = GLib.get_monotonic_time() + ERROR_HOLD_MS * 1000
        self.set_status(message, ERROR_ICON, error=True)

    def set_status(self, message, icon_name, error):
        self.status_label.set_text(message)
        self.status_label.set_tooltip_text(message)  # the whole text, if it is cut
        self.status_icon.set_from_icon_name(icon_name)
        for widget in (self.status_label, self.status_icon):
            set_css_class(widget, "status-error", error)

    def update_button_states(self, state):
        "Highlight the button matching the current playback state"
        for candidate, button in self.state_buttons.items():
            set_css_class(button, STATES[candidate].css_class, candidate == state)

    def update_repeat_style(self):
        set_css_class(self.repeat_button, REPEAT_CSS_CLASS, self.repeat_button.get_active())

    def set_track_title(self, title):
        "The current track, shown in the window and in its title bar"
        if title == self.track_label.get_text():
            return

        self.track_label.set_text(title)
        self.track_label.set_tooltip_text(title)
        self.set_title(WINDOW_TITLE if title == NO_TRACK else f"{title} — Spotifice")

    # ---- outcome of the client calls ---------------------------------

    def on_call_failed(self, action, error):
        self.status_pending = False

        if isinstance(error, Ice.OperationNotExistException):
            # the render is there, it just does not implement this operation
            logger.info(f"The render has no {action}(): it implements the v0 interface")
            if action == "get_status":
                self.refresh_status()  # ask again, the way a v0 render understands
            return

        if isinstance(error, Ice.LocalException):
            self.ready = False  # the render is gone; the next one may be a new process
            self.client.forget_interface()

        server = OWN_CALLS.get(action)
        if server:
            # the GUI's own calls do not complain: they wait for the server they
            # need, never bury the error of what the user pressed, and log why
            if isinstance(error, Spotifice.BadReference):
                server = MEDIA_PROVIDER  # the render answered: it cannot reach it

            logger.info(f"Waiting for the {server}: {action}() {describe_error(error)}")
            self.update_status(waiting_for(server))
            return

        self.show_error(f"Error in {action}(): {describe_error(error)}")

    # ---- render state ------------------------------------------------

    def load_tracks(self):
        self.client.get_all_tracks(self.apply_tracks)

    def apply_tracks(self, tracks):
        for track in tracks:
            label = Gtk.Label(label=track.title, xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_tooltip_text(track.title)
            self.track_list.append(label)
            self.track_ids.append(track.id)

        if tracks:
            _minimum, natural = self.track_list.get_row_at_index(0).get_preferred_size()
            self.scroller.set_min_content_height(LIST_ROWS * natural.height)

        if not tracks:
            self.show_error("The provider has no tracks")
            return

        self.ready = True
        self.load_first_track()

    def load_first_track(self, index=0):
        """Make sure the render has a track loaded, without disturbing the current
        one. get_current_track() is asked because both interfaces have it."""
        def on_track(track):
            if track is None:
                self.client.load_track(self.track_ids[index])  # its reply refreshes

        self.client.get_current_track(on_track)

    def bind_provider(self):
        """The render has answered, so it is running, but it may be a fresh process
        with no provider bound. Every poll retries until the binding succeeds,
        which also waits for a provider that is not up yet."""
        row = self.track_list.get_selected_row()
        index = row.get_index() if row else 0

        self.client.bind_provider(lambda _result: self.on_bound(index))

    def on_bound(self, index):
        if not self.track_ids:
            self.load_tracks()  # only ready once they are listed
            return

        self.ready = True
        self.load_first_track(index)

    def refresh_status(self):
        "Ask the render for its state; the UI follows when the reply arrives"
        if self.status_pending:
            return

        self.status_pending = True
        self.client.follow(self.apply_status, self.apply_track)

    def apply_status(self, status):
        "The render has a playback status, so it implements the v1 interface"
        self.status_pending = False
        if not self.ready:
            self.bind_provider()

        self.show_controls(full=True)
        self.show_track(status.current_track)
        self.update_button_states(status.state)

        with self.ui_update():
            self.repeat_button.set_active(bool(status.is_repeating))
            self.update_repeat_style()

        self.update_status(STATES.get(status.state, READY))

    def apply_track(self, track):
        "The v0 interface only tells which track is loaded, not what the player does"
        self.status_pending = False
        if not self.ready:
            self.bind_provider()

        self.show_controls(full=False)
        self.show_track(track)
        self.update_button_states(None)  # there is no state to highlight

        self.update_status(BASIC)

    def show_track(self, track):
        self.set_track_title(track.title if track and track.title else NO_TRACK)
        self.select_track(track.id if track else None)

    def show_controls(self, full):
        "The v0 interface has no pause(), previous() nor set_repeat()"
        for button in (self.pause_button, self.previous_button, self.repeat_button):
            button.set_visible(full)

    def select_track(self, track_id):
        "Mark the track the render has loaded, scrolling it into sight"
        index = self.track_ids.index(track_id) if track_id in self.track_ids else -1
        row = self.track_list.get_row_at_index(index) if index >= 0 else None

        self.track_list.select_row(row)
        if row:
            self.scroll_to_row(row)

    def scroll_to_row(self, row):
        "GtkListBox has no scroll_to, so the scrollbar is moved by hand"
        allocation = row.get_allocation()
        adjustment = self.scroller.get_vadjustment()
        top, visible = adjustment.get_value(), adjustment.get_page_size()

        if allocation.y < top or allocation.y + allocation.height > top + visible:
            adjustment.set_value(allocation.y)

    def poll_status(self):
        "Follow the render while it is there, and retry less often while it is not"
        self.refresh_status()
        GLib.timeout_add(POLL_MS if self.ready else RETRY_MS, self.poll_status)

        return GLib.SOURCE_REMOVE

    # ---- actions -----------------------------------------------------

    def on_track_activated(self, _list, row):
        self.client.load_track(self.track_ids[row.get_index()])

    def on_repeat(self, button):
        if self.updating_ui:
            return

        self.update_repeat_style()
        self.client.set_repeat(button.get_active())


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
