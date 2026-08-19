"""Guard that every name in ``pagr.__all__`` actually resolves on the package.

Catches the case where a symbol is added to / removed from ``__all__`` without
updating the corresponding import in ``pagr/__init__.py``.
"""

import pagr


def test_all_exports_are_importable():
    # Every name in __all__ actually resolves on the package.
    for name in pagr.__all__:
        assert hasattr(pagr, name), f"pagr.__all__ names {name!r} but it is not defined"
