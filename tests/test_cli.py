import sys

import pytest

from noah_ark.cli import build_parser, main


def test_parser_builds_and_parses_common_commands():
    parser = build_parser()
    assert parser.parse_args(["up", "-d", "api"]).detach is True
    assert parser.parse_args(["logs", "-f", "-n", "5"]).follow is True
    assert parser.parse_args(["down", "-f", "x/ark.yaml"]).file == "x/ark.yaml"


def test_init_scans_services(tmp_path, monkeypatch, capsys):
    for name in ("api_a", "api_b"):
        (tmp_path / "services" / name).mkdir(parents=True)
        (tmp_path / "services" / name / "main.py").write_text("app = None\n")
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    text = (tmp_path / "ark.yaml").read_text()
    assert "api-a:" in text and "--port 8001" in text and "--port 8002" in text
    assert main(["validate"]) == 0
    assert "start order" in capsys.readouterr().out


def test_up_then_logs_and_ps(tmp_path, monkeypatch, capsys):
    (tmp_path / "ark.yaml").write_text(
        f'services:\n  hi: {{cmd: ["{sys.executable}", "-c", "print(42)"], restart: "no"}}\n'
    )
    monkeypatch.chdir(tmp_path)
    assert main(["up", "--no-color"]) == 0
    capsys.readouterr()
    assert main(["logs", "--no-color"]) == 0
    assert "hi  | 42" in capsys.readouterr().out
    assert main(["ps"]) == 0
    assert "not running" in capsys.readouterr().out


def test_config_errors_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["ps"]) == 2
    assert "no ark.yaml" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--version"]])
def test_version(argv, capsys):
    with pytest.raises(SystemExit):
        main(argv)
    assert "noah-ark" in capsys.readouterr().out


def test_logs_grep(tmp_path, monkeypatch, capsys):
    code = "print('alpha ok'); print('beta ERROR boom'); print('gamma error two')"
    (tmp_path / "ark.yaml").write_text(
        f'services:\n  hi: {{cmd: ["{sys.executable}", "-c", "{code}"], restart: "no"}}\n'
    )
    monkeypatch.chdir(tmp_path)
    assert main(["up", "--no-color", "--no-console"]) == 0
    assert main(["logs", "--no-color", "--grep", "ERROR"]) == 0
    out = capsys.readouterr().out
    assert "beta ERROR boom" in out and "gamma" not in out and "alpha" not in out
    assert main(["logs", "--no-color", "-g", "error", "-i"]) == 0
    out = capsys.readouterr().out
    assert "beta ERROR boom" in out and "gamma error two" in out and "alpha" not in out
    assert main(["logs", "-g", "("]) == 2


def test_profile_flag_parses():
    args = build_parser().parse_args(["up", "--profile", "a", "-p", "b"])
    assert args.profile == ["a", "b"]
