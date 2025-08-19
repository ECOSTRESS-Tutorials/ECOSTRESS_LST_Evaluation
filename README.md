# ECOSTRESS_LST_Evaluation
Evaluating ECOSTRESS land surface temperature data over coastal waters with in-situ buoy observations. A detailed pre-processing workflow.

This workflow is designed to evaluate ECOSTRESS Land Surface Temperature (LST) observations against NOAA NDBC buoy water temperature measurements in coastal environments.

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

The workflow enables global-scale validation of ECOSTRESS for coastal monitoring and provides an open, reproducible framework for integrating satellite and in-situ thermal data.

REPOSITORY CONTENTS

- NDBC_Retrieval+Binning.py – Download and average buoy water temperature data
- ECOSTRESS_QC_Filter.py – Apply quality control filters to ECOSTRESS scenes
- Geolocation Shift – Manually shift scenes using Georeferencer
- Vectorize_Mask.py - Invert and vectorize the ECOSTRESS water product
- Land_Mask.py – Remove land pixels from scenes
- Calibration_LOESS_Graphs.py - Develop an average baseline and apply threshold
- Compute_ALL_LOESS.py – Remove edge-of-cloud artifacts from scenes
- ECOSTRESS_binning.py – Align ECOSTRESS overpasses with buoy timestamps
- Pixel_Window_Extraction.py – Extract and average pixels around buoy locations
- Scatter_Plot.py – Create scatterplot visualization with statistics
- PDF_Plot.py - Probability Density Function plot
- Statistics_Boxplots.py - Individual buoy statistics box and whisker plots

CONTACTS
If you have any questions, encounter issues, or would like to suggest improvements, please contact jacquelyn.s.meiseles@jpl.nasa.gov, quinmeiseles@gmail.com
