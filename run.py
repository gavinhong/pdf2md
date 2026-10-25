"""Development launcher; packaged applications use the same entry point."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

if __name__ == "__main__":
    from pdf2md.entry import main

    main()
