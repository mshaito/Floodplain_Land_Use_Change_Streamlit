"""Download source data and compare categorical coverage on a shared grid."""

import json
import math
import shutil
from pathlib import Path
from zipfile import ZipFile

import cdsapi
import ee
import gdown
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
import xarray as xr
from rasterio.features import geometry_mask, shapes
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling, transform_bounds
from shapely.geometry import mapping, shape

ASSET = 'IAHS/GFPLAIN250/v0'
MRLC = 'https://www.mrlc.gov/downloads/sciweb1/shared/mrlc/data-bundles'
PRODUCTS = {
    'USGS modeled land cover': (1941, 2000, 250),
    'Annual NLCD': (1985, 2025, 30),
    'ESA CCI / C3S': (1992, 2022, 300),
}
CLASSES = {
    1: 'Water / ice',
    2: 'Developed',
    3: 'Barren',
    4: 'Forest',
    5: 'Shrub / grass / sparse vegetation',
    6: 'Agriculture',
    7: 'Wetlands',
}
CROSSWALKS = {
    'USGS modeled land cover': ((1, 17), (2, 3, 4, 5, 6), (7,), (8, 9, 10),
                                (11, 12, 14), (13,), (15, 16)),
    'Annual NLCD': ((11, 12), (21, 22, 23, 24), (31,), (41, 42, 43),
                    (51, 52, 71), (81, 82), (90, 95)),
    'ESA CCI / C3S': (
        (210, 220), (190,), (200, 201, 202),
        (50, 60, 61, 62, 70, 71, 72, 80, 81, 82, 90, 100, 160, 170),
        (110, 120, 121, 122, 130, 140, 150, 151, 152, 153),
        (10, 11, 12, 20, 30, 40), (180,),
    ),
}


def download(url, target):
    with requests.get(url, stream=True, timeout=(20, 180)) as response:
        response.raise_for_status()
        with target.open('wb') as destination:
            for chunk in response.iter_content(1024 * 1024):
                destination.write(chunk)


def unpack_one(archive, suffix, target, preferred=None):
    with ZipFile(archive) as bundle:
        members = [m for m in bundle.namelist() if m.lower().endswith(suffix)]
        if preferred:
            members = [m for m in members if Path(m).name == preferred]
        if len(members) != 1:
            raise ValueError(f'Expected one {suffix} data file in the source archive.')
        with bundle.open(members[0]) as source, target.open('wb') as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
    archive.unlink()


def write_tif(path, data, transform, crs, nodata=0):
    bands = np.asarray(data)
    if bands.ndim == 2:
        bands = bands[np.newaxis, ...]
    with rasterio.open(
        path, 'w', driver='GTiff', width=bands.shape[2], height=bands.shape[1],
        count=bands.shape[0], dtype=bands.dtype, transform=transform, crs=crs,
        nodata=nodata, compress='deflate', tiled=True,
    ) as destination:
        destination.write(bands)


def read_on_grid(path, grid):
    transform, width, height, crs = grid
    with rasterio.open(path) as source:
        with WarpedVRT(source, crs=crs, transform=transform, width=width,
                       height=height, resampling=Resampling.nearest) as warped:
            return warped.read(1, masked=True)


def make_grid(bounds, meters, crs):
    west, south, east, north = transform_bounds('EPSG:4326', crs, *bounds)
    west = math.floor(west / meters) * meters
    north = math.ceil(north / meters) * meters
    width = math.ceil((east - west) / meters)
    height = math.ceil((north - south) / meters)
    if width * height > 4_000_000 or max(width, height) > 9000:
        raise ValueError('The study area is too large at this resolution. Draw or upload a smaller area.')
    return from_origin(west, north, meters, meters), width, height, crs


def floodplain_mask():
    # The catalog raster uses zero for floodplain and masks other pixels.
    return ee.Image(ASSET).select('flood').remap([0], [1]).rename('Floodplain')


def fetch_floodplain(geometry, grid, work):
    transform, width, height, crs = grid
    region = ee.Geometry(mapping(geometry), proj='EPSG:4326', geodesic=False)
    a, b, c, d, e, f = tuple(transform)[:6]
    image = floodplain_mask().clip(region).unmask(0).toByte()
    url = image.getDownloadURL({
        'crs': crs, 'crs_transform': [a, b, c, d, e, f],
        'dimensions': [width, height], 'format': 'GEO_TIFF',
    })
    path = work / 'floodplain_source.tif'
    download(url, path)
    return np.ma.filled(read_on_grid(path, grid), 0) == 1


def usgs_links(work):
    archive = work / 'lookup.zip'
    if not gdown.download(id='1C_0bSvaPMj_7u3qhNitBQUb-F0mOPxRs',
                          output=str(archive), quiet=True):
        raise ValueError('The USGS lookup table could not be downloaded.')
    csv = work / 'lookup.csv'
    unpack_one(archive, '.csv', csv, 'USGS_lookup_table.csv')
    data = pd.read_csv(csv)
    return dict(zip(data['Year'].astype(int), data['Link']))


def fetch_land_cover(product, year, bounds, work, links, cds_key):
    target = work / f'source_{year}.tif'
    archive = work / 'source.zip'
    if product == 'USGS modeled land cover':
        if year not in links:
            raise ValueError(f'The USGS lookup table has no entry for {year}.')
        download(links[year], target)
    elif product == 'Annual NLCD':
        filename = f'Annual_NLCD_LndCov_{year}_CU_C1V2'
        download(f'{MRLC}/{filename}.zip', archive)
        unpack_one(archive, '.tif', target, f'{filename}.tif')
    else:
        version = 'v2_0_7cds' if year < 2016 else 'v2_1_1'
        client = cdsapi.Client(url='https://cds.climate.copernicus.eu/api',
                               key=cds_key, quiet=True, timeout=90)
        client.retrieve('satellite-land-cover', {
            'variable': 'all', 'year': [str(year)], 'version': [version],
        }, str(archive))
        nc = work / 'landcover.nc'
        unpack_one(archive, '.nc', nc)
        clip_esa(nc, bounds, target)
        nc.unlink()
    return target


def clip_esa(nc, bounds, target):
    west, south, east, north = bounds
    with xr.open_dataset(nc, mask_and_scale=False) as dataset:
        source = dataset['lccs_class']
        lat = source.lat.values
        lon = source.lon.values
        dx, dy = abs(float(lon[1] - lon[0])), abs(float(lat[1] - lat[0]))
        ys = np.flatnonzero((lat >= south - 2 * dy) & (lat <= north + 2 * dy))
        xs = np.flatnonzero((lon >= west - 2 * dx) & (lon <= east + 2 * dx))
        if not ys.size or not xs.size:
            raise ValueError('The requested area lies outside the ESA raster.')
        selection = source.isel(lat=slice(ys[0], ys[-1] + 1),
                                lon=slice(xs[0], xs[-1] + 1))
        selection = selection.squeeze(dim=[d for d in selection.dims if d not in ('lat', 'lon')],
                                      drop=True).transpose('lat', 'lon')
        if lat[0] < lat[-1]:
            selection = selection.isel(lat=slice(None, None, -1))
        if lon[0] > lon[-1]:
            selection = selection.isel(lon=slice(None, None, -1))
        transform = from_origin(float(selection.lon[0]) - dx / 2,
                                float(selection.lat[0]) + dy / 2, dx, dy)
        # Some files store unsigned class IDs in an int8 container.
        write_tif(target, selection.values.astype('uint8'), transform, 'EPSG:4326')


def reclassify(source, product, eligible):
    result = np.zeros(source.shape, dtype='uint8')
    valid = eligible & ~np.ma.getmaskarray(source)
    for category, codes in enumerate(CROSSWALKS[product], start=1):
        result[valid & np.isin(source.data, codes)] = category
    return result


def classify_change(initial, final):
    comparable = (initial > 0) & (final > 0)
    change = np.full(initial.shape, 255, dtype='uint8')
    change[comparable] = np.where(initial[comparable] == final[comparable], 1, 2)
    return change


def write_floodplain_shape(mask, grid, path):
    transform, _, _, crs = grid
    geometries = [shape(geom) for geom, _ in shapes(mask.astype('uint8'), mask=mask,
                                                     transform=transform)]
    gpd.GeoDataFrame({'raster_val': [1] * len(geometries)}, geometry=geometries,
                     crs=crs).to_crs(4326).to_file(path, index=False)


def analyze(aoi, product, years, cds_key, work, progress, *, floodplain_loader=fetch_floodplain):
    geometry = aoi.geometry.iloc[0]
    bounds = tuple(geometry.bounds)
    if bounds[2] - bounds[0] > 180 or bounds[1] < -85 or bounds[3] > 85:
        raise ValueError('Use an area within ±85° latitude that does not span the antimeridian.')
    if product != 'ESA CCI / C3S':
        west, south, east, north = bounds
        if west < -125 or east > -66 or south < 24 or north > 50:
            raise ValueError(
                f'This dataset covers the conterminous United States. The selected boundary spans '
                f'longitude {west:.4f} to {east:.4f} and latitude {south:.4f} to {north:.4f}. '
                'Clear and redraw the area if these coordinates differ from your intended location, '
                'or choose ESA for a location outside the conterminous United States.'
            )
    _, _, meters = PRODUCTS[product]
    crs = 'EPSG:6933' if product == 'ESA CCI / C3S' else 'EPSG:5070'
    grid = make_grid(bounds, meters, crs)
    fp_grid = make_grid(bounds, 250, crs)
    progress('Extracting GFPLAIN250m floodplain extent')
    flood = floodplain_loader(geometry, fp_grid, work)
    fp_transform, _, _, _ = fp_grid
    projected = aoi.to_crs(crs).geometry.iloc[0]
    flood &= geometry_mask([mapping(projected)], flood.shape, fp_transform, invert=True)
    if not flood.any():
        raise ValueError('GFPLAIN250m has no mapped floodplain pixels inside this study area.')
    results = work / 'results'
    results.mkdir()
    fp_path = results / 'floodplain.tif'
    write_tif(fp_path, flood.astype('uint8'), fp_transform, crs)
    write_floodplain_shape(flood, fp_grid, results / 'floodplain.shp')
    aoi.to_file(results / 'boundary_GCS.shp', index=False)
    transform, width, height, _ = grid
    eligible = np.ma.filled(read_on_grid(fp_path, grid), 0) == 1
    eligible &= geometry_mask([mapping(projected)], (height, width), transform, invert=True)
    links = usgs_links(work) if product == 'USGS modeled land cover' else None
    arrays = []
    for year, label in zip(years, ('initial', 'final')):
        progress(f'Retrieving and processing {product} for {year}')
        source = fetch_land_cover(product, year, bounds, work, links, cds_key)
        raster = reclassify(read_on_grid(source, grid), product, eligible)
        source.unlink()
        arrays.append(raster)
        write_tif(results / f'landuse_{label}.tif', raster, transform, crs)
    change = classify_change(*arrays)
    final_map = results / 'Final_Map.tif'
    write_tif(final_map, change, transform, crs, nodata=255)
    pixel_area = meters ** 2 / 1_000_000
    changed = float(np.count_nonzero(change == 2) * pixel_area)
    compared = float(np.count_nonzero(change != 255) * pixel_area)
    metrics = {
        'source': product, 'initial_year': years[0], 'final_year': years[1],
        'floodplain_km2': float(np.count_nonzero(flood) * 0.0625),
        'comparable_km2': compared, 'changed_km2': changed,
        'changed_percent': 100 * changed / compared if compared else None,
        'analysis_crs': crs, 'pixel_size_m': meters, 'landcover_codes': CLASSES,
        'change_codes': {1: 'Unchanged', 2: 'Changed', 255: 'No comparison'},
    }
    (results / 'summary.json').write_text(json.dumps(metrics, indent=2))
    return results, metrics
