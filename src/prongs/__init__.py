"""PYTEST_DONT_REWRITE: `prongs run` calls pytest.main with this package
already imported, and from a wheel pytest marks the pytest11 plugin's package
for assertion rewriting; its "already imported" warning is an error under
filterwarnings=error."""

__version__ = "1.0.0"
