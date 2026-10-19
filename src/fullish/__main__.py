"""Run the Fullish foundation with the NoneBot/Uvicorn signal lifecycle."""

import nonebot

from fullish.app import create_app


def main() -> None:
    create_app()
    nonebot.run()


if __name__ == "__main__":
    main()
