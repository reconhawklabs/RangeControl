"""Entry point for both `python -m rangecontrol` and the frozen binary."""

from rangecontrol.main import main

if __name__ == "__main__":
    raise SystemExit(main())
