# coding=utf-8
from __future__ import print_function

import argparse
import logging
from concurrent import futures
import multiprocessing
import itertools
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from time import gmtime
from io import BytesIO
import json
from urllib.parse import urlparse
from os import makedirs, path
import hashlib

import boto3
import botocore
import mercantile
from marblecutter import get_resolution_in_meters, tiling
from marblecutter.catalogs import WGS84_CRS
from marblecutter.catalogs.postgis import PostGISCatalog
from marblecutter.formats.geotiff import GeoTIFF
from marblecutter.stats import Timer
from marblecutter.utils import Bounds
from mercantile import Tile

from ..catalogs import SpatialiteCatalog
from ..colormap import COLORMAP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("botocore.credentials").setLevel(logging.WARNING)
logging.getLogger("marblecutter.mosaic").setLevel(logging.WARNING)
logging.getLogger("rasterio._base").setLevel(logging.WARNING)

S3 = boto3.client("s3")

GEOTIFF_FORMAT = GeoTIFF(colormap=COLORMAP)


def build_catalog(tile, min_zoom, max_zoom):
    upstream_catalog = PostGISCatalog(table="land_cover")
    catalog = SpatialiteCatalog()

    for source in upstream_sources_for_tile(
        tile, upstream_catalog, min_zoom=min_zoom, max_zoom=max_zoom
    ):
        catalog.add_source(source)

    return catalog


# TODO fold this upstream, e.g. footprints.something
def upstream_sources_for_tile(tile, catalog, min_zoom=None, max_zoom=None):
    """Render a tile's source footprints."""
    bounds = Bounds(mercantile.bounds(tile), WGS84_CRS)
    shape = (256, 256)
    resolution = get_resolution_in_meters(bounds, shape)

    return catalog.get_sources(
        bounds,
        resolution,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        include_geometries=True,
    )


def generate_tiles(tile, max_zoom, index_zooms=None):
    index_zooms = None
    tiles = [tile]

    for z in range(tile.z, max_zoom + 1):
        if index_zooms is None or z in index_zooms:
            a, tiles = itertools.tee(tiles, 2)

            yield from a

        tiles = itertools.chain.from_iterable(mercantile.children(t) for t in tiles)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-x", type=int, required=True)
    parser.add_argument("-y", type=int, required=True)
    parser.add_argument("--zoom", "-z", type=int, required=True)
    parser.add_argument("--max-zoom", "-Z", type=int, required=True)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--concurrency", "-c", type=int, default=multiprocessing.cpu_count() * 2
    )
    parser.add_argument("--hash", "-H", action="store_true")
    parser.add_argument("target", default="file://./", nargs="?")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    root = Tile(args.x, args.y, args.zoom)
    concurrency = args.concurrency
    min_zoom = args.zoom
    max_zoom = args.max_zoom
    hash = args.hash

    logger.info(
        "Caching sources for root tile %s from zoom %d to %d", root, min_zoom, max_zoom
    )

    catalog = build_catalog(root, min_zoom, max_zoom)

    def render(tile_with_sources):
        tile, sources = tile_with_sources
        # logger.debug("sources: %s", list(map(lambda x: x["url"], sources)))
        logger.debug("rendering %d/%d/%d", tile.z, tile.x, tile.y)

        with Timer() as t:
            headers, data = tiling.render_tile_from_sources(
                tile, sources, format=GEOTIFF_FORMAT, scale=2
            )

        logger.debug(
            "%d/%d/%d) Took %.03fs to render tile (%s bytes), %s",
            tile.z,
            tile.x,
            tile.y,
            t.elapsed,
            len(data),
            headers.get("Server-Timing"),
        )

        return (tile, (headers, data))

    def sources_for_tile(tile):
        """Render a tile's source footprints."""
        bounds = Bounds(mercantile.bounds(tile), WGS84_CRS)
        # TODO scale
        shape = (512, 512)
        resolution = get_resolution_in_meters(bounds, shape)

        # convert sources to a list to avoid passing the generator across thread boundaries
        return (tile, list(catalog.get_sources(bounds, resolution)))

    with futures.ProcessPoolExecutor(max_workers=concurrency) as executor:
        # logger.info("%d/%d/%d", root.z, root.x, root.y)
        date_time = gmtime()[0:6]
        out = BytesIO()
        with ZipFile(out, "w", ZIP_DEFLATED, allowZip64=True) as archive:
            archive.comment = json.dumps(
                {
                    "name": "Land Cover",
                    "description": "Unified land cover, derived from MODIS-LC, ESACCI-LC, NLCD, and C-CAP.",
                    "minzoom": min_zoom,
                    "maxzoom": max_zoom,
                    "bbox": mercantile.bounds(root),
                    "formats": {"tif": "image/tiff"},
                    # omitted, as they don't help at the archive level
                    # "overviews": True,
                    # "metatile": 2 ** max_zoom,
                    "root": "{}/{}/{}".format(root.z, root.x, root.y),
                }
            ).encode("utf-8")

            for tile, (headers, data) in executor.map(
                render, map(sources_for_tile, generate_tiles(root, max_zoom))
            ):
                logger.info("%d/%d/%d", tile.z, tile.x, tile.y)

                info = ZipInfo("{}/{}/{}.tif".format(tile.z, tile.x, tile.y), date_time)
                info.external_attr = 0o755 << 16
                archive.writestr(info, data, ZIP_DEFLATED)

        url = urlparse(args.target)

        if url.scheme in ("", "file"):
            target = path.join(
                path.abspath(url.netloc + url.path),
                str(root.z),
                str(root.x),
                "{}.zip".format(root.y),
            )

            if not path.isdir(path.dirname(target)):
                makedirs(path.dirname(target))

            with open(target, "wb") as archive:
                archive.write(out.getvalue())
        elif url.scheme == "s3":
            bucket = url.netloc
            if hash:
                k = "{}/{}/{}".format(root.z, root.x, root.y)
                h = hashlib.md5(k.encode("utf-8")).hexdigest()[:5]
                key = path.join(url.path[1:], "{}/{}.zip".format(h, k))
            else:
                key = path.join(
                    url.path[1:], "{}/{}/{}.zip".format(root.z, root.x, root.y)
                )

            try:
                S3.put_object(
                    Body=out.getvalue(),
                    Bucket=bucket,
                    Key=key,
                    ContentType="image/tiff",
                )
            except botocore.exceptions.ClientError as e:
                logger.exception(e)