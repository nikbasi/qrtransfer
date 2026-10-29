"""run.py asks the questions and turns the answers into the right encoder command."""
import builtins
import io

import pytest

import run


@pytest.fixture
def drive(monkeypatch):
    """Run run.main() with scripted answers; returns what it would have passed to qrencode.main."""
    calls = {}

    def go(answers, picked=None):
        inputs = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(inputs))
        monkeypatch.setattr(run, "pick_file", lambda: picked)
        import qrencode

        def fake_main(argv):
            import sys
            calls["argv"] = argv
            calls["stdin"] = sys.stdin.buffer.read() if "-" in argv else None
        monkeypatch.setattr(qrencode, "main", fake_main)
        run.main()
        return calls
    return go


def test_defaults_are_the_fast_live_layout(drive, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"x" * 50_000)
    # file, no password, default speed, no colour
    c = drive(["1", "", "", ""], picked=str(f))
    assert c["argv"] == [str(f), "--live", "--cols", "4", "--rows", "3"]


def test_password_speed_and_colour_choices(drive, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    c = drive(["1", "y", "2", "y"], picked=str(f))
    assert c["argv"] == [str(f), "--live", "--cols", "3", "--rows", "2", "-p", "--color"]


def test_typed_message_goes_through_stdin_not_a_file(drive):
    c = drive(["2", "hello", "wörld", "", "n", "", "n"])
    assert c["argv"][:1] == ["-"] and "--name" in c["argv"] and "message.txt" in c["argv"]
    assert c["stdin"] == "hello\nwörld\n".encode()


def test_no_dialog_falls_back_to_a_typed_or_dragged_path(drive, tmp_path):
    f = tmp_path / "my file.txt"
    f.write_text("hi")
    # dialog unavailable; the path arrives quoted, as a terminal pastes it; then the other questions
    c = drive(["1", f"'{f}'", "", "", ""], picked=None)
    assert c["argv"][0] == str(f)


def test_clean_path():
    assert run.clean_path("'/a b/c.txt'") == "/a b/c.txt"
    assert run.clean_path('"/a b/c.txt"') == "/a b/c.txt"
    assert run.clean_path("/a\\ b/c.txt") == "/a b/c.txt"
    assert run.clean_path("~/x").endswith("/x") and not run.clean_path("~/x").startswith("~")


def test_estimate_is_readable():
    assert "seconds" in run.estimate(5_000, 4, 3)
    assert "minutes" in run.estimate(1_000_000, 4, 3)


def test_required_packages_present():
    run.ensure_requirements()  # would prompt to install if something were missing
