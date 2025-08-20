#!/usr/bin/env python3
"""
ECOSTRESS vs. Buoy Station Statistics Figure

This script calculates summary statistics (N, R², Bias, RMSE, MAE) for each buoy 
station comparing ECOSTRESS LST to buoy water temperature. It then generates a 
boxplot figure with one subplot per statistic to visualize station-level variability.

Inputs:
- CSV file containing matched buoy and ECOSTRESS LST data, including columns:
  'station_id', 'buoy_temp', 'ecostress_lst'
Outputs:
- Boxplot figure displaying N, R², Bias, RMSE, and MAE per station
"""
# Import the Libraries Needed for Making a Stats Figure
import pandas as pd
import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score

# CONFIGURATION
# Path to CSV file containing matched buoy and ECOSTRESS data
CSV_PATH = '<<< REPLACE_THIS_TEXT_WITH_MATCHED_CSV_PATH.csv >>>'
df = pd.read_csv(CSV_PATH)

# Compute statistics per station
stats_list = []

for station, group in df.groupby("station_id"):
    y_true = group["buoy_temp"]
    y_pred = group["ecostress_lst"]
    n = len(group)
    r2 = r2_score(y_true, y_pred)
    bias = np.mean(y_pred - y_true)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    mae = mean_absolute_error(y_true, y_pred)

    # Store stats in list
    stats_list.append({
        "station_id": station,
        "N": n,
        "R²": r2,
        "Bias": bias,
        "RMSE": rmse,
        "MAE": mae
    })

# Convert to DataFrame
stats_df = pd.DataFrame(stats_list)

# Melt the DataFrame for plotting
# This reshapes the data so that each row is one station-statistic combination
melted_df = stats_df.melt(id_vars="station_id",
                          value_vars=["N", "R²", "Bias", "RMSE", "MAE"],
                          var_name="Statistic", value_name="Value")

# Define custom y-axis limits and ticks per statistic (the axis will need to stretch based on values)
y_axis_limits = {
    "N": (0, 400),
    "R²": (0.35, 1.00),
    "Bias": (-1.5, 1.0),
    "RMSE": (1.5, 6.5),
    "MAE": (1.0, 5.0),
}
y_axis_ticks = {
    "N": np.arange(0, 400.001, 50),
    "R²": np.arange(0.35, 1.001, 0.05),
    "Bias": np.arange(-1.5, 1.001, 0.5),
    "RMSE": np.arange(1.5, 6.501, 0.5),
    "MAE": np.arange(1.0, 5.001, 0.5),
}

# Create boxplots for each statistic
fig, axes = plt.subplots(1, 5, figsize=(18, 5), sharex=False)

for ax, stat in zip(axes, y_axis_limits.keys()):
    subset = melted_df[melted_df["Statistic"] == stat]
    sns.boxplot(y="Value", data=subset, ax=ax, color='skyblue')
    ax.set_title(stat)
    ax.set_ylim(y_axis_limits[stat])
    ax.set_yticks(y_axis_ticks[stat])
    ax.set_xlabel("")  
    ax.set_ylabel("")  

# Adjust layout and show figure
plt.tight_layout(rect=[0, 0, 1, 0.95])
plt.show()
