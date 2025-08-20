#!/usr/bin/env python3
"""
ECOSTRESS - Buoy Temperature PDF Plot

This script generates a Probability Density Function (PDF) plot showing the distribution
of residuals between ECOSTRESS LST measurements and buoy water temperature. Reference
lines indicate zero difference and a target error range.

Inputs:
- CSV file containing matched buoy and ECOSTRESS LST data, including columns:
  'buoy_temp' and 'ecostress_lst'
Outputs:
- PDF plot of ECOSTRESS - Buoy residuals with reference lines
"""
# Import the Libraries Needed for Making a PDF Plot.
import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# CONFIGURATION
# Path to CSV file containing matched data
CSV_PATH = "<<< REPLACE_THIS_TEXT_WITH_CSV_PATH.csv >>>"

def main():
    # Verify CSV file exists
    if not os.path.isfile(csv_file):
        print(f"Error: '{csv_file}' not found.")
        return

    # Load the data
    df = pd.read_csv(csv_file)

    # Check that necessary columns exists
    required_cols = {'ecostress_lst', 'buoy_temp'}
    if not required_cols.issubset(df.columns):
        print(f"Error: CSV is missing required columns: {required_cols}")
        return

    # Compute residuals (ECOSTRESS - Buoy)
    df['residual'] = df['ecostress_lst'] - df['buoy_temp']
    residuals = df['residual'].dropna()

    if residuals.empty:
        print("No valid data to plot.")
        return

    # Plot PDF of residuals
    plt.figure(figsize=(8, 5))
    sns.kdeplot(residuals, fill=True, color='steelblue', linewidth=2, label='PDF: ECOSTRESS - Buoy')

    # Add reference lines
    plt.axvline(0, color='red', linestyle='--', label='Zero Difference')
    plt.axvline(-1, color='green', linestyle='--', label='Target Error [-1,1]')
    plt.axvline(1, color='green', linestyle='--')

    # Set axis limits
    plt.xlim(-6, 6)
    plt.ylim(0, 0.5)

    # Add labels, title, legend, and formatting
    plt.title('PDF of ECOSTRESS LST - Buoy Temperature')
    plt.xlabel('Temperature Difference (°C)')
    plt.ylabel('Density')
    plt.legend()
    plt.grid(True)
    plt.tight_layout()

    # Show plot 
    plt.show()

if __name__ == '__main__':
    main()
