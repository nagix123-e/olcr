"""Compatibility metadata for Python environments whose pip predates PEP 660."""

from setuptools import setup

setup(
    name="olcr",
    version="0.5.0",
    description="Ollama Local Cognitive Runtime terminal client",
    license="Apache-2.0",
    python_requires=">=3.12",
    package_dir={"": "backend"},
    packages=["olcr_api", "olcr_cli"],
    package_data={"olcr_api": ["coding_verification_runner.cjs"]},
    install_requires=[
        "fastapi==0.116.1", "httpx==0.28.1", "pydantic==2.11.7",
        "uvicorn==0.35.0", "torch==2.8.0", "torchaudio==2.8.0", "transformers==4.57.6",
        "liquid-audio>=1.0,<2", "huggingface_hub>=0.20.0",
    ],
    entry_points={"console_scripts": ["olcr=olcr_cli.main:main"]},
)
