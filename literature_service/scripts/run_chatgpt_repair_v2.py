"""CLI for the isolated repair experiment."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == '__main__':
    from nutri_literature.chatgpt_repair_runner import main
    main()
