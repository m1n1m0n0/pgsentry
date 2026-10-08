"""pgsentry: PostgreSQL health and Kubernetes security-posture checks."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pgsentry")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
