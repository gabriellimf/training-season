from training_season import __version__


def test_package_version_is_importable() -> None:
    assert isinstance(__version__, str)
    assert __version__ != ""
