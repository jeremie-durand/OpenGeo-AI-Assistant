"""Smoke test: every key dependency must be importable."""

import importlib

import pytest

PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("aiohttp", "aiohttp"),
    ("httpx", "httpx"),
    ("requests", "requests"),
    ("openai", "openai"),
    ("anthropic", "anthropic"),
    ("semantic-kernel", "semantic_kernel"),
    ("pystac-client", "pystac_client"),
    ("stac-pydantic", "stac_pydantic"),
    ("geopy", "geopy"),
    ("planetary-computer", "planetary_computer"),
    ("numpy", "numpy"),
    ("xarray", "xarray"),
    ("fsspec", "fsspec"),
    ("cachetools", "cachetools"),
    ("python-dateutil", "dateutil"),
    ("limits", "limits"),
    ("rasterio", "rasterio"),
]


@pytest.mark.parametrize(
    "install_name,import_name", PACKAGES, ids=[p[0] for p in PACKAGES]
)
def test_package_importable(install_name: str, import_name: str) -> None:
    try:
        importlib.import_module(import_name)
    except ImportError as exc:
        pytest.fail(
            f"Cannot import '{import_name}' (installed as '{install_name}'): {exc}"
        )
