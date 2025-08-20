#!/usr/bin/env python3
"""
ECOSTRESS vs. Buoy Water Temperature Scatter Plot

This script generates a scatter plot comparing buoy water temperature measurements
to matched ECOSTRESS LST values. Points are classified as day or night based on
local time, and statistics (R², RMSE, MAE, Bias) are computed and annotated
on the plot for day, night, and combined data.

Inputs:
- CSV file containing matched buoy and ECOSTRESS LST records, including:
  buoy_timestamp, ecostress_timestamp, station_id, latitude, longitude, buoy_temp, ecostress_lst
Outputs:
- Scatter plot displaying ECOSTRESS LST vs. buoy temperature with day/night color coding
  and regression statistics annotated on the figure.
"""
# Import the Libraries Needed for Making a Scatter Plot
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error, r2_score, mean_absolute_error
import numpy as np

# CONFIGURATION
# Path to CSV file containing matched buoy and ECOSTRESS data
CSV_PATH = '<<< REPLACE_THIS_TEXT_WITH_MATCHED_CSV_PATH.csv >>>'
df = pd.read_csv(CSV_PATH)

# Convert local time column to datetime assuming timestamp is in format YYYYMMDDhhmm
df['buoy_datetime'] = pd.to_datetime(df['buoy_timestamp'], format='%Y%m%d%H%M', errors='coerce')

# Remove rows with missing or invalid values
df = df.dropna(subset=['buoy_temp', 'ecostress_lst'])
df = df[(df['ecostress_lst'] > 0) & (df['buoy_temp'] > 0)]

# Classify day/night: define day as 6am–5:59pm, night as 6pm–5:59am
df['hour'] = df['buoy_datetime'].dt.hour
df['time_of_day'] = np.where(df['hour'].between(6, 17), 'day', 'night')

# Set up plot style
plt.figure(figsize=(10, 10))
sns.set_style("whitegrid")

# SCATTER PLOT BY TIME OF DAY
# Day points
sns.scatterplot(data=df[df['time_of_day'] == 'day'],
                x='buoy_temp', y='ecostress_lst',
                color='red', label='Day', alpha=0.6, edgecolor='k')
# Night points
sns.scatterplot(data=df[df['time_of_day'] == 'night'],
                x='buoy_temp', y='ecostress_lst',
                color='blue', label='Night', alpha=0.6, edgecolor='k')

# Add 1:1 reference line
min_temp = min(df['buoy_temp'].min(), df['ecostress_lst'].min()) - 1
max_temp = max(df['buoy_temp'].max(), df['ecostress_lst'].max()) + 1
plt.plot([min_temp, max_temp], [min_temp, max_temp], 'k--', linewidth=1, label='1:1 Line')

# Fit regression and annotate statistics
def fit_and_annotate(data, label, color, ypos):
    """Fit linear regression and annotate statistics on the plot."""
    if len(data) >= 2:
        X = data['buoy_temp'].values.reshape(-1, 1)
        y = data['ecostress_lst'].values
        reg = LinearRegression().fit(X, y)
        y_pred = reg.predict(X)
        r2 = r2_score(y, y_pred)
        rmse = mean_squared_error(y, y_pred) ** 0.5
        mae = mean_absolute_error(y, y_pred)
        bias = np.mean(y - X.flatten())
        n = len(data)

        # Label
        plt.text(
            0.05, ypos, label,
            transform=plt.gca().transAxes,
            fontsize=13, fontweight='bold', color=color,
            verticalalignment='top'
        )

        # Stats box
        plt.text(
            0.05, ypos - 0.135,
            f"N={n}\nR²={r2:.2f}\nRMSE={rmse:.2f}\nMAE={mae:.2f}\nBias={bias:.2f}",
            transform=plt.gca().transAxes,
            fontsize=13, color='black',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8)
        )

# Annotate day, night, and all
fit_and_annotate(df[df['time_of_day'] == 'day'], 'Day', 'red', ypos=0.97)
fit_and_annotate(df[df['time_of_day'] == 'night'], 'Night', 'blue', ypos=0.77)
fit_and_annotate(df, 'All', 'black', ypos=0.57)


# Customize axes, labels, title, legend
plt.xlim(0, 35)
plt.ylim(0, 35)
plt.xticks(np.arange(0, 36, 5))
plt.yticks(np.arange(0, 36, 5))
plt.xlabel('Buoy Water Temperature (°C)', fontsize=14, fontweight='bold')
plt.ylabel('ECOSTRESS LST (°C)', fontsize=14, fontweight='bold')
plt.title('ECOSTRESS LST vs. Buoy Temperature (All Stations)', fontsize=16, fontweight='bold')
plt.legend(loc="lower right")
plt.tight_layout()

# Show plot
plt.show()
