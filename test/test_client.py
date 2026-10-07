"Tests for the parts of the GUI that need neither a display nor a server"

import Ice
import pytest
from gi.repository import GLib

from media_control_gui import (
    MEDIA_PROVIDER,
    MEDIA_RENDER,
    OWN_CALLS,
    READY,
    REPEAT_CSS_CLASS,
    STATES,
    STYLE,
    Spotifice,
    SpotificeClient,
    describe_error,
    waiting_for,
)


class FakeFuture:
    "An Ice future that is already done"

    def __init__(self, result=None, error=None):
        self.value = result
        self.error = error

    def add_done_callback(self, callback):
        callback(self)

    def result(self):
        if self.error:
            raise self.error

        return self.value


class FakeProxy:
    "Records the invoked operations instead of talking to a server"

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error

    def __getattr__(self, operation):
        def invoke(*args):
            self.calls.append((operation, args))
            return FakeFuture(self.result, self.error)

        return invoke


def flush():
    "Run the pending idle callbacks, as the GTK main loop would"
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(may_block=False)


@pytest.fixture
def outcome():
    return {'results': [], 'errors': [], 'changes': 0}


def make_client(provider=None, render=None, outcome=None):
    def on_error(action, error):
        outcome['errors'].append((action, error))

    def on_changed():
        outcome['changes'] += 1

    return SpotificeClient(provider or FakeProxy(), render or FakeProxy(), on_error, on_changed)


def test_a_read_is_delivered_in_the_gtk_thread(outcome):
    provider = FakeProxy(result=['a track'])
    client = make_client(provider=provider, outcome=outcome)

    client.get_all_tracks(outcome['results'].append)
    assert outcome['results'] == []  # not before the main loop runs

    flush()
    assert outcome['results'] == [['a track']]
    assert provider.calls == [('get_all_tracksAsync', ())]


def test_a_write_reports_a_change(outcome):
    render = FakeProxy()
    client = make_client(render=render, outcome=outcome)

    client.load_track('t1')
    flush()

    assert render.calls == [('load_trackAsync', ('t1',))]
    assert outcome['changes'] == 1


def test_a_failure_names_the_action(outcome):
    error = Spotifice.TrackError(item='t9', reason='Track not found')
    client = make_client(render=FakeProxy(error=error), outcome=outcome)

    client.play()
    flush()

    assert outcome['errors'] == [('play', error)]
    assert outcome['changes'] == 0


@pytest.mark.parametrize('method, args, operation', [
    ('play', (), 'playAsync'),
    ('pause', (), 'pauseAsync'),
    ('stop', (), 'stopAsync'),
    ('previous', (), 'previousAsync'),
    ('set_repeat', (True,), 'set_repeatAsync'),
    ('load_track', ('t1',), 'load_trackAsync'),
    ('get_status', (lambda _status: None,), 'get_statusAsync'),
])
def test_operations_reach_the_render(method, args, operation, outcome):
    "The names have to match those generated from spotifice_v1.ice"
    render = FakeProxy()
    client = make_client(render=render, outcome=outcome)

    getattr(client, method)(*args)
    flush()

    invoked, invoked_args = render.calls[0]
    assert invoked == operation
    assert invoked_args == tuple(a for a in args if not callable(a))


def test_binding_sends_the_provider(outcome):
    provider, render = FakeProxy(), FakeProxy()
    client = make_client(provider=provider, render=render, outcome=outcome)

    client.bind_provider(outcome['results'].append)
    flush()

    assert render.calls == [('bind_media_providerAsync', (provider,))]


def test_spotifice_errors_are_described_in_one_line():
    error = Spotifice.TrackError(item='t9', reason='Track not found')
    assert describe_error(error) == 'TrackError: Track not found (t9)'


def test_ice_errors_are_described_in_one_line():
    "Ice spreads its messages over several lines, which the status bar cannot show"
    assert describe_error(RuntimeError('broken\n  pipe')) == 'broken pipe'
    assert describe_error(RuntimeError()) == 'RuntimeError'


def test_every_playback_state_is_presented():
    "A state missing from the table would leave the UI without text, icon or colour"
    for state in Spotifice.PlaybackState._enumerators.values():
        assert state in STATES, f'{state} is not in STATES'
        assert STATES[state].text and STATES[state].icon


def test_the_stylesheet_covers_every_highlight():
    "A css class with no rule in style.css would simply not show"
    css = STYLE.read_text()

    for state in STATES.values():
        assert f'button.{state.css_class}' in css

    assert f'button.{REPEAT_CSS_CLASS}' in css
    assert 'label.status-error' in css
    assert READY.css_class == ''  # no highlight before the render answers


def test_the_waiting_status_names_the_server():
    assert waiting_for(MEDIA_RENDER).text == 'Waiting for the media render…'
    assert waiting_for(MEDIA_PROVIDER).icon  # an icon of its own, not an error one


def test_the_calls_the_gui_makes_by_itself_know_their_server():
    assert OWN_CALLS == {
        'get_status': MEDIA_RENDER,
        'get_current_track': MEDIA_RENDER,
        'bind_media_provider': MEDIA_RENDER,
        'get_all_tracks': MEDIA_PROVIDER,
    }


def test_a_render_that_answers_get_status_implements_v1(outcome):
    client = make_client(render=FakeProxy(result='a status'), outcome=outcome)

    client.follow(outcome['results'].append, outcome['errors'].append)
    flush()

    assert client.playback_status is True
    assert outcome['results'] == ['a status']


def test_a_render_without_get_status_implements_v0(outcome):
    "v0 and v1 share their type ids, so only an invocation tells them apart"
    render = FakeProxy(error=Ice.OperationNotExistException())
    client = make_client(render=render, outcome=outcome)

    client.follow(outcome['results'].append, outcome['results'].append)
    flush()

    assert render.calls == [('get_statusAsync', ())]  # the probe
    assert client.playback_status is False

    client.follow(outcome['results'].append, outcome['results'].append)
    flush()

    assert render.calls[-1] == ('get_current_trackAsync', ())  # what v0 does have


def test_the_interface_is_probed_again_after_losing_the_render(outcome):
    client = make_client(render=FakeProxy(error=Ice.OperationNotExistException()), outcome=outcome)

    client.follow(outcome['results'].append, outcome['results'].append)
    flush()
    assert client.playback_status is False

    client.forget_interface()
    assert client.playback_status is None
