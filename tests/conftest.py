import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def demo_store(tmp_path):
    """A store filled by replaying a synthetic tape through the real capturer.

    Returns ``(store, tape)``.  The tape is labelled synthetic because it is;
    what it proves is that capture, store, audit and derive agree, not that the
    market looks like this.
    """
    from monster.data.capture import Capturer, FileTransport
    from monster.data.store import Store
    from monster.data.synthetic import write_demo_tape

    tape = write_demo_tape(tmp_path / "tape", samples=40, interval_s=300.0)
    store = Store(tmp_path / "data")
    capturer = Capturer(store, FileTransport(tape.root))
    for index in range(tape.samples):
        report = capturer.capture(at=tape.base_ts + index * tape.interval_s)
        assert report.ok, report.describe()
    return store, tape

