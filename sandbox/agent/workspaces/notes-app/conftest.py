import pathlib
import sys

# the package lives in src/ (src layout); tests import it as `notes`
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))
