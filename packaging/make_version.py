"""Write a PyInstaller version resource so Windows shows the version in the .exe properties."""

import sys

from pcdeck_agent import __version__

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'PC Control Deck'),
      StringStruct('FileDescription', 'PC Control Deck'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'PCControlDeck'),
      StringStruct('OriginalFilename', 'PCControlDeck.exe'),
      StringStruct('ProductName', 'PC Control Deck'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""

if __name__ == "__main__":
    parts = [int(p) for p in __version__.split(".")[:3]] + [0]
    while len(parts) < 4:
        parts.append(0)
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        f.write(TEMPLATE.format(t=tuple(parts[:4]), v=__version__))
    print(f"wrote {sys.argv[1]} ({__version__})")
