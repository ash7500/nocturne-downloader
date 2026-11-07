"""Environment for the whole suite. Must run before any test module imports app.main."""
import os
import tempfile

os.environ.setdefault("ACCESS_KEY", "test-access-key-0123456789")
os.environ.setdefault("DOWNLOADS_DIR", tempfile.mkdtemp(prefix="nocturne_tests_"))
os.environ.setdefault("ALLOWED_ORIGINS", "https://nocturne.example.app")
os.environ["ALLOW_PRIVATE_URLS"] = "1"  # tests download from a local HTTP server; the SSRF tests switch it off
