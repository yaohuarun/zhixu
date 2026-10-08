from pathlib import Path

assert Path("/data/models/.ready").is_file()
path = Path("/data/sources/.rag-acceptance-mount-check")
assert not path.exists(), "Acceptance probe path already exists"
try:
    path.write_text("check")
except OSError as exc:
    assert exc.errno == 30, exc
else:
    path.unlink()
    raise AssertionError("Source mount is writable")
print("Read-only source mount and retained OCR assets: passed")
