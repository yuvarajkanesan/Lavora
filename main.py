"""PyInstaller entry point (the package itself uses relative imports)."""
from lavora.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
