"""PyInstaller entry point for PCControlDeck.exe."""

from pcdeck_agent.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
