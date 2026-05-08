"""`python -m artnet_blaze` entry point."""

import sys

from .main import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
