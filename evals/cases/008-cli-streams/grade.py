import subprocess
import sys


def test_cli() -> None:
    for args, expected in [
        (["hello"], "hello\n"),
        (["--upper", "hello"], "HELLO\n"),
        (["--", "-x"], "-x\n"),
    ]:
        result = subprocess.run([sys.executable, "app.py", *args], capture_output=True, text=True)
        assert result.returncode == 0 and result.stdout == expected and not result.stderr
    for args in [[], ["--unknown"]]:
        result = subprocess.run([sys.executable, "app.py", *args], capture_output=True, text=True)
        assert result.returncode == 2 and result.stderr and not result.stdout
