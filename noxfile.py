import nox

python_versions = ["3.10", "3.11", "3.12"]

@nox.session(python=python_versions)
def tests(session: nox.Session) -> None:
    """Run the test suite."""
    session.install("poetry")
    session.run("poetry", "install")
    session.run("poetry", "run", "pytest", "--benchmark-disable", "-x", "-v")
