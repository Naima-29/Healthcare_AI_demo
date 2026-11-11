"""Generate dev keypairs for every seat in seats/manifests: `python scripts/gen_keys.py [keys_dir]`.
Never reuse these in production, and never commit the .key files (keys/ is git-ignored)."""

import sys
from pathlib import Path

from kcd_core.keys import generate_keypair

root = Path(__file__).resolve().parents[1]
out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "keys"
for manifest in sorted((root / "seats" / "manifests").glob("*.manifest.json")):
    seat = manifest.name.removesuffix(".manifest.json")
    try:
        generate_keypair(out, seat)
        print("generated", seat)
    except FileExistsError:
        print("kept existing", seat)
