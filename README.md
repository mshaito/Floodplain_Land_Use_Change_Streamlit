# Detection of Floodplain Land Use Change

Streamlit adaptation of the Colab notebook developed by Qianjin Zheng and Adnan Rajib at the [Hydrology and Hydroinformatics Innovation Lab](https://www.adnanrajib.com), University of Texas at Arlington. Contact: [adnan.rajib@uta.edu](mailto:adnan.rajib@uta.edu).

## Run locally

Use Python 3.12. From this directory, run:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .streamlit/secrets.example.toml .streamlit/secrets.toml
python -m streamlit run app.py
```

Enter your Google Cloud project and upload a service account key in the app. The local secrets file is optional. To use local Earth Engine credentials from `earthengine authenticate`, configure just the project in local secrets and select the app’s configured project.

## Deploy on Streamlit Community Cloud

1. Extract the ZIP and commit its contents, including `.gitignore` and the example secrets file, to a GitHub repository. Keep your real secrets file out of Git.
2. In [Streamlit Community Cloud](https://share.streamlit.io/), create an app from that repository and set the entry point to `app.py`. Choose Python 3.12 in Advanced settings.
3. Optionally, to offer a shared Earth Engine connection, in the [Secrets field](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management), paste the configured TOML from the example. Set `earth_engine.project` to an [Earth Engine registered Google Cloud project](https://developers.google.com/earth-engine/guides/auth) with the Earth Engine API enabled. Paste the full [service account JSON](https://developers.google.com/earth-engine/guides/service_account) within the triple single quotes for `credentials_json`. Remove the comment markers on those lines. Give the service account access to use Earth Engine and the selected project.
4. For ESA analysis, set `copernicus.key` to a fresh [Climate Data Store personal access token](https://cds.climate.copernicus.eu/how-to-api) and accept the [land-cover dataset terms](https://cds.climate.copernicus.eu/datasets/satellite-land-cover?tab=download) with that account. The original notebook contains an exposed token. Revoke it and generate a replacement before deployment.

## Run an analysis

1. Enter a project name, choose a dataset, and pick two different years in ascending order.
2. Under **Step 3. Link your tool with Google Earth Engine**, sign in to Google and register your Cloud project for Earth Engine. Enter its project ID or number and upload the JSON key from a service account authorized to use the project. If the deployment owner provided Earth Engine credentials in Secrets, you can instead choose **Use the app’s configured project**. Upload keys only to an app deployment you trust.
3. Zoom to your area and draw a rectangle or polygon. Alternatively, upload matching `.shp`, `.dbf`, `.shx`, and `.prj` files together, with an optional `.cpg`.
4. Run the analysis and download the results ZIP. It includes the WGS 84 study boundary and floodplain shapefiles, the floodplain mask, both reclassified land-cover GeoTIFFs, a change GeoTIFF, an interactive HTML map, and a JSON summary with class codes.

| Source | Years available in the app | Scope | Comparison grid |
| --- | --- | --- | --- |
| [USGS modeled land cover and projections](https://doi.org/10.5066/F7KK99RR) | 1941–2000, using the notebook lookup table | Conterminous U.S. | 250 m |
| [Annual NLCD Collection 1.2](https://www.mrlc.gov/data) | 1985–2025 | Conterminous U.S. | 30 m |
| [ESA CCI / Copernicus C3S](https://cds.climate.copernicus.eu/datasets/satellite-land-cover) | 1992–2022, as configured in the notebook | Global land cover | 300 m |

The tool uses the [GFPLAIN250m](https://developers.google.com/earth-engine/datasets/catalog/IAHS_GFPLAIN250_v0) floodplain layer. Results require mapped floodplains and land-cover data at the selected location. The floodplain output uses a 250 m equal-area grid. Both land-cover years are aligned with nearest-neighbor resampling onto the product's equal-area comparison grid. The changed share divides changed area by the floodplain area with recognized class data in both years. Change codes are 1 for unchanged, 2 for changed, and 255 for no comparison. Only transitions between the notebook's seven reclassified categories are counted.

The app accepts regional study areas, with a maximum of 4 million cells per grid and no antimeridian crossings. NLCD still downloads national rasters and ESA CDS requests may queue. These jobs can take several minutes and can exceed the available resources of some hosting plans. Each run uses its own temporary files and removes them after building the results.
