"""GUI package."""

__all__ = ["launch_gui"]


def launch_gui(*args, **kwargs):
    from .app import launch_gui as _launch_gui

    return _launch_gui(*args, **kwargs)
