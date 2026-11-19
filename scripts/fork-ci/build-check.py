import os
import plistlib
import struct
from pathlib import Path

app = Path("Paseo.app").resolve()
contents = app / "Contents"
with (contents / "Info.plist").open("rb") as stream:
    info = plistlib.load(stream)
assert info["CFBundleShortVersionString"] == "0.16.2"

for executable in [
    contents / "MacOS" / info["CFBundleExecutable"],
    contents / "Frameworks/Paseo Helper.app/Contents/MacOS/Paseo Helper",
]:
    with executable.open("rb") as stream:
        assert struct.unpack("<II", stream.read(8)) == (0xFEEDFACF, 0x0100000C), executable
    assert os.access(executable, os.X_OK), executable

for link in app.rglob("*"):
    if link.is_symlink():
        assert not link.readlink().is_absolute(), link
        assert link.resolve(strict=True).is_relative_to(app), link
print("Verified ARM64 app and helper, version", info["CFBundleShortVersionString"], "and internal symlinks")
