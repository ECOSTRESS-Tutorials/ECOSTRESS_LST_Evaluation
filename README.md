# ECOSTRESS_LST_Validation
This processing and buoy-matchup workflow is designed to evaluate ECOSTRESS Land Surface Temperature (LST) observations against NOAA NDBC buoy water temperature measurements in coastal environments.

Please navigate to the ECOSTRESS Tutorials Repository to familiarize yourself with ECOSTRESS products. https://github.com/ECOSTRESS-Tutorials

OVERVIEW

Coastal environments are experiencing increasing thermal stress due to climate change, yet monitoring water surface temperatures at high resolution remains a challenge. ECOSTRESS, aboard the International Space Station (ISS), provides thermal observations at ~70 m spatial resolution, but its coastal applications require careful preprocessing and validation.

This repository contains a set of Python scripts that:
- Retrieve NDBC buoy water temperature records
- Filter and quality-control ECOSTRESS LST scenes
- Correct geolocation shifts for accurate alignment
- Mask out land and cloud-contaminated pixels
- Extract nearshore pixels at buoy locations
- Compare ECOSTRESS LST with in-situ buoy temperatures
- Generate figures and statistics for validation

This workflow enables global-scale validation of ECOSTRESS for coastal monitoring and provides an open, reproducible framework for integrating satellite and in-situ thermal data.

REPOSITORY CONTENTS

- NDBC_v2.py – Download and average buoy water temperature data
- QC_v22.py – Run quality and confidence controls on ECOSTRESS scenes
- GeoViewer_69.py – Manually shift scenes using GeoViewer
- Land_shp_v2.py - Invert and vectorize the ECOSTRESS water mask layer to produce a land boundary shapefile.
- Land_Mask_v3.py – Remove land pixels from scenes
- LOESS_v16.py - Develop an average baseline and apply thresholds to remove outliers from scenes
- Binning_v4.py – Align ECOSTRESS overpasses with buoy timestamps
- Pixel_Extraction_v4.py – Extract and average pixels around buoy locations
- Scatter_Plot_v2.py – Create scatterplot visualization with statistics
- PDF_Plot_v2.py - Create a probability density function plot
- Statistics_Boxplots.py - Individual buoy statistics box and whisker plots

CONTACTS

If you have any questions, encounter issues, or would like to suggest improvements, please contact jacquelyn.s.meiseles@jpl.nasa.gov, meiseles@chapman.edu, quinmeiseles@gmail.com
