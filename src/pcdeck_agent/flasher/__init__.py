"""Deck firmware updates over USB: find the deck, fetch its latest release, flash it with esptool.

- ``boards``   the deck models, their chip, GitHub repository and image names
- ``probe``    USB serial ports that look like a deck, and what the deck's console says it is
- ``releases`` the latest firmware release of a model on GitHub, downloaded and verified
- ``flash``    one update: connect (esptool), check the chip, download, write, restart

Nothing here touches the network or a port until asked; the GUI runs it off the UI thread.
"""
