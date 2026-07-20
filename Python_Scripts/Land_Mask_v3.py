#!/usr/bin/env python3
"""
ECOSTRESS LST Land Masking 

This script applies a land polygon shapefile mask to every GeoTIFF in the same
folder as this script. Land pixels are set to a fill value.

Put this script in the folder that contains:
  1. Your LST GeoTIFF files ending in .tif or .tiff
  2. Your land shapefile from the previous land-polygon workflow

The script automatically:
  - Finds the working folder from the script location
  - Finds the land shapefile in that folder
  - Finds all .tif and .tiff files in that folder
  - Applies the land mask to each TIFF

Output behavior:
  - append:
      Saves masked files into a new folder called land_masked.
      Original TIFFs are kept unchanged.
      Output filenames are unchanged.
  - overwrite:
      Replaces the original TIFFs in the working folder.
      Use this only after you have backed up your originals.

Required Python packages:
  - rasterio
  - geopandas
  - shapely
  - numpy
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.mask import mask


# =============================================================================
# USER SETTINGS
# =============================================================================

# Choose one:
#   "append"    -> write masked copies to ./land_masked/ with unchanged filenames
#   "overwrite" -> overwrite the original TIFFs in the working folder
OUTPUT_MODE = "overwrite"

# Value assigned to land pixels after masking.
# For most float LST products, -9999.0 is safe and easy to recognize as NoData.
FILL_VALUE = -9999.0

# Name of the output folder used only when OUTPUT_MODE = "append".
APPEND_OUTPUT_FOLDER_NAME = "land_masked"

# If True, preserve the source GeoTIFF's compression/profile settings when present.
# Keep this True unless you have a specific reason to force the defaults below.
PRESERVE_SOURCE_PROFILE = True

# Default compression settings used when the source TIFF has no compression.
# These settings keep output files much smaller than uncompressed GeoTIFFs.
DEFAULT_COMPRESS = "DEFLATE"
DEFAULT_ZLEVEL = 6
DEFAULT_TILE_SIZE = 256

# Shapefile selection priority.
# The previous land-polygon script commonly creates these files.
# The detector prefers smoother land masks first, then falls back to rigid.
PREFERRED_LAND_SHAPEFILES = [
    "smoothest.shp",
    "smoother.shp",
    "smooth.shp",
    "rigid.shp",
]

# Shapefiles with these words are treated as AOI/boundary files, not land masks,
# unless they are the only shapefile in the folder.
AOI_NAME_HINTS = [
    "aoi",
    "area_of_interest",
    "areaofinterest",
    "boundary",
    "bounds",
    "study_area",
    "studyarea",
]


# =============================================================================
# DISCOVERY HELPERS
# =============================================================================

def get_working_folder() -> Path:
    """Return the folder where this script lives."""
    try:
        return Path(__file__).resolve().parent
    except NameError:
        # Fallback for interactive environments.
        return Path.cwd().resolve()


def discover_land_shapefile(folder: Path) -> Path:
    """Find the land shapefile to use for masking."""
    shapefiles = sorted(folder.glob("*.shp"))

    if not shapefiles:
        raise FileNotFoundError(
            f"No .shp file found in working folder:\n  {folder}\n\n"
            "Place the land shapefile in the same folder as this script."
        )

    if len(shapefiles) == 1:
        return shapefiles[0]

    by_lower_name = {p.name.lower(): p for p in shapefiles}

    for preferred_name in PREFERRED_LAND_SHAPEFILES:
        match = by_lower_name.get(preferred_name.lower())
        if match is not None:
            return match

    land_named = [p for p in shapefiles if "land" in p.stem.lower()]
    if len(land_named) == 1:
        return land_named[0]

    non_aoi = [
        p for p in shapefiles
        if not any(hint in p.stem.lower() for hint in AOI_NAME_HINTS)
    ]

    if len(non_aoi) == 1:
        return non_aoi[0]

    candidates = non_aoi if len(non_aoi) > 1 else shapefiles
    candidate_list = "\n".join(f"  - {p.name}" for p in candidates)
    raise RuntimeError(
        "Multiple shapefiles were found, and the script could not safely decide "
        "which one is the land mask.\n\n"
        f"Candidate shapefiles:\n{candidate_list}\n\n"
        "Recommended fix: keep only one land shapefile in the folder, or rename "
        "the intended land mask to one of these preferred names:\n"
        "  smoothest.shp, smoother.shp, smooth.shp, or rigid.shp"
    )


def discover_tiff_files(folder: Path) -> list[Path]:
    """Find all top-level TIFF files in the working folder."""
    tiff_files = sorted(
        [
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in {".tif", ".tiff"}
        ]
    )

    if not tiff_files:
        raise FileNotFoundError(
            f"No .tif or .tiff files found in working folder:\n  {folder}\n\n"
            "Place the LST TIFF files in the same folder as this script."
        )

    return tiff_files


# =============================================================================
# MASKING HELPERS
# =============================================================================

def validate_output_mode(mode: str) -> str:
    """Validate and normalize the output mode."""
    mode = mode.strip().lower()
    if mode not in {"append", "overwrite"}:
        raise ValueError(
            f"Invalid OUTPUT_MODE: {OUTPUT_MODE!r}\n\n"
            'Use either OUTPUT_MODE = "append" or OUTPUT_MODE = "overwrite".'
        )
    return mode


def geometries_for_raster_crs(
    land_gdf_original: gpd.GeoDataFrame,
    raster_crs,
    cache: dict[str, list],
) -> list:
    """Return land geometries reprojected to the current raster CRS."""
    if raster_crs is None:
        raise ValueError(
            "A raster has no CRS. Cannot safely align the land shapefile to it."
        )

    raster_crs_key = str(raster_crs)

    if raster_crs_key not in cache:
        if land_gdf_original.crs != raster_crs:
            land_gdf = land_gdf_original.to_crs(raster_crs)
        else:
            land_gdf = land_gdf_original

        geoms = [
            geom for geom in land_gdf.geometry
            if geom is not None and not geom.is_empty
        ]

        if not geoms:
            raise ValueError("The selected land shapefile contains no valid geometries.")

        cache[raster_crs_key] = geoms

    return cache[raster_crs_key]


def _dtype_name(dtype) -> str:
    """Return a rasterio-friendly dtype string."""
    return np.dtype(dtype).name


def _predictor_for_dtype(dtype) -> int | None:
    """Choose a GeoTIFF predictor appropriate for the raster dtype."""
    dtype = np.dtype(dtype)
    if np.issubdtype(dtype, np.floating):
        return 3
    if np.issubdtype(dtype, np.integer) and dtype.itemsize > 1:
        return 2
    return None


def _has_creation_option(profile: dict, key: str) -> bool:
    """Check profile for a creation option regardless of key casing."""
    key_lower = key.lower()
    return any(str(k).lower() == key_lower for k in profile.keys())


def build_output_profile(src, out_image, out_transform) -> dict:
    """Build a compressed output GeoTIFF profile."""
    if PRESERVE_SOURCE_PROFILE:
        profile = src.profile.copy()
    else:
        profile = src.meta.copy()

    height = out_image.shape[1]
    width = out_image.shape[2]
    dtype_name = _dtype_name(out_image.dtype)

    profile.update(
        {
            "driver": "GTiff",
            "height": height,
            "width": width,
            "count": out_image.shape[0],
            "dtype": dtype_name,
            "crs": src.crs,
            "transform": out_transform,
            "nodata": FILL_VALUE,
            "BIGTIFF": "IF_SAFER",
        }
    )

    # If the source profile has no compression, add compression now.
    # This prevents 9 MB source files from turning into huge uncompressed outputs.
    if not _has_creation_option(profile, "compress"):
        profile["compress"] = DEFAULT_COMPRESS
        profile["zlevel"] = DEFAULT_ZLEVEL

    # Add predictor when missing. Predictor helps compression, especially for
    # continuous LST rasters. Do not override a predictor already in the source.
    if not _has_creation_option(profile, "predictor"):
        predictor = _predictor_for_dtype(dtype_name)
        if predictor is not None:
            profile["predictor"] = predictor

    # Use tiling for medium/large rasters if the source did not already define
    # block sizes. Tiled compressed GeoTIFFs are usually smaller and faster.
    has_block_size = (
        _has_creation_option(profile, "blockxsize") or
        _has_creation_option(profile, "blockysize")
    )
    if not has_block_size and width >= DEFAULT_TILE_SIZE and height >= DEFAULT_TILE_SIZE:
        profile["tiled"] = True
        profile["blockxsize"] = DEFAULT_TILE_SIZE
        profile["blockysize"] = DEFAULT_TILE_SIZE

    # If the raster is small and inherited invalid tile settings, remove them.
    # This avoids GDAL errors for very small rasters.
    if width < 16 or height < 16:
        profile.pop("tiled", None)
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)

    return profile


def write_masked_tiff(
    source_path: Path,
    output_path: Path,
    out_image,
    out_profile: dict,
    overwrite_original: bool,
) -> None:
    """Write masked TIFF output.

    For overwrite mode, write to a temporary file first, then atomically replace
    the original. This avoids corrupting the source if writing fails.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if overwrite_original:
        fd, temp_name = tempfile.mkstemp(
            suffix=source_path.suffix,
            prefix=f".{source_path.stem}_landmask_tmp_",
            dir=str(source_path.parent),
        )
        os.close(fd)
        temp_path = Path(temp_name)

        try:
            with rasterio.open(temp_path, "w", **out_profile) as dst:
                dst.write(out_image)

            os.replace(temp_path, source_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    else:
        with rasterio.open(output_path, "w", **out_profile) as dst:
            dst.write(out_image)


def apply_land_mask_to_tiff(
    tif_path: Path,
    land_gdf_original: gpd.GeoDataFrame,
    crs_geometry_cache: dict[str, list],
    output_path: Path,
    overwrite_original: bool,
) -> None:
    """Apply the land mask to one TIFF."""
    with rasterio.open(tif_path) as src:
        land_shapes = geometries_for_raster_crs(
            land_gdf_original=land_gdf_original,
            raster_crs=src.crs,
            cache=crs_geometry_cache,
        )

        # invert=True masks pixels INSIDE the land polygons.
        out_image, out_transform = mask(
            dataset=src,
            shapes=land_shapes,
            invert=True,
            crop=False,
            nodata=FILL_VALUE,
            filled=True,
        )

        out_profile = build_output_profile(
            src=src,
            out_image=out_image,
            out_transform=out_transform,
        )

    write_masked_tiff(
        source_path=tif_path,
        output_path=output_path,
        out_image=out_image,
        out_profile=out_profile,
        overwrite_original=overwrite_original,
    )


# =============================================================================
# MAIN WORKFLOW
# =============================================================================

def main() -> None:
    print("\n=== ECOSTRESS LST Land Masking - Working Folder Version ===\n")

    output_mode = validate_output_mode(OUTPUT_MODE)
    overwrite_original = output_mode == "overwrite"

    working_folder = get_working_folder()
    print(f"[INFO] Working folder: {working_folder}")

    land_shp = discover_land_shapefile(working_folder)
    print(f"[INFO] Land shapefile: {land_shp.name}")

    tiff_files = discover_tiff_files(working_folder)
    print(f"[INFO] Found {len(tiff_files)} TIFF file(s) to mask.")

    if output_mode == "append":
        output_folder = working_folder / APPEND_OUTPUT_FOLDER_NAME
        print("[INFO] Output mode: append")
        print(f"[INFO] Masked files will be saved to: {output_folder}")
        print("[INFO] Original TIFF files will be kept unchanged.")
    else:
        output_folder = working_folder
        print("[WARNING] Output mode: overwrite")
        print("[WARNING] Original TIFF files in the working folder will be replaced.")
        print("[WARNING] Make sure you have a backup before using overwrite mode.")

    print("\n[INFO] Loading land shapefile...")
    land_gdf_original = gpd.read_file(land_shp)

    if land_gdf_original.empty:
        raise ValueError(f"The selected land shapefile is empty: {land_shp}")

    if land_gdf_original.crs is None:
        raise ValueError(
            f"The selected land shapefile has no CRS: {land_shp}\n"
            "Define its CRS before running this script."
        )

    print(f"[INFO] Land shapefile CRS: {land_gdf_original.crs}")
    print(f"[INFO] Compression fallback: {DEFAULT_COMPRESS}, zlevel={DEFAULT_ZLEVEL}")

    crs_geometry_cache: dict[str, list] = {}

    print("\n[INFO] Applying land mask...")
    success_count = 0
    fail_count = 0

    for i, tif_path in enumerate(tiff_files, start=1):
        output_path = output_folder / tif_path.name
        input_size_mb = tif_path.stat().st_size / (1024 * 1024)

        print(f"\n[{i}/{len(tiff_files)}] Masking: {tif_path.name}")
        print(f"  Input size: {input_size_mb:.2f} MB")

        try:
            apply_land_mask_to_tiff(
                tif_path=tif_path,
                land_gdf_original=land_gdf_original,
                crs_geometry_cache=crs_geometry_cache,
                output_path=output_path,
                overwrite_original=overwrite_original,
            )
            success_count += 1

            final_path = tif_path if overwrite_original else output_path
            output_size_mb = final_path.stat().st_size / (1024 * 1024)

            if overwrite_original:
                print(f"  → Overwrote original file: {tif_path.name}")
            else:
                print(f"  → Saved masked copy: {output_path}")
            print(f"  Output size: {output_size_mb:.2f} MB")

        except Exception as exc:
            fail_count += 1
            print(f"  [ERROR] Failed to mask {tif_path.name}: {exc}")

    print("\n=== Processing Summary ===")
    print(f"Successful: {success_count}")
    print(f"Failed:     {fail_count}")

    if output_mode == "append":
        print(f"\nMasked outputs saved in:\n  {output_folder}")
    else:
        print(f"\nOriginal TIFF files were overwritten in:\n  {working_folder}")

    print("\n[DONE]")


if __name__ == "__main__":
    main()
