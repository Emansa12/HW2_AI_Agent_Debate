"""Allow `python -m debate` to dispatch to `debate.main`."""

from debate.main import main

if __name__ == "__main__":
    raise SystemExit(main())
