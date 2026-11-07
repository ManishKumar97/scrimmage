"""Acme's bot as a script: python bot.py {question}. It prints its answer, then starts a child."""

import subprocess
import sys

from acme.agent import answer


def main() -> None:
    print(answer(sys.argv[1]), flush=True)
    subprocess.run([sys.executable, "report.py", sys.argv[1]], check=False)


if __name__ == "__main__":
    main()
