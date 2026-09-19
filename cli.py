"""Entry point for the one-file builds (PyInstaller cannot start a package's __main__ directly)."""
from donutcoin.__main__ import main

if __name__ == "__main__":
    main()
