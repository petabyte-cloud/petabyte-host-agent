"""
Setup script for Petabyte Agent
"""
from setuptools import setup, find_packages

setup(
    name="petabyte-agent",
    version="1.0.0",
    description="Petabyte Agent - Decentralized Resource Sharing Agent",
    author="Petabyte Team",
    packages=find_packages(),
    install_requires=[
        "httpx>=0.27.0",
        "nbformat>=5.9.0",
        "nbclient>=0.9.0",
        "flask>=3.0.0",
        "requests>=2.31.0",
        # Imported at host runtime but previously missing here — a clean `pip install`
        # of the package would ImportError: crypto.py needs cryptography (Ed25519 result +
        # attestation signing), main.py calls load_dotenv (python-dotenv).
        "cryptography>=42.0.0",
        "python-dotenv>=1.0.0",
    ],
    entry_points={
        "console_scripts": [
            "petabyte-agent=main:main",
        ],
    },
    include_package_data=True,
    package_data={
        "": ["templates/*.html"],
    },
)

