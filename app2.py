"""Streamlit entry point for floodplain land-cover comparison."""

import json
import logging
import re
import shutil
import tempfile
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from threading import Lock

import ee
import folium
import geopandas as gpd
import numpy as np
import rasterio
import streamlit as st
from folium.plugins import Draw
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from streamlit_folium import st_folium

from analysis import ASSET, PRODUCTS, analyze, fetch_floodplain

st.set_page_config(page_title='Floodplain Land Use Change', page_icon='🌊', layout='wide')

COLORS = {
    'Floodplain extent': (36, 130, 192, 115),
    'Unchanged land cover': (36, 130, 192, 175),
    'Changed land cover': (219, 64, 48, 235),
}


@st.cache_resource
def earth_engine_lock():
    return Lock()


@contextmanager
def earth_engine_connection(project, key):
    credentials = None
    if key is not None:
        try:
            info = json.loads(key)
            if (not isinstance(info, dict) or info.get('type') != 'service_account'
                    or not info.get('client_email') or not info.get('private_key')):
                raise ValueError
            credentials = ee.ServiceAccountCredentials(info['client_email'], key_data=key)
        except (TypeError, ValueError) as error:
            raise ValueError('Upload a valid Google Cloud service account JSON key.') from error
    # The Earth Engine Python client keeps its connection in process-wide state.
    with earth_engine_lock():
        try:
            if credentials is None:
                ee.Initialize(project=project)
            else:
                ee.Initialize(credentials, project=project)
            yield
        finally:
            ee.Reset()


def connected_floodplain(project, key, geometry, grid, work):
    with earth_engine_connection(project, key):
        return fetch_floodplain(geometry, grid, work)


def config():
    try:
        return st.secrets.to_dict()
    except FileNotFoundError:
        return {}


def normalize_aoi(geometries):
    if geometries.empty or geometries.crs is None:
        raise ValueError('Provide a nonempty polygon boundary with a defined coordinate system.')
    geometries = geometries.to_crs(4326)
    for geometry in geometries.geometry:
        if (geometry is None or geometry.is_empty or not geometry.is_valid
                or geometry.geom_type not in ('Polygon', 'MultiPolygon')):
            raise ValueError('The boundary must contain valid polygon or multipolygon features.')
    merged = unary_union(geometries.geometry)
    if not np.isfinite(merged.bounds).all():
        raise ValueError('The boundary contains invalid coordinates.')
    return gpd.GeoDataFrame(geometry=[merged], crs=4326)


def uploaded_aoi(files, directory):
    components = {}
    for file in files:
        path = Path(file.name)
        if path.name != file.name or path.suffix.lower() not in {'.shp', '.shx', '.dbf', '.prj', '.cpg'}:
            raise ValueError('Upload the shapefile components for a single study boundary.')
        if path.suffix.lower() in components:
            raise ValueError('Upload only one shapefile.')
        components[path.suffix.lower()] = (path.stem.lower(), file)
    if not {'.shp', '.shx', '.dbf', '.prj'} <= components.keys() or len(
        {stem for stem, _ in components.values()}
    ) != 1:
        raise ValueError('Upload matching .shp, .shx, .dbf, and .prj files together.')
    for suffix, (_, file) in components.items():
        (directory / f'aoi{suffix}').write_bytes(file.getbuffer())
    return normalize_aoi(gpd.read_file(directory / 'aoi.shp'))


def drawing_map(flood_tiles):
    map_view = folium.Map(location=[30, -30], zoom_start=2, tiles='OpenStreetMap')
    if flood_tiles:
        folium.TileLayer(flood_tiles, name='GFPLAIN250m', attr='GFPLAIN250m / Google Earth Engine',
                         overlay=True, opacity=0.4).add_to(map_view)
        folium.LayerControl().add_to(map_view)
    Draw(draw_options={
        'polyline': False, 'circle': False, 'circlemarker': False, 'marker': False,
        'rectangle': True, 'polygon': {'allowIntersection': False},
    }).add_to(map_view)
    event = st_folium(map_view, key='aoi_map', height=450, use_container_width=True,
                      returned_objects=['all_drawings'])
    drawings = event.get('all_drawings') or []
    if drawings:
        return drawings[-1]['geometry']
    return None


def preview_raster(file_path):
    with rasterio.open(file_path) as source:
        with WarpedVRT(source, crs='EPSG:3857') as image:
            factor = max(image.width, image.height) / 1200
            width = max(1, round(image.width / max(1, factor)))
            height = max(1, round(image.height / max(1, factor)))
            data = image.read(1, out_shape=(height, width))
            bounds = transform_bounds(image.crs, 'EPSG:4326', *image.bounds)
    west, south, east, north = bounds
    return data, [[south, west], [north, east]]


def result_map(aoi, results):
    west, south, east, north = aoi.total_bounds
    map_view = folium.Map(tiles='OpenStreetMap')
    map_view.fit_bounds([[south, west], [north, east]])
    flood, fp_bounds = preview_raster(results / 'floodplain.tif')
    change, change_bounds = preview_raster(results / 'Final_Map.tif')
    for name, values, bounds, palette in (
        ('Floodplain extent', flood, fp_bounds, {1: COLORS['Floodplain extent']}),
        ('Land-cover comparison', change, change_bounds,
         {1: COLORS['Unchanged land cover'], 2: COLORS['Changed land cover']}),
    ):
        rgba = np.zeros((*values.shape, 4), dtype='uint8')
        for code, color in palette.items():
            rgba[values == code] = color
        folium.raster_layers.ImageOverlay(rgba, bounds, name=name, pixelated=True).add_to(map_view)
    folium.GeoJson(mapping(aoi.geometry.iloc[0]), name='Study boundary',
                   style_function=lambda _: {'fill': False, 'color': '#202b36',
                                             'weight': 2}).add_to(map_view)
    folium.LayerControl().add_to(map_view)
    legend = (
        '<div style="position:fixed;bottom:28px;left:12px;z-index:9999;'
        'background:white;color:#202b36;padding:8px 12px;border-radius:5px">'
        '<div><span style="color:#db4030">■</span> Changed land cover</div>'
        '<div><span style="color:#2482c0">■</span> Unchanged land cover</div>'
        '<div><span style="color:#86badf">■</span> Floodplain extent</div></div>'
    )
    map_view.get_root().html.add_child(folium.Element(legend))
    return map_view.get_root().render()


st.title('Floodplain Land Use Change')
st.write('Compare land-cover classes across two years within mapped floodplains.')
st.caption('Developed by Qianjin Zheng and Adnan Rajib, '
           '[H2I Lab, University of Texas at Arlington](https://www.adnanrajib.com). '
           '[Contact the lab](mailto:adnan.rajib@uta.edu).')

settings = config()
ee_settings = settings.get('earth_engine', {})
cds_key = settings.get('copernicus', {}).get('key')
with st.sidebar:
    project_name = st.text_input('Project name', 'Floodplain')
    product = st.selectbox('Land-cover dataset', list(PRODUCTS), index=1)
    low, high, resolution = PRODUCTS[product]
    initial = st.selectbox('Initial year', range(low, high + 1), key=f'initial_{product}')
    final = st.selectbox('Final year', range(low, high + 1), index=high - low,
                         key=f'final_{product}')
    coverage = 'Global land cover' if product == 'ESA CCI / C3S' else 'Conterminous U.S. land cover'
    st.caption(f'{coverage} · {resolution} m comparison grid')
    if product == 'ESA CCI / C3S' and not cds_key:
        st.info('Set the Copernicus CDS key in Streamlit Secrets to use ESA land cover.')

st.subheader('Step 3. Link your tool with Google Earth Engine')
st.markdown(
    'Sign in with your Google account and register a Google Cloud project for '
    '[Earth Engine](https://code.earthengine.google.com/register). Enter that project below.'
)
configured = ee_settings.get('project')
mode = 'Use my project'
if configured:
    mode = st.radio('Earth Engine connection', ['Use my project', 'Use the app’s configured project'],
                    horizontal=True)
if mode == 'Use my project':
    ee_project = st.text_input('Google Cloud project ID or number', placeholder='e.g., h2i-lab').strip()
    upload_key = st.file_uploader(
        'Google Cloud service account JSON key', type=['json'],
        help='Upload the key file downloaded from Google Cloud for the service account.',
    )
    ee_key = upload_key.getvalue().decode('utf-8-sig', errors='replace') if upload_key else None
    st.caption('[Create a service account key](https://cloud.google.com/iam/docs/keys-create-delete). '
               'Use a key only on a Streamlit deployment you trust.')
else:
    ee_project = str(configured)
    ee_key = ee_settings.get('credentials_json')

ee_ready = bool(ee_project and (ee_key or mode != 'Use my project'))
if not ee_ready:
    st.info('Enter your project ID or number and upload its service account JSON key to run analysis.')

st.subheader('Study area')
method = st.radio('Boundary source', ['Draw on map', 'Upload shapefile'], horizontal=True)
drawn, uploads = None, None
if method == 'Draw on map':
    st.caption('Zoom in and draw a rectangle or polygon. If you draw several, the last one is used.')
    tiles = None
    if ee_ready and st.checkbox('Show mapped floodplains while drawing'):
        try:
            with earth_engine_connection(ee_project, ee_key):
                image = ee.ImageCollection(ASSET).mosaic().remap([0], [1]).selfMask()
                tiles = image.getMapId({'palette': ['2482c0']})['tile_fetcher'].url_format
        except Exception:
            st.warning('The preview layer is unavailable. You can still draw a study boundary.')
    drawn = drawing_map(tiles)
else:
    uploads = st.file_uploader('Upload matching .shp, .shx, .dbf, and .prj files',
                               type=['shp', 'shx', 'dbf', 'prj', 'cpg'], accept_multiple_files=True)

if st.button('Run analysis', type='primary', disabled=not (ee_ready and (drawn or uploads))):
    st.session_state.pop('result', None)
    try:
        if initial >= final:
            raise ValueError('Choose a final year later than the initial year.')
        if product == 'ESA CCI / C3S' and not cds_key:
            raise ValueError('Configure a Copernicus CDS key before using the ESA dataset.')
        with tempfile.TemporaryDirectory(prefix='floodplain_') as directory:
            work = Path(directory)
            if drawn:
                aoi = normalize_aoi(gpd.GeoDataFrame(geometry=[shape(drawn)], crs=4326))
            else:
                aoi = uploaded_aoi(uploads, work)
            with st.status('Running floodplain analysis', expanded=True) as status:
                st.write('Connecting to Earth Engine and retrieving source data')
                floodplain_loader = partial(connected_floodplain, ee_project, ee_key)
                files, metrics = analyze(aoi, product, (initial, final), cds_key, work, st.write,
                                         floodplain_loader=floodplain_loader)
                st.write('Preparing the interactive map and GIS downloads')
                html = result_map(aoi, files)
                (files / 'CNC_Map.html').write_text(html, encoding='utf-8')
                archive = Path(shutil.make_archive(str(work / 'output'), 'zip', files))
                filename = re.sub(r'[^A-Za-z0-9_-]+', '_', project_name).strip('_') or 'Floodplain'
                st.session_state['result'] = (metrics, html, archive.read_bytes(), filename)
                status.update(label='Analysis complete', state='complete')
    except ValueError as error:
        st.error(str(error))
    except Exception as error:
        logging.getLogger(__name__).warning('Floodplain processing failed: %s', type(error).__name__)
        st.error('Analysis failed while accessing or processing the source data. Check service '
                 'credentials, dataset availability, and hosting logs before retrying.')
        # Provider exception text can include signed URLs or credentials.

if 'result' in st.session_state:
    metrics, html, archive, filename = st.session_state['result']
    st.subheader('Results')
    st.write(f"{metrics['source']} · {metrics['initial_year']} to {metrics['final_year']}")
    columns = st.columns(4)
    columns[0].metric('Mapped floodplain', f"{metrics['floodplain_km2']:,.2f} km²")
    columns[1].metric('Comparable area', f"{metrics['comparable_km2']:,.2f} km²")
    columns[2].metric('Changed area', f"{metrics['changed_km2']:,.2f} km²")
    percent = metrics['changed_percent']
    columns[3].metric('Changed share', f'{percent:.2f}%' if percent is not None else 'Unavailable')
    if percent is None:
        st.warning('The two years have no overlapping classified land-cover pixels in this floodplain.')
    elif percent == 0:
        st.info('No changes between the seven mapped land-cover classes were detected.')
    st.caption('Changed share uses the area with class data in both years.')
    st.components.v1.html(html, height=560)
    st.download_button('Download results ZIP', archive, f'{filename}_floodplain.zip',
                       mime='application/zip', on_click='ignore')

with st.expander('Data sources'):
    st.markdown(
        '[GFPLAIN250m](https://doi.org/10.1038/sdata.2018.309) · '
        '[USGS historical modeled land cover](https://doi.org/10.5066/F7KK99RR) · '
        '[USGS land cover projections](https://doi.org/10.5066/P95AK9HP) · '
        '[Annual NLCD](https://www.mrlc.gov/data) · '
        '[ESA CCI / Copernicus C3S](https://cds.climate.copernicus.eu/datasets/satellite-land-cover)'
    )
