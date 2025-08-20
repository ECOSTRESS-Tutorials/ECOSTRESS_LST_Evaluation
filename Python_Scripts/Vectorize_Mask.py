#!/usr/bin/env python3
"""
ECOSTRESS Water Mask to Land Polygons

This script converts an ECOSTRESS water mask TIFF into land polygons clipped to
a study area, with optional smoothing.

Inputs:
  - ECOSTRESS water TIFF in EPSG:32617
  - Area_of_Interest.shp in EPSG:4326
Outputs (in input folder, all in EPSG:4326):
  - binary_summed.tif
  - binary_summed_clipped.tif
  - rigid.shp
  - smooth.shp
  - smoother.shp
  - smoothest.shp
"""

# Import the Libraries Needed for Polygonization 
import os
import numpy as np
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.mask import mask
from rasterio.features import shapes
import fiona
from shapely.geometry import shape, mapping

# CONFIGURATION
INPUT_FOLDER = "<<< REPLACE_THIS_TEXT_WITH_FOLDER_PATH >>>"
INPUT_TIF = os.path.join(INPUT_FOLDER, "<<< REPLACE_THIS_TEXT_WITH_TIFF_NAME.tif >>>")
AOI_SHP = os.path.join(INPUT_FOLDER, "<<< REPLACE_THIS_TEXT_WITH_AOI_NAME.shp >>>")

def main():
    if not os.path.exists(INPUT_TIF):
        print(f"ERROR: {INPUT_TIF} not found.")
        return
    if not os.path.exists(AOI_SHP):
        print(f"ERROR: {AOI_SHP} not found.")
        return

    dst_crs = "EPSG:4326"

    # Reproject TIFF to EPSG:4326
    with rasterio.open(INPUT_TIF) as src:
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        out_meta = src.meta.copy()
        out_meta.update({
            "crs": dst_crs,
            "transform": transform,
            "width": width,
            "height": height,
            "dtype": rasterio.uint8,
            "count": 1
        })

        src_arr = src.read(1)
        reprojected = np.zeros((height, width), dtype=np.uint8)
        reproject(
            source=src_arr,
            destination=reprojected,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform,
            dst_crs=dst_crs,
            resampling=Resampling.nearest
        )

    # Threshold and write binary_summed.tif
    binary = (reprojected != 0).astype(np.uint8)
    binary_path = os.path.join(INPUT_FOLDER, "binary_summed.tif")
    with rasterio.open(binary_path, "w", **out_meta) as dst:
        dst.write(binary, 1)

    # Clip to AOI 
    with fiona.open(AOI_SHP, "r") as shp:
        shapes_geom = [feat["geometry"] for feat in shp]

    with rasterio.open(binary_path) as src:
        clipped_img, clipped_transform = mask(src, shapes_geom, crop=True)
        clipped_meta = src.meta.copy()
        clipped_meta.update({
            "height": clipped_img.shape[1],
            "width": clipped_img.shape[2],
            "transform": clipped_transform
        })

    clipped_path = os.path.join(INPUT_FOLDER, "binary_summed_clipped.tif")
    with rasterio.open(clipped_path, "w", **clipped_meta) as dst:
        dst.write(clipped_img)

    # Polygonize land 
    with rasterio.open(clipped_path) as src:
        arr = src.read(1)
        transform = src.transform
        crs = src.crs

    land_mask = (arr == 0)
    raw_polys = [
        shape(geom) for geom, val
        in shapes(arr, mask=land_mask, transform=transform)
    ]

    schema = {"geometry": "Polygon", "properties": {}}
    rigid_path = os.path.join(INPUT_FOLDER, "rigid.shp")
    with fiona.open(rigid_path, "w", driver="ESRI Shapefile", crs=crs, schema=schema) as dst:
        for poly in raw_polys:
            if not poly.is_valid:
                poly = poly.buffer(0)
            dst.write({"geometry": mapping(poly), "properties": {}})

    # Smoothing calculates base pixel size from raster transform
    base_px = min(abs(transform.a), abs(transform.e))
    # Smaller fractions have less smoothing (more rigid polygons)
    # Larger fractions have more smoothing (more rounded polygons)
    smoothing_specs = [
        ("smooth.shp",     0.5),
        ("smoother.shp",   0.75),
        ("smoothest.shp",  1.0)
    ]

    for filename, factor in smoothing_specs:
        dist = base_px * factor
        smooth_path = os.path.join(INPUT_FOLDER, filename)
        with fiona.open(smooth_path, "w", driver="ESRI Shapefile", crs=crs, schema=schema) as dst:
            for poly in raw_polys:
                smoothed = poly.buffer(-dist).buffer(dist)
                final = smoothed if not smoothed.is_empty else poly
                if not final.is_valid:
                    final = final.buffer(0)
                dst.write({"geometry": mapping(final), "properties": {}})

    # Finish
    print("Finished. Outputs saved in:", INPUT_FOLDER)
    print(" • binary_summed.tif")
    print(" • binary_summed_clipped.tif")
    print(" • rigid.shp")
    print(" • smooth.shp")
    print(" • smoother.shp")
    print(" • smoothest.shp")

if __name__ == "__main__":
    main()

