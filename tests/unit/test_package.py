import investment_analyst


def test_package_importable():
    assert investment_analyst is not None


def test_version():
    assert investment_analyst.__version__ == "0.1.0"
