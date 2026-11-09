"""SDR Toolkit: decoders for amateur and utility radio data modes, fed by any audio on this PC."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sdr-toolkit")
except PackageNotFoundError:  # running from a checkout without installing
    __version__ = "0.0.0+checkout"
APP_ID = "io.github.ssjrocks.SDRToolkit"
APP_NAME = "SDR Toolkit"
REPO = "ssjrocks/sdr-toolkit"
