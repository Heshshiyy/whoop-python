"""Setup script for whoop-desktop."""

from setuptools import setup, find_packages

setup(
    name="whoop-desktop",
    version="0.1.0",
    description="Python WHOOP app — BLE client, storage, analytics, and CLI",
    author="WHOOP Desktop Project",
    python_requires=">=3.10",
    packages=find_packages(include=["whoop", "whoop.*"]),
    install_requires=[
        "bleak>=0.21.0",
    ],
    extras_require={
        "rich": ["rich>=13.0.0"],
    },
    entry_points={
        "console_scripts": [
            "whoop=whoop.cli:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Operating System :: OS Independent",
    ],
)
